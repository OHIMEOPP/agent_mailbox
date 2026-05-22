# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0.0"]
# ///
"""
Claude Code Mailbox — Cross-instance message queue via MCP.

Each Claude Code instance spawns its own copy of this stdio MCP server.

Configuration via env vars (set in each project's .mcp.json):
  CLAUDE_MAILBOX_NAME    — this instance's identity (REQUIRED, e.g. "wiki", "koatag")
  CLAUDE_MAILBOX_DB      — local SQLite file path (default: ~/.claude/mailbox/mailbox.db)
                           IGNORED when CLAUDE_MAILBOX_REMOTE is set.
  CLAUDE_MAILBOX_REMOTE  — hub URL (e.g. http://hub-lan-ip:1905). When set, all 5
                           tools dispatch via REST to mailbox-server.py on the hub
                           instead of touching local SQLite. Use on "spoke" machines
                           that should not own a mailbox DB.
  CLAUDE_MAILBOX_TOKEN   — bearer token for the remote hub. Required if REMOTE set.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------- Configuration ----------

NAME = os.environ.get("CLAUDE_MAILBOX_NAME")
if not NAME:
    raise RuntimeError(
        "CLAUDE_MAILBOX_NAME env var must be set in your .mcp.json "
        "(e.g. 'wiki', 'koatag') so the mailbox knows your identity."
    )

REMOTE = os.environ.get("CLAUDE_MAILBOX_REMOTE", "").strip().rstrip("/") or None
TOKEN = os.environ.get("CLAUDE_MAILBOX_TOKEN", "").strip() or None

if REMOTE and not TOKEN:
    raise RuntimeError(
        "CLAUDE_MAILBOX_REMOTE is set but CLAUDE_MAILBOX_TOKEN is missing. "
        "Both required for spoke-mode."
    )

# Only resolve / init local DB when NOT in remote-mode (avoid creating ghost DB on spoke)
DB_PATH: Path | None = None
ATTACHMENTS_DIR: Path | None = None
if not REMOTE:
    DB_PATH = Path(
        os.environ.get(
            "CLAUDE_MAILBOX_DB",
            str(Path.home() / ".claude" / "mailbox" / "mailbox.db"),
        )
    )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Derive attachments dir from DB parent — matches mailbox-server.py convention
    # so local-mode and docker-mode share the same blob layout.
    ATTACHMENTS_DIR = DB_PATH.parent / "attachments"
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Remote (REST) helpers ----------

def _remote(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{REMOTE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"remote {method} {path} → HTTP {e.code}: {err_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"remote {method} {path} unreachable: {e.reason}")


def _remote_multipart(path: str, payload: dict, files: list[tuple[str, str, bytes]]) -> dict:
    """POST multipart/form-data to hub. files = [(filename, mime, bytes), ...]."""
    boundary = "----mailboxmcp" + os.urandom(8).hex()
    chunks: list[bytes] = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    chunks.append(b"Content-Type: application/json; charset=utf-8\r\n\r\n")
    chunks.append(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    chunks.append(b"\r\n")
    for i, (fname, mime, data) in enumerate(files):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n'
            .encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    url = f"{REMOTE}{path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    # Bigger timeout: 500MB over LAN at ~50MB/s ≈ 10s; allow plenty of headroom.
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"remote POST {path} → HTTP {e.code}: {err_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"remote POST {path} unreachable: {e.reason}")


def _remote_get_bytes(path: str) -> tuple[bytes, str]:
    """GET binary blob. Returns (data, server-reported sha256)."""
    url = f"{REMOTE}{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
            sha = r.headers.get("X-Mailbox-Sha256", "")
            return data, sha
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"remote GET {path} → HTTP {e.code}: {err_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"remote GET {path} unreachable: {e.reason}")


# ---------- Local DB / blob helpers ----------

def _connect() -> sqlite3.Connection:
    assert DB_PATH is not None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _init_db() -> None:
    with _connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name  TEXT NOT NULL,
                to_name    TEXT NOT NULL,
                body       TEXT NOT NULL,
                sent_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                read_at    TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                in_reply_to INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_messages_to_unread
                ON messages(to_name, read_at);

            CREATE TABLE IF NOT EXISTS peers (
                name          TEXT PRIMARY KEY,
                last_seen_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                filename TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_attach_msg ON attachments(message_id);
            CREATE INDEX IF NOT EXISTS idx_attach_sha ON attachments(sha256);
            """
        )
        # Forward-compat: idempotent column adds for existing DBs
        cols = {r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
        if "has_attachments" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN has_attachments INTEGER NOT NULL DEFAULT 0")
        if "in_reply_to" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN in_reply_to INTEGER")
        # Index after ALTER (column may have just been added); IF NOT EXISTS safe.
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to "
                  "ON messages(in_reply_to) WHERE in_reply_to IS NOT NULL")
        c.execute(
            "INSERT INTO peers(name, last_seen_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (NAME,),
        )


