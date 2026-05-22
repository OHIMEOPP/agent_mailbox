"""mailbox-server — LAN/VPN REST API + SSE watch for cross-device agent mailbox.

Architecture: hub-and-spoke
  - One designated host runs this server, owns the SQLite mailbox.db (single writer)
  - Other devices (laptop / VM / future mobile) connect via HTTP over LAN or VPN
  - Tailscale / WireGuard adds nothing to the protocol — just changes bind/connect IP

Endpoints:
  GET  /health                   → "ok" (no auth)
  POST /send                     → JSON {from, to, body} → {id, sent_at}
  GET  /inbox?name=X&unread=1    → list of messages (unread=0/1, limit=50)
  POST /mark_read                → JSON {ids:[...]} → {count}
  GET  /peers                    → list of known peers + last_seen_at
  GET  /watch?name=X             → SSE stream of new mail to X (long-poll)

Auth:
  All endpoints (except /health) require `Authorization: Bearer <token>` header.
  Server reads `CLAUDE_MAILBOX_TOKEN` env var. If unset, server refuses to start.

Run:
  CLAUDE_MAILBOX_TOKEN=xxx py mailbox-server.py
  CLAUDE_MAILBOX_TOKEN=xxx py mailbox-server.py --host 0.0.0.0 --port 1905
  CLAUDE_MAILBOX_TOKEN=xxx py mailbox-server.py --db /path/mailbox.db

Cross-machine deployment:
  Hub:     bind 0.0.0.0:1905 (or only tailscale0 IP)
  Spoke:   peer agent uses mailbox-watch.py --remote http://hub-ip:1905 --token xxx

stdlib only.
"""
import argparse
import http.server
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
import urllib.parse

DEFAULT_PORT = 1905
DEFAULT_DB = pathlib.Path.home() / ".claude" / "mailbox" / "mailbox.db"
WATCH_POLL_INTERVAL = 2.0  # seconds between SQLite polls for SSE watch
WATCH_HEARTBEAT_INTERVAL = 30.0  # SSE comment heartbeat to keep conn alive


