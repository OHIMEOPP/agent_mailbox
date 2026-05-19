"""Tiny HTTP bridge: receive Discord-sourced messages from node-red and INSERT
them into the shared mailbox SQLite so agent watchers wake up.

Why a separate process and not direct SQLite write from node-red:
- node-red runs in `discordBot` container; mailbox.db is on host filesystem
- node-red CAN reach host via `host.docker.internal` (verified)
- mounting the DB into container would require docker-compose change + restart
- a tiny stdlib http.server on 127.0.0.1 sidesteps both — minimal moving parts

Routing convention:
- All Discord-sourced messages go to `wiki` by default (wiki is monitor; can relay
  to koatag / koatag-frontend via standard agent mailbox)
- POST body can override `to_name` for direct routing (e.g., user types
  `@koatag fix the X` → bridge can parse + route)

Usage:
    py mailbox-discord-bridge.py                 # start listener (foreground)
    py mailbox-discord-bridge.py --port 1904     # custom port
    py mailbox-discord-bridge.py --db <path>     # custom DB

Recommended: run in background once per host boot:
    Start-Process -WindowStyle Hidden py mailbox-discord-bridge.py
or include in autostart.
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_DB = r'C:\Users\User\.claude\mailbox\mailbox.db'
DEFAULT_PORT = 1904

# Force UTF-8 on stdout/stderr (Windows console default is cp950)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)


import urllib.request

# When the bridge runs in the same Docker network as the node-red container,
# host-port (1901) is irrelevant — talk to the service directly on its
# internal port. Override via env for that case:
#   NOTIFY_URL=http://nodered:1880/agent-notify
NOTIFY_URL = os.environ.get('NOTIFY_URL', 'http://localhost:1901/agent-notify')
OFFLINE_THRESHOLD_SECONDS = 300  # 5 min — if no read activity in this window, treat as offline

# The "trusted root user" — DMs from this Discord username are routed as user
# input (default to wiki, @prefix overrides). Strangers (anyone else) go through
# the whitelist gate. Lowercase-normalized for case-insensitive comparison.
TRUSTED_USER = os.environ.get('TRUSTED_DISCORD_USER', 'ohimeopp').lower()

# Whitelist + pending table lives in a separate SQLite file from messages.db
# (different concerns; keeps bind-mount surface small for stranger-side tooling)
WHITELIST_DB = os.environ.get('WHITELIST_DB', '/data/whitelist.db')

# === Discord outbound (REST API; coexists with node-red gateway) ============
# Set DISCORD_BOT_TOKEN to enable the bridge's own /agent-notify endpoint.
# Same bot token can be used in parallel with node-red because Discord's
# single-connection limit applies to the GATEWAY websocket only — REST API
# (which we use here) is stateless and tolerates multiple concurrent clients.
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DISCORD_API_BASE = os.environ.get('DISCORD_API_BASE', 'https://discord.com/api/v10')
# Default DM channel for the trusted user (ohimeopp). Override via env.
DISCORD_DEFAULT_CHANNEL = os.environ.get('DISCORD_DEFAULT_CHANNEL', '1284065900659740773')
# status -> icon mapping; identical to node-red /agent-notify flow.
NOTIFY_ICON = {'done': '✅', 'fail': '❌', 'warn': '⚠️', 'info': '📋'}


def _agent_recently_active(db_path, agent_name, within_seconds):
    """Return True if agent's watcher has written a heartbeat in the window.
    Watcher updates peers.last_seen_at on each 5s tick — this is the real
    'agent alive' signal (mark_read is lumpy, agent may sit idle waiting).
    Falls back to checking message read_at for backward compat if peers
    row is missing."""
    try:
        conn = sqlite3.connect(db_path)
        # Primary signal: heartbeat from watcher
        cur = conn.execute("SELECT last_seen_at FROM peers WHERE name=?", (agent_name,))
        row = cur.fetchone()
        heartbeat = row[0] if row else None
        # Fallback signal (legacy): most recent mark_read
        cur = conn.execute(
            "SELECT MAX(read_at) FROM messages "
            "WHERE to_name=? AND read_at IS NOT NULL",
            (agent_name,),
        )
        last_read = cur.fetchone()[0]
        conn.close()
        latest = max(filter(None, [heartbeat, last_read]), default=None)
        if not latest:
            return False
        import datetime
        last = datetime.datetime.fromisoformat(latest.rstrip('Z'))
        now = datetime.datetime.utcnow()
        return (now - last).total_seconds() < within_seconds
    except Exception:
        return False


def _notify_offline(msg_id, to_name):
    """Send Discord DM telling user the agent is offline + msg queued."""
    try:
        body = {
            'agent': to_name,
            'task': '⏸ offline，訊息已 queue',
            'status': 'warn',
            'detail': f'msg #{msg_id} 已存進 mailbox，下次 {to_name} session 啟動會處理。'
                      f'若急可直接開 Claude Code 進對應 working dir。',
        }
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8'},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stdout.write(f"[bridge] offline-notify fail: {e}\n")


def _notify_stranger_pending(username, preview):
    """Tell user (via wiki Discord DM) that an unknown person DMed; await allow/deny."""
    try:
        body = {
            'agent': 'wiki',
            'task': f'👤 stranger DM: {username}',
            'status': 'warn',
            'detail': f'username: {username}\n預覽 (200字): {preview[:200]}\n\n核可指令（DM 給 wiki）：\n- 「allow {username}」  → promote pending DMs to stranger-conv\n- 「deny {username}」   → discard pending DMs',
        }
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8'},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stdout.write(f"[bridge] stranger-notify fail: {e}\n")


def _is_whitelisted(username):
    try:
        conn = sqlite3.connect(WHITELIST_DB)
        row = conn.execute("SELECT 1 FROM whitelist WHERE discord_username=?", (username.lower(),)).fetchone()
        conn.close()
        return bool(row)
    except sqlite3.Error:
        # If whitelist DB doesn't exist yet, no one is whitelisted (fail-closed).
        return False


def _queue_pending(username, body_text, author_id=None, channel=None):
    """Insert into pending table; returns new pending id (or None on error)."""
    try:
        conn = sqlite3.connect(WHITELIST_DB)
        # ensure schema exists (idempotent) — channel column added 2026-05-15
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS whitelist (
            discord_username TEXT PRIMARY KEY,
            discord_id       TEXT,
            approved_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            approved_by      TEXT NOT NULL DEFAULT 'ohimeopp',
            note             TEXT
        );
        CREATE TABLE IF NOT EXISTS pending (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_username TEXT NOT NULL,
            discord_id       TEXT,
            discord_channel  TEXT,
            body             TEXT NOT NULL,
            received_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """)
        # Backward-compat: add columns to existing pending table if missing
        for col in ('discord_id TEXT', 'discord_channel TEXT'):
            try:
                conn.execute(f"ALTER TABLE pending ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already exists
        cur = conn.execute(
            "INSERT INTO pending (discord_username, discord_id, discord_channel, body) "
            "VALUES (?, ?, ?, ?)",
            (username.lower(), author_id, channel, body_text),
        )
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        return pid
    except sqlite3.Error as e:
        sys.stdout.write(f"[bridge] pending-queue fail: {e}\n")
        return None


def _approve_user(username, mailbox_db):
    """Add user to whitelist + move their pending DMs to stranger-conv mailbox.

    Returns (promoted_count, error_str_or_None).
    """
    username = username.lower()
    wl = None
    mb = None
    try:
        wl = sqlite3.connect(WHITELIST_DB, timeout=5.0)
        wl.execute("PRAGMA busy_timeout = 5000")
        wl.execute(
            "INSERT OR IGNORE INTO whitelist (discord_username) VALUES (?)",
            (username,),
        )
        pending = wl.execute(
            "SELECT discord_username, body, received_at, discord_channel FROM pending "
            "WHERE discord_username=? ORDER BY id",
            (username,),
        ).fetchall()
        if pending:
            mb = sqlite3.connect(mailbox_db, timeout=5.0)
            mb.execute("PRAGMA busy_timeout = 5000")
            for uname, body_text, recv_at, ch in pending:
                # Embed channel in from_name so stranger-conv can reply without DB lookup
                fname = f"user-discord ({uname}) ch={ch}" if ch else f"user-discord ({uname})"
                mb.execute(
                    "INSERT INTO messages (from_name, to_name, body, sent_at) "
                    "VALUES (?, ?, ?, ?)",
                    (fname, "stranger-conv", body_text, recv_at),
                )
            mb.commit()
        wl.execute("DELETE FROM pending WHERE discord_username=?", (username,))
        wl.commit()
        return (len(pending), None)
    except sqlite3.Error as e:
        return (0, str(e))
    finally:
        if mb is not None:
            try: mb.close()
            except Exception: pass
        if wl is not None:
            try: wl.close()
            except Exception: pass


def _deny_user(username):
    """Discard pending DMs for user; whitelist unchanged. Returns (discarded_count, err)."""
    username = username.lower()
    wl = None
    try:
        wl = sqlite3.connect(WHITELIST_DB, timeout=5.0)
        wl.execute("PRAGMA busy_timeout = 5000")
        cur = wl.execute(
            "DELETE FROM pending WHERE discord_username=?", (username,)
        )
        n = cur.rowcount
        wl.commit()
        return (n, None)
    except sqlite3.Error as e:
        return (0, str(e))
    finally:
        if wl is not None:
            try: wl.close()
            except Exception: pass


def _notify_command_result(action, target, count, err):
    """Tell user via Discord that their allow/deny command ran."""
    try:
        if err:
            status = 'fail'
            task = f'❌ {action} {target} failed'
            detail = f'錯誤: {err}'
        elif action == 'allow':
            status = 'done'
            task = f'✅ allow {target}'
            detail = (f'已加入白名單；{count} 條 pending DM 已搬到 stranger-conv mailbox' if count
                      else f'已加入白名單；無 pending DM 可搬')
        else:  # deny
            status = 'done'
            task = f'✅ deny {target}'
            detail = (f'丟掉 {count} 條 pending DM；白名單不變' if count
                      else f'{target} 沒有 pending DM 可丟')
        body = {'agent': 'wiki', 'task': task, 'status': status, 'detail': detail}
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8'},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stdout.write(f"[bridge] cmd-notify fail: {e}\n")


import re
ALLOW_DENY_RE = re.compile(r'^(allow|deny)\s+(\S+)\s*$', re.IGNORECASE)


def _format_notify_message(agent, task, status, detail):
    """Format payload identically to node-red /agent-notify flow.
    Discord renders:
        {icon} **[{agent}]** {task}
        {detail}
    Multi-line detail is preserved; blank task is allowed.
    """
    icon = NOTIFY_ICON.get((status or 'info').lower(), NOTIFY_ICON['info'])
    head = f"{icon} **[{agent}]**"
    if task:
        head += f" {task}"
    if detail:
        return f"{head}\n{detail}"
    return head


def _discord_send_dm(channel_id, content):
    """POST to Discord REST API, no gateway connection needed.
    Returns (ok: bool, status_code: int, body: str).
    """
    if not DISCORD_BOT_TOKEN:
        return (False, 0, 'no_token')
    if not channel_id:
        return (False, 0, 'no_channel')
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    payload = json.dumps({'content': content}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        method='POST',
        headers={
            'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'mailbox-bridge-py/1.0',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (True, resp.status, resp.read().decode('utf-8', 'replace')[:200])
    except urllib.error.HTTPError as e:
        return (False, e.code, e.read().decode('utf-8', 'replace')[:200])
    except Exception as e:
        return (False, 0, f'{type(e).__name__}: {e}')


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stdout.write(f"[bridge] {self.address_string()} - {fmt % args}\n")

        def _json(self, code, payload):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

        def _read_json_body(self):
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length).decode('utf-8')
            return json.loads(raw)

        def _handle_agent_notify(self):
            """Python equivalent of node-red /agent-notify, using Discord REST.

            Request JSON (same schema as node-red endpoint):
                agent:   str  (required, instance name, e.g. "wiki")
                task:    str  (optional, short title)
                status:  str  (info|done|fail|warn; defaults to info)
                detail:  str  (optional, body text)
                channel: str  (optional, Discord channel_id; defaults to
                              DISCORD_DEFAULT_CHANNEL = trusted user's DM)

            Returns rendered DM as text (matches node-red response shape so
            agents using existing schema-check logic still get the same hint).
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

            content = _format_notify_message(agent, task, status, detail)
            ok, code, resp_body = _discord_send_dm(channel, content)

            if ok:
                sys.stdout.write(f"[bridge] agent-notify OK agent={agent} ch={channel} task={task[:40]!r}\n")
                # Plain-text response so existing client code (which checks for
                # "<icon> **[wiki]**" prefix) keeps working without parsing JSON.
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
                return

            sys.stdout.write(f"[bridge] agent-notify FAIL agent={agent} code={code} body={resp_body!r}\n")
            return self._json(502, {'ok': False, 'error': 'discord_send_fail',
                                    'discord_status': code, 'discord_body': resp_body})

        def do_POST(self):
            if self.path == '/agent-notify':
                return self._handle_agent_notify()
            if self.path != '/from-discord':
                return self._json(404, {'ok': False, 'error': 'unknown_path'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length).decode('utf-8')
                body = json.loads(raw)
            except Exception as e:
                return self._json(400, {'ok': False, 'error': f'parse_fail: {e}'})

            content = (body.get('content') or '').strip()
            if not content:
                return self._json(400, {'ok': False, 'error': 'empty_content'})

            author = body.get('author') or 'discord-user'
            author_id = body.get('author_id') or None  # Phase 3: stable across username changes
            channel = body.get('channel') or ''        # Discord DM channel id (needed for replies)
            is_trusted = author.lower() == TRUSTED_USER
            to_name = body.get('to_name') or 'wiki'

            # === Stranger gate: anyone except TRUSTED_USER goes through whitelist ===
            if not is_trusted:
                if _is_whitelisted(author):
                    # Approved — route to stranger-conv (override any to_name + ignore @prefix
                    # so strangers can't spoof routing to wiki / koatag / koatag-frontend)
                    to_name = 'stranger-conv'
                else:
                    # Not approved — queue in pending table, notify wiki, do NOT write mailbox
                    pid = _queue_pending(author, content, author_id, channel)
                    sys.stdout.write(f"[bridge] stranger DM queued pending #{pid} from {author!r} (id={author_id} ch={channel}): {content[:80]!r}\n")
                    _notify_stranger_pending(author, content)
                    return self._json(202, {'ok': True, 'pending': pid, 'note': 'awaiting approval'})

            # === Trusted-user inline commands (intercept before mailbox write) ===
            # `allow <username>` / `deny <username>` from TRUSTED → run whitelist action,
            # ack via Discord, DO NOT write to mailbox.
            if is_trusted:
                m = ALLOW_DENY_RE.match(content)
                if m:
                    action, target = m.group(1).lower(), m.group(2)
                    if action == 'allow':
                        count, err = _approve_user(target, db_path)
                    else:
                        count, err = _deny_user(target)
                    sys.stdout.write(f"[bridge] cmd {action} {target} → count={count} err={err}\n")
                    _notify_command_result(action, target, count, err)
                    return self._json(200, {
                        'ok': err is None, 'cmd': action, 'target': target,
                        'count': count, 'error': err,
                    })

            # === Trusted-user @prefix routing override ===
            # Only honored for TRUSTED_USER; strangers' @prefix is ignored above.
            if is_trusted:
                for prefix in ('@koatag-frontend ', '@koatag ', '@stranger-conv '):
                    if content.lower().startswith(prefix.lower()):
                        to_name = prefix[1:-1]
                        content = content[len(prefix):]
                        break

            # Stranger-conv replies need to know the Discord channel to send back to.
            # Embed it in from_name as "user-discord (X) ch=12345" so the receiving agent
            # can parse it without a DB lookup. Trusted user (you, ohimeopp) replies via
            # wiki always go to the hardcoded user-DM channel, so no need to embed there.
            if to_name == 'stranger-conv' and channel:
                from_name = f'user-discord ({author}) ch={channel}'
            elif author != 'discord-user':
                from_name = f'user-discord ({author})'
            else:
                from_name = 'user-discord'

            try:
                conn = sqlite3.connect(db_path)
                cur = conn.execute(
                    'INSERT INTO messages (from_name, to_name, body) VALUES (?, ?, ?)',
                    (from_name, to_name, content),
                )
                mid = cur.lastrowid
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                return self._json(500, {'ok': False, 'error': f'db: {e}'})

            sys.stdout.write(f"[bridge] msg #{mid} {from_name} -> {to_name} (ch={channel}): {content[:80]!r}\n")

            # Offline-detection: if target agent hasn't read mail recently, notify Discord
            if not _agent_recently_active(db_path, to_name, OFFLINE_THRESHOLD_SECONDS):
                sys.stdout.write(f"[bridge] {to_name} appears offline (>{OFFLINE_THRESHOLD_SECONDS}s), notifying user\n")
                _notify_offline(mid, to_name)

            return self._json(200, {'ok': True, 'id': mid, 'to': to_name})

        def do_GET(self):
            if self.path == '/healthz':
                return self._json(200, {'ok': True, 'db': db_path})
            return self._json(404, {'ok': False})

    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=DEFAULT_PORT)
    p.add_argument('--db', default=DEFAULT_DB)
    p.add_argument('--bind', default='0.0.0.0',
                   help="bind addr; 0.0.0.0 lets Docker reach via host.docker.internal "
                        "(127.0.0.1 only would block container)")
    args = p.parse_args()

    if not os.path.exists(args.db):
        sys.stderr.write(f"[bridge] FATAL: mailbox db not found: {args.db}\n")
        return 1

    handler = make_handler(args.db)
    srv = HTTPServer((args.bind, args.port), handler)
    sys.stdout.write(f"[bridge] listening on {args.bind}:{args.port}, db={args.db}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("[bridge] stopped\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