if not REMOTE:
    _init_db()


def _write_blob(data: bytes) -> tuple[str, int]:
    """Content-addressed blob write (local mode). Returns (sha256, size)."""
    assert ATTACHMENTS_DIR is not None
    sha = hashlib.sha256(data).hexdigest()
    target = ATTACHMENTS_DIR / sha[:2] / sha
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return sha, len(data)


def _guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ---------- MCP server & tools ----------

mcp = FastMCP("mailbox")


@mcp.tool()
def send(to: str, body: str, files: list[str] | None = None,
         in_reply_to: int | None = None) -> dict:
    """Send a message (optionally with file attachments) to another Claude Code instance.

    Args:
        to: recipient's CLAUDE_MAILBOX_NAME (e.g. "wiki", "koatag@LAPTOP-XYZ789")
        body: message text
        files: optional list of host filesystem paths to attach. Each file up to
               100 MB, total payload up to 500 MB. For folder transfer, zip
               first then attach the zip.
        in_reply_to: optional message id this message is a reply to. Used by
               mailbox-dump tree view to render conversation threads. Pass the
               `id` from a prior inbox() entry. No FK enforcement — if the
               parent was retention-pruned, the field becomes a broken chain
               (rendered as orphan in dump).

    Returns:
        {id, sent_at, from, to, in_reply_to?, attachments?: [{id, filename, mime, size, sha256}]}
    """
    if files:
        file_parts: list[tuple[str, str, bytes]] = []
        for fp in files:
            p = Path(fp)
            if not p.exists():
                raise RuntimeError(f"file not found: {fp}")
            file_parts.append((p.name, _guess_mime(p.name), p.read_bytes()))

        if REMOTE:
            body_payload: dict = {"from": NAME, "to": to, "body": body}
            if in_reply_to is not None:
                body_payload["in_reply_to"] = in_reply_to
            r = _remote_multipart(
                "/send-file",
                body_payload,
                file_parts,
            )
            return {
                "id": r["id"], "sent_at": r["sent_at"], "from": NAME, "to": to,
                "in_reply_to": in_reply_to,
                "attachments": r["attachments"],
            }

        # Local mode: write blobs then DB rows
        written: list[dict] = []
        for fname, mime, data in file_parts:
            sha, size = _write_blob(data)
            written.append({"filename": fname, "mime": mime, "size": size, "sha256": sha})
        with _connect() as c:
            row = c.execute(
                "INSERT INTO messages(from_name, to_name, body, has_attachments, in_reply_to) "
                "VALUES(?, ?, ?, 1, ?) RETURNING id, sent_at",
                (NAME, to, body, in_reply_to),
            ).fetchone()
            msg_id = row["id"]
            for w in written:
                r2 = c.execute(
                    "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
                    "VALUES(?, ?, ?, ?, ?) RETURNING id",
                    (msg_id, w["filename"], w["mime"], w["size"], w["sha256"]),
                ).fetchone()
                w["id"] = r2["id"]
        return {
            "id": msg_id, "sent_at": row["sent_at"], "from": NAME, "to": to,
            "in_reply_to": in_reply_to,
            "attachments": written,
        }

    # text-only path
    if REMOTE:
        body_payload2: dict = {"from": NAME, "to": to, "body": body}
        if in_reply_to is not None:
            body_payload2["in_reply_to"] = in_reply_to
        r = _remote("POST", "/send", body_payload2)
        return {"id": r["id"], "sent_at": r["sent_at"], "from": NAME, "to": to,
                "in_reply_to": in_reply_to}

    with _connect() as c:
        row = c.execute(
            "INSERT INTO messages(from_name, to_name, body, in_reply_to) "
            "VALUES(?, ?, ?, ?) RETURNING id, sent_at",
            (NAME, to, body, in_reply_to),
        ).fetchone()
    return {"id": row["id"], "sent_at": row["sent_at"], "from": NAME, "to": to,
            "in_reply_to": in_reply_to}