def db_connect(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Pin DELETE journal mode every connection — defense against Docker Desktop
    # WAL/mmap "disk I/O error" pitfall (cf. server.py same fix). Other process
    # may have left WAL on, but every fresh connection enforces DELETE here.
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def db_init(path: pathlib.Path):
    """Create tables if missing — mirrors server.py schema."""
    conn = db_connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            read_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_to_unread
            ON messages(to_name, read_at);
        CREATE TABLE IF NOT EXISTS peers (
            name TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def heartbeat_peer(db_path: pathlib.Path, name: str):
    """Mark a peer as alive — same protocol watcher uses."""
    conn = db_connect(db_path)
    try:
        conn.execute(
            "INSERT INTO peers(name, last_seen_at) VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            " ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (name,),
        )
        conn.commit()
    finally:
        conn.close()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mailbox-server/0.1"

    def _send(self, status: int, ctype: str, body: bytes, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, "application/json",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth[7:].strip() == self.server.token

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _parse_query(self):
        _, _, query = self.path.partition("?")
        params: dict[str, list[str]] = {}
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
            params.setdefault(k, []).append(v)
        return params

    def do_GET(self):
        path, _, _ = self.path.partition("?")
        params = self._parse_query()

        def first(k, default=""):
            return params[k][0] if k in params and params[k] else default

        if path == "/health":
            return self._send(200, "text/plain", b"ok\n")

        if not self._check_auth():
            return self._json(401, {"error": "missing or invalid bearer token"})

        srv = self.server

        if path == "/inbox":
            name = first("name").strip()
            if not name:
                return self._json(400, {"error": "missing name"})
            unread_only = first("unread", "1") in ("1", "true", "yes")
            limit = int(first("limit", "50"))
            sql = ("SELECT id, from_name, to_name, body, sent_at, read_at "
                   "FROM messages WHERE to_name=?")
            args: list = [name]
            if unread_only:
                sql += " AND read_at IS NULL"
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            conn = db_connect(srv.db_path)
            try:
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()
            return self._json(200, {"messages": rows})

        if path == "/peers":
            conn = db_connect(srv.db_path)
            try:
                rows = [dict(r) for r in
                        conn.execute("SELECT name, last_seen_at FROM peers ORDER BY last_seen_at DESC").fetchall()]
            finally:
                conn.close()
            return self._json(200, {"peers": rows})

        if path == "/watch":
            return self._sse_watch(first("name").strip(), first("since"))

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path, _, _ = self.path.partition("?")

        if not self._check_auth():
            return self._json(401, {"error": "missing or invalid bearer token"})

        srv = self.server

        try:
            payload = self._read_body_json()
        except Exception as e:
            return self._json(400, {"error": f"invalid json: {e}"})

        if path == "/send":
            for k in ("from", "to", "body"):
                if k not in payload or not isinstance(payload[k], str):
                    return self._json(400, {"error": f"missing or non-string field: {k}"})
            conn = db_connect(srv.db_path)
            try:
                row = conn.execute(
                    "INSERT INTO messages(from_name, to_name, body) VALUES(?, ?, ?) "
                    "RETURNING id, sent_at",
                    (payload["from"], payload["to"], payload["body"]),
                ).fetchone()
                conn.execute(
                    "INSERT INTO peers(name, last_seen_at) "
                    "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (payload["from"],),
                )
                conn.commit()
            finally:
                conn.close()
            return self._json(200, {"id": row["id"], "sent_at": row["sent_at"]})

        if path == "/mark_read":
            ids = payload.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                return self._json(400, {"error": "ids must be list[int]"})
            if not ids:
                return self._json(200, {"count": 0})
            placeholders = ",".join("?" for _ in ids)
            conn = db_connect(srv.db_path)
            try:
                cur = conn.execute(
                    f"UPDATE messages SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    f"WHERE id IN ({placeholders}) AND read_at IS NULL",
                    ids,
                )
                conn.commit()
                count = cur.rowcount
            finally:
                conn.close()
            return self._json(200, {"count": count})

        return self._json(404, {"error": "not found"})

    def _sse_watch(self, name: str, since: str):
        if not name:
            return self._json(400, {"error": "missing name"})
        try:
            since_id = int(since) if since else 0
        except ValueError:
            return self._json(400, {"error": "since must be integer"})

        srv = self.server
        # Establish baseline: latest message id for this recipient
        conn = db_connect(srv.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE to_name=?",
                (name,),
            ).fetchone()
            last_id = max(since_id, row[0])
        finally:
            conn.close()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # for any reverse-proxy
        self.end_headers()

        try:
            self.wfile.write(f": baseline last_id={last_id}\n\n".encode("utf-8"))
            self.wfile.flush()
            last_heartbeat = time.monotonic()
            while True:
                time.sleep(WATCH_POLL_INTERVAL)
                heartbeat_peer(srv.db_path, name)
                conn = db_connect(srv.db_path)
                try:
                    rows = conn.execute(
                        "SELECT id, from_name, to_name, body, sent_at FROM messages "
                        "WHERE to_name=? AND id>? ORDER BY id ASC",
                        (name, last_id),
                    ).fetchall()
                finally:
                    conn.close()
                for r in rows:
                    payload = json.dumps(dict(r), ensure_ascii=False)
                    self.wfile.write(f"event: mail\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_id = r["id"]
                # heartbeat to keep connection alive
                now = time.monotonic()
                if now - last_heartbeat >= WATCH_HEARTBEAT_INTERVAL:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
        except (ConnectionResetError, BrokenPipeError):
            return  # client disconnected
        except Exception as e:
            try:
                self.wfile.write(f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[mailbox-server] {self.address_string()} {fmt % args}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind interface (default 0.0.0.0 = all). Use 100.x.y.z for tailscale-only.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    args = ap.parse_args()

    token = os.environ.get("CLAUDE_MAILBOX_TOKEN", "").strip()
    if not token:
        sys.exit("CLAUDE_MAILBOX_TOKEN env var required. "
                 "Generate one: py -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    if len(token) < 16:
        sys.exit("CLAUDE_MAILBOX_TOKEN too short (min 16 chars)")

    if not args.db.exists():
        print(f"[mailbox-server] db missing at {args.db}, initializing schema", file=sys.stderr)
    db_init(args.db)

    httpd = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.token = token
    httpd.db_path = args.db
    print(f"[mailbox-server] listening on http://{args.host}:{args.port}  db={args.db}",
          file=sys.stderr)
    print(f"[mailbox-server] bearer token: {token[:6]}... (length {len(token)})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mailbox-server] bye", file=sys.stderr)


if __name__ == "__main__":
    main()
