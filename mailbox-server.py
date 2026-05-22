"""mailbox-server — LAN/VPN REST API + SSE watch for cross-device agent mailbox.

Architecture: hub-and-spoke
  - One designated host runs this server, owns the SQLite mailbox.db (single writer)
  - Other devices (laptop / VM / future mobile) connect via HTTP over LAN or VPN
  - Tailscale / WireGuard adds nothing to the protocol — just changes bind/connect IP

Endpoints:
  GET  /health                   → "ok" (no auth)
  POST /send                     → JSON {from, to, body} → {id, sent_at}
  POST /send-file                → multipart (payload_json + files[N]) → {id, sent_at, attachments:[...]}
  GET  /attachment/<id>          → blob bytes + Content-Disposition
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
import hashlib
import http.server
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import urllib.parse

DEFAULT_PORT = 1905
DEFAULT_DB = pathlib.Path.home() / ".claude" / "mailbox" / "mailbox.db"
WATCH_POLL_INTERVAL = 2.0  # seconds between SQLite polls for SSE watch
WATCH_HEARTBEAT_INTERVAL = 30.0  # SSE comment heartbeat to keep conn alive

MAX_SINGLE_FILE = 100 * 1024 * 1024   # 100 MB per file
MAX_TOTAL_PAYLOAD = 500 * 1024 * 1024  # 500 MB per request
MAX_FILES_PER_MSG = 32                 # sanity cap on count


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
            read_at TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_to_unread
            ON messages(to_name, read_at);
        CREATE TABLE IF NOT EXISTS peers (
            name TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            filename TEXT NOT NULL,
            mime TEXT,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_attach_msg ON attachments(message_id);
        CREATE INDEX IF NOT EXISTS idx_attach_sha ON attachments(sha256);
    """)
    # Forward-compat: if table existed before this column was added, add it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "has_attachments" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN has_attachments INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def blob_path(attachments_dir: pathlib.Path, sha256: str) -> pathlib.Path:
    """Content-addressed blob path: <dir>/<sha[:2]>/<sha>."""
    return attachments_dir / sha256[:2] / sha256


def write_blob_atomic(attachments_dir: pathlib.Path, data: bytes) -> tuple[str, int]:
    """Write data to content-addressed path atomically; returns (sha256, size).
    Dedup: if blob exists already, return without rewriting.
    """
    sha = hashlib.sha256(data).hexdigest()
    target = blob_path(attachments_dir, sha)
    if target.exists():
        return sha, len(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)  # atomic on same filesystem
    return sha, len(data)


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


# ---------- multipart parser (minimal, stdlib-only) ----------

_BOUNDARY_RE = re.compile(r'boundary=([^;]+)', re.IGNORECASE)
_NAME_RE = re.compile(r'name="([^"]+)"')
_FILENAME_RE = re.compile(r'filename="([^"]*)"')