@mcp.tool()
def inbox(unread_only: bool = True, limit: int = 50) -> list[dict]:
    """Fetch messages addressed to this instance.

    Args:
        unread_only: if True (default), only return messages not yet marked read
        limit: max messages to return (default 50)

    Returns:
        List of {id, from, body, sent_at, read_at, attachments: [...]}.
        attachments is [] when message has no files; otherwise each entry is
        {id, filename, mime, size, sha256}. Use download(attachment_id, save_to)
        to fetch blob.
    """
    if REMOTE:
        unread_flag = "1" if unread_only else "0"
        r = _remote("GET", f"/inbox?name={NAME}&unread={unread_flag}&limit={limit}")
        return [
            {"id": m["id"], "from": m["from_name"], "body": m["body"],
             "sent_at": m["sent_at"], "read_at": m["read_at"],
             "in_reply_to": m.get("in_reply_to"),
             "attachments": m.get("attachments", [])}
            for m in r["messages"]
        ]

    sql = ("SELECT id, from_name, body, sent_at, read_at, has_attachments, in_reply_to "
           "FROM messages WHERE to_name = ?")
    params: list = [NAME]
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    with _connect() as c:
        rows = c.execute(sql, params).fetchall()
        out: list[dict] = []
        msg_ids_with_atts = [r["id"] for r in rows if r["has_attachments"]]
        atts_by_msg: dict[int, list] = {}
        if msg_ids_with_atts:
            placeholders = ",".join("?" * len(msg_ids_with_atts))
            for a in c.execute(
                f"SELECT message_id, id, filename, mime, size, sha256 "
                f"FROM attachments WHERE message_id IN ({placeholders}) "
                f"ORDER BY message_id, id",
                msg_ids_with_atts,
            ).fetchall():
                atts_by_msg.setdefault(a["message_id"], []).append({
                    "id": a["id"], "filename": a["filename"], "mime": a["mime"],
                    "size": a["size"], "sha256": a["sha256"],
                })
        for r in rows:
            out.append({
                "id": r["id"], "from": r["from_name"], "body": r["body"],
                "sent_at": r["sent_at"], "read_at": r["read_at"],
                "in_reply_to": r["in_reply_to"],
                "attachments": atts_by_msg.get(r["id"], []),
            })
    return out


@mcp.tool()
def mark_read(ids: list[int]) -> dict:
    """Mark one or more messages as read.

    Args:
        ids: list of message IDs to mark read

    Returns:
        {marked: int}
    """
    if not ids:
        return {"marked": 0}

    if REMOTE:
        r = _remote("POST", "/mark_read", {"ids": ids})
        return {"marked": r["count"]}

    qmarks = ",".join("?" * len(ids))
    with _connect() as c:
        cur = c.execute(
            f"UPDATE messages SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            f"WHERE id IN ({qmarks}) AND to_name = ? AND read_at IS NULL",
            list(ids) + [NAME],
        )
    return {"marked": cur.rowcount}


@mcp.tool()
def peers() -> list[dict]:
    """List all mailbox peers.

    Returns:
        List of {name, last_seen_at}, sorted by last_seen desc
    """
    if REMOTE:
        r = _remote("GET", "/peers")
        return [{"name": p["name"], "last_seen_at": p["last_seen_at"]} for p in r["peers"]]

    with _connect() as c:
        rows = c.execute(
            "SELECT name, last_seen_at FROM peers ORDER BY last_seen_at DESC"
        ).fetchall()
    return [{"name": r["name"], "last_seen_at": r["last_seen_at"]} for r in rows]


@mcp.tool()
def download(attachment_id: int, save_to: str) -> dict:
    """Download an attachment blob to a local file path.

    Use the attachment_id field returned by inbox()[].attachments[].id.
    Spoke watcher only notifies of new attachments; it does NOT auto-download.
    Call this explicitly when you decide to fetch the blob.

    Args:
        attachment_id: id from inbox()[].attachments[].id
        save_to: absolute local path to save to (parent dir created if missing;
                 overwrites existing file)

    Returns:
        {path, size, sha256}
    """
    save_path = Path(save_to)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    if REMOTE:
        data, sha = _remote_get_bytes(f"/attachment/{attachment_id}")
        # Verify integrity if server returned hash
        if sha:
            local_sha = hashlib.sha256(data).hexdigest()
            if local_sha != sha:
                raise RuntimeError(
                    f"sha256 mismatch: server={sha} local={local_sha}"
                )
        save_path.write_bytes(data)
        return {
            "path": str(save_path.absolute()),
            "size": len(data),
            "sha256": sha or hashlib.sha256(data).hexdigest(),
        }

    # Local mode: look up blob via DB then read from ATTACHMENTS_DIR
    with _connect() as c:
        row = c.execute(
            "SELECT filename, size, sha256 FROM attachments WHERE id=?",
            (attachment_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"attachment {attachment_id} not found")
    assert ATTACHMENTS_DIR is not None
    src = ATTACHMENTS_DIR / row["sha256"][:2] / row["sha256"]
    if not src.exists():
        raise RuntimeError(f"blob missing at {src}")
    save_path.write_bytes(src.read_bytes())
    return {
        "path": str(save_path.absolute()),
        "size": row["size"],
        "sha256": row["sha256"],
    }


@mcp.tool()
def whoami() -> dict:
    """Return this instance's identity and where it reads/writes."""
    if REMOTE:
        return {"name": NAME, "mode": "remote", "hub": REMOTE}
    return {"name": NAME, "mode": "local", "db_path": str(DB_PATH.absolute())}


if __name__ == "__main__":
    mcp.run()
