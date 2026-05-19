"""HTTP server on :1904 — exposes:

- POST /from-discord   legacy webhook for node-red's discordMessage flow
- POST /agent-notify   Python equivalent of node-red /agent-notify (REST DM)
- GET  /healthz        liveness ping

Body of both POST endpoints feeds through the same inbound / notify modules.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import DISCORD_DEFAULT_CHANNEL
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

        def do_POST(self):
            if self.path == '/agent-notify':
                return self._handle_agent_notify()
            if self.path == '/from-discord':
                return self._handle_from_discord()
            return self._json(404, {'ok': False, 'error': 'unknown_path'})

        def do_GET(self):
            if self.path == '/healthz':
                return self._json(200, {'ok': True, 'db': db_path})
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