def parse_multipart(body: bytes, content_type: str) -> dict:
    """Parse multipart/form-data body.

    Returns dict mapping field-name → {"filename": str|None, "mime": str, "data": bytes}.
    Format assumption: client uses RFC 7578 multipart with \\r\\n line endings.
    Boundary must not appear inside binary parts (caller is trusted; our CLI
    uses random hex boundary).
    """
    m = _BOUNDARY_RE.search(content_type)
    if not m:
        raise ValueError("multipart Content-Type missing boundary parameter")
    boundary = m.group(1).strip().strip('"')
    delim = b"--" + boundary.encode("ascii")

    # Split body on the delimiter. First chunk is preamble (usually empty),
    # last chunk is the closing "--\r\n" suffix.
    chunks = body.split(delim)
    result: dict = {}
    for chunk in chunks[1:-1]:
        # Each chunk starts with \r\n (after the delimiter) and ends with \r\n
        # (before the next delimiter). Strip them.
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        # Split headers from body at first \r\n\r\n
        sep = chunk.find(b"\r\n\r\n")
        if sep < 0:
            continue
        header_block = chunk[:sep].decode("utf-8", errors="replace")
        data = chunk[sep + 4:]

        headers: dict[str, str] = {}
        for line in header_block.split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        cd = headers.get("content-disposition", "")
        nm = _NAME_RE.search(cd)
        if not nm:
            continue
        name = nm.group(1)
        fnm = _FILENAME_RE.search(cd)
        result[name] = {
            "filename": fnm.group(1) if fnm else None,
            "mime": headers.get("content-type", ""),
            "data": data,
        }
    return result


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mailbox-server/0.2"

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

    def _send_bytes(self, status: int, ctype: str, body: bytes, extra_headers: dict | None = None):
        """Send raw bytes WITHOUT charset suffix — for binary blob downloads."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
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
            sql = ("SELECT id, from_name, to_name, body, sent_at, read_at, has_attachments "
                   "FROM messages WHERE to_name=?")
            args: list = [name]
            if unread_only:
                sql += " AND read_at IS NULL"
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            conn = db_connect(srv.db_path)
            try:
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
                # Attach attachment metadata for messages with has_attachments=1
                for r in rows:
                    if r.get("has_attachments"):
                        atts = conn.execute(
                            "SELECT id, filename, mime, size, sha256 "
                            "FROM attachments WHERE message_id=? ORDER BY id",
                            (r["id"],),
                        ).fetchall()
                        r["attachments"] = [dict(a) for a in atts]
                    else:
                        r["attachments"] = []
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

        # /attachment/<id>
        if path.startswith("/attachment/"):
            return self._serve_attachment(path[len("/attachment/"):])

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path, _, _ = self.path.partition("?")

        if not self._check_auth():
            return self._json(401, {"error": "missing or invalid bearer token"})

        srv = self.server

        if path == "/send-file":
            return self._handle_send_file()

        # All other POSTs read JSON bodies.
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

    def _handle_send_file(self):
        """POST /send-file — multipart with payload_json + files[N]."""
        srv = self.server
        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data"):
            return self._json(400, {"error": "Content-Type must be multipart/form-data"})

        # Size guard before allocating memory
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "invalid Content-Length"})
        if length <= 0:
            return self._json(400, {"error": "missing Content-Length"})
        if length > MAX_TOTAL_PAYLOAD:
            return self._json(
                413,
                {"error": f"payload too large: {length} > {MAX_TOTAL_PAYLOAD}"},
            )

        body = self.rfile.read(length)
        try:
            parts = parse_multipart(body, ctype)
        except Exception as e:
            return self._json(400, {"error": f"multipart parse failed: {e}"})

        if "payload_json" not in parts:
            return self._json(400, {"error": "missing payload_json part"})
        try:
            payload = json.loads(parts["payload_json"]["data"].decode("utf-8"))
        except Exception as e:
            return self._json(400, {"error": f"payload_json invalid: {e}"})

        for k in ("from", "to", "body"):
            if k not in payload or not isinstance(payload[k], str):
                return self._json(400, {"error": f"payload missing or non-string: {k}"})

        # Collect file parts: any field name like files[0], files[1], ... or "files"
        file_parts: list[tuple[str, dict]] = []
        for name, part in parts.items():
            if name == "payload_json":
                continue
            if not name.startswith("files"):
                continue
            file_parts.append((name, part))
        if not file_parts:
            return self._json(400, {"error": "no file parts found (expected files[0], files[1], ...)"})
        if len(file_parts) > MAX_FILES_PER_MSG:
            return self._json(
                400,
                {"error": f"too many files: {len(file_parts)} > {MAX_FILES_PER_MSG}"},
            )

        # Per-file size check
        total = 0
        for _, part in file_parts:
            sz = len(part["data"])
            if sz > MAX_SINGLE_FILE:
                return self._json(
                    413,
                    {"error": f"file '{part.get('filename')}' exceeds {MAX_SINGLE_FILE} bytes (got {sz})"},
                )
            total += sz
        if total > MAX_TOTAL_PAYLOAD:
            return self._json(
                413,
                {"error": f"total attachment size {total} > {MAX_TOTAL_PAYLOAD}"},
            )

        # Write blobs (content-addressed dedup), then DB rows.
        written: list[dict] = []
        for _, part in file_parts:
            filename = part.get("filename") or "unnamed"
            mime = part.get("mime") or "application/octet-stream"
            sha, size = write_blob_atomic(srv.attachments_dir, part["data"])
            written.append({"filename": filename, "mime": mime, "size": size, "sha256": sha})

        conn = db_connect(srv.db_path)
        try:
            row = conn.execute(
                "INSERT INTO messages(from_name, to_name, body, has_attachments) "
                "VALUES(?, ?, ?, 1) RETURNING id, sent_at",
                (payload["from"], payload["to"], payload["body"]),
            ).fetchone()
            msg_id = row["id"]
            attach_rows = []
            for w in written:
                a = conn.execute(
                    "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
                    "VALUES(?, ?, ?, ?, ?) RETURNING id",
                    (msg_id, w["filename"], w["mime"], w["size"], w["sha256"]),
                ).fetchone()
                attach_rows.append({
                    "id": a["id"],
                    "filename": w["filename"],
                    "mime": w["mime"],
                    "size": w["size"],
                    "sha256": w["sha256"],
                })
            conn.execute(
                "INSERT INTO peers(name, last_seen_at) "
                "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (payload["from"],),
            )
            conn.commit()
        finally:
            conn.close()

        return self._json(200, {
            "id": msg_id,
            "sent_at": row["sent_at"],
            "attachments": attach_rows,
        })

    def _serve_attachment(self, raw_id: str):
        """GET /attachment/<id> — stream blob by attachment id."""
        srv = self.server
        try:
            attach_id = int(raw_id)
        except ValueError:
            return self._json(400, {"error": "attachment id must be integer"})

        conn = db_connect(srv.db_path)
        try:
            row = conn.execute(
                "SELECT filename, mime, size, sha256 FROM attachments WHERE id=?",
                (attach_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return self._json(404, {"error": "attachment not found"})

        path = blob_path(srv.attachments_dir, row["sha256"])
        if not path.exists():
            return self._json(500, {"error": f"blob missing at {path}"})

        data = path.read_bytes()
        # Content-Disposition with RFC 5987 for non-ASCII filenames
        filename = row["filename"] or "attachment"
        ascii_fallback = filename.encode("ascii", errors="replace").decode("ascii")
        utf8_pct = urllib.parse.quote(filename, safe="")
        cd = f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{utf8_pct}'
        mime = row["mime"] or "application/octet-stream"
        return self._send_bytes(200, mime, data, extra_headers={
            "Content-Disposition": cd,
            "X-Mailbox-Sha256": row["sha256"],
        })

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
                        "SELECT id, from_name, to_name, body, sent_at, has_attachments "
                        "FROM messages WHERE to_name=? AND id>? ORDER BY id ASC",
                        (name, last_id),
                    ).fetchall()
                    # Pre-fetch attachments per message that has them
                    attachments_by_msg: dict[int, list] = {}
                    msg_ids_with_atts = [r["id"] for r in rows if r["has_attachments"]]
                    if msg_ids_with_atts:
                        placeholders = ",".join("?" for _ in msg_ids_with_atts)
                        att_rows = conn.execute(
                            f"SELECT message_id, id, filename, mime, size "
                            f"FROM attachments WHERE message_id IN ({placeholders}) "
                            f"ORDER BY message_id, id",
                            msg_ids_with_atts,
                        ).fetchall()
                        for a in att_rows:
                            attachments_by_msg.setdefault(a["message_id"], []).append({
                                "id": a["id"], "filename": a["filename"],
                                "mime": a["mime"], "size": a["size"],
                            })
                finally:
                    conn.close()
                for r in rows:
                    d = dict(r)
                    d["attachments"] = attachments_by_msg.get(r["id"], [])
                    payload = json.dumps(d, ensure_ascii=False)
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
    ap.add_argument("--attachments-dir", type=pathlib.Path, default=None,
                    help="blob storage directory (default: <db parent>/attachments)")
    args = ap.parse_args()

    # Derive attachments dir from db parent if not explicitly set. This is the
    # only way to make docker mounts work without extra config — inside the
    # container Path.home() is /root but --db is /data/mailbox.db, so attachments
    # land at /data/attachments which maps to the host mount.
    if args.attachments_dir is None:
        args.attachments_dir = args.db.parent / "attachments"

    token = os.environ.get("CLAUDE_MAILBOX_TOKEN", "").strip()
    if not token:
        sys.exit("CLAUDE_MAILBOX_TOKEN env var required. "
                 "Generate one: py -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    if len(token) < 16:
        sys.exit("CLAUDE_MAILBOX_TOKEN too short (min 16 chars)")

    if not args.db.exists():
        print(f"[mailbox-server] db missing at {args.db}, initializing schema", file=sys.stderr)
    db_init(args.db)
    args.attachments_dir.mkdir(parents=True, exist_ok=True)

    httpd = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.token = token
    httpd.db_path = args.db
    httpd.attachments_dir = args.attachments_dir
    print(f"[mailbox-server] listening on http://{args.host}:{args.port}  db={args.db}",
          file=sys.stderr)
    print(f"[mailbox-server] attachments dir: {args.attachments_dir}", file=sys.stderr)
    print(f"[mailbox-server] bearer token: {token[:6]}... (length {len(token)})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mailbox-server] bye", file=sys.stderr)


if __name__ == "__main__":
    main()
