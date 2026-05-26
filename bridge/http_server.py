"""HTTP server on :1904 — exposes:

- POST /from-discord       legacy webhook for node-red's discordMessage flow
- POST /agent-notify       text-only DM (JSON body: agent/task/status/detail)
- POST /agent-notify-file  DM with attachments (multipart/form-data)
- GET  /healthz            liveness ping
"""
import cgi
import io
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import DISCORD_DEFAULT_CHANNEL
from .gateway import gateway_state
from .inbound import process_discord_inbound
from .notify import discord_send_dm, format_notify_message


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stdout.write(f"[http] {self.address_string()} - {fmt % args}\n")

        def _json(self, code, payload):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

        def _read_json_body(self):
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length).decode('utf-8')
            return json.loads(raw)

        def _handle_from_discord(self):
            try:
                body = self._read_json_body()
            except Exception as e:
                return self._json(400, {'ok': False, 'error': f'parse_fail: {e}'})
            status, resp = process_discord_inbound(
                content=body.get('content'),
                author=body.get('author'),
                author_id=body.get('author_id'),
                channel=body.get('channel') or '',
                to_name_hint=body.get('to_name'),
                db_path=db_path,
                attachments=body.get('attachments'),
            )
            return self._json(status, resp)

        def _handle_agent_notify(self):
            """Python equivalent of node-red /agent-notify, REST API send.

            Request JSON (identical schema to node-red endpoint):
                agent:   str  (required)
                task:    str  (optional, short title)
                status:  str  (info|done|fail|warn; default info)
                detail:  str  (optional, body)
                channel: str  (optional, DM channel id; default trusted user DM)
            """
            try:
                body = self._read_json_body()
            except Exception as e:
                return self._json(400, {'ok': False, 'error': f'parse_fail: {e}'})

            agent = (body.get('agent') or '').strip()
            if not agent:
                return self._json(400, {'ok': False, 'error': 'missing_agent'})
            task = (body.get('task') or '').strip()
            status = (body.get('status') or 'info').strip().lower()
            detail = body.get('detail') or ''
            channel = (body.get('channel') or DISCORD_DEFAULT_CHANNEL).strip()

            content = format_notify_message(agent, task, status, detail)
            ok, code, resp_body = discord_send_dm(channel, content)

            if ok:
                sys.stdout.write(f"[http] agent-notify OK agent={agent} ch={channel} "
                                 f"task={task[:40]!r}\n")
                # Plain text response so client schema checks pass.
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
                return

            sys.stdout.write(f"[http] agent-notify FAIL agent={agent} code={code} "
                             f"body={resp_body!r}\n")
            return self._json(502, {'ok': False, 'error': 'discord_send_fail',
                                    'discord_status': code, 'discord_body': resp_body})

        def _handle_agent_notify_file(self):
            """multipart/form-data variant of /agent-notify.

            Form fields:
              payload_json   JSON string with {agent, task, status, detail, channel?}
              files[N]       one or more file parts (filename header preserved)
            """
            ctype = self.headers.get('Content-Type', '')
            if not ctype.startswith('multipart/form-data'):
                return self._json(400, {'ok': False,
                                        'error': 'expected multipart/form-data'})
            try:
                # cgi.FieldStorage handles multipart parsing
                fs = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST',
                             'CONTENT_TYPE': ctype},
                    keep_blank_values=True,
                )
            except Exception as e:
                return self._json(400, {'ok': False, 'error': f'multipart_parse: {e}'})

            if 'payload_json' not in fs:
                return self._json(400, {'ok': False, 'error': 'missing payload_json'})
            try:
                body = json.loads(fs.getfirst('payload_json'))
            except Exception as e:
                return self._json(400, {'ok': False, 'error': f'payload_json_parse: {e}'})

            agent = (body.get('agent') or '').strip()
            if not agent:
                return self._json(400, {'ok': False, 'error': 'missing_agent'})
            task = (body.get('task') or '').strip()
            status = (body.get('status') or 'info').strip().lower()
            detail = body.get('detail') or ''
            channel = (body.get('channel') or DISCORD_DEFAULT_CHANNEL).strip()

            attachments = []
            for key in fs.keys():
                if not key.startswith('files['):
                    continue
                item = fs[key]
                items = item if isinstance(item, list) else [item]
                for it in items:
                    if not getattr(it, 'filename', None):
                        continue
                    attachments.append((it.filename, it.file.read()))
            if not attachments:
                return self._json(400, {'ok': False,
                                        'error': 'no_files',
                                        'hint': 'expected one or more files[N] parts'})

            content = format_notify_message(agent, task, status, detail)
            ok, code, resp_body = discord_send_dm(channel, content, attachments=attachments)

            if ok:
                sizes = ", ".join(f"{fn}({len(b)}B)" for fn, b in attachments)
                sys.stdout.write(f"[http] agent-notify-file OK agent={agent} ch={channel} "
                                 f"files=[{sizes}] task={task[:40]!r}\n")
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
                return

            sys.stdout.write(f"[http] agent-notify-file FAIL agent={agent} code={code} "
                             f"body={resp_body!r}\n")
            return self._json(502, {'ok': False, 'error': 'discord_send_fail',
                                    'discord_status': code, 'discord_body': resp_body})

        def do_POST(self):
            if self.path == '/agent-notify':
                return self._handle_agent_notify()
            if self.path == '/agent-notify-file':
                return self._handle_agent_notify_file()
            if self.path == '/from-discord':
                return self._handle_from_discord()
            return self._json(404, {'ok': False, 'error': 'unknown_path'})

        def do_GET(self):
            if self.path == '/healthz':
                gs = gateway_state()
                # If we expected a gateway (token + lib present) and it's not
                # online, fail healthcheck so docker / orchestration sees it.
                ok = not (gs["expected"] and not gs["online"])
                code = 200 if ok else 503
                return self._json(code, {'ok': ok, 'db': db_path, 'gateway': gs})
            return self._json(404, {'ok': False})

    return Handler


def serve(db_path, bind, port):
    handler = make_handler(db_path)
    srv = HTTPServer((bind, port), handler)
    sys.stdout.write(f"[http] listening on {bind}:{port}, db={db_path}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("[http] stopped\n")
