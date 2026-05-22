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

import fnmatch
import hashlib
import json
import mimetypes
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import mailbox_audit
import mailbox_migrations
import mailbox_reactions
import mailbox_scheduled
from mcp.server.fastmcp import FastMCP

# Mailing-list / glob fanout settings (must match mailbox-server.py)
ALIAS_ACTIVE_DAYS = 7
ALIAS_MAX_RECIPIENTS = 32


def _is_alias_pattern(name: str) -> bool:
    """True if name contains shell-glob magic (*?[)."""
    return any(c in name for c in "*?[")


def _resolve_alias(conn, pattern: str) -> list[str]:
    """Local-mode alias resolution. Mirrors mailbox-server.py logic."""
    if not _is_alias_pattern(pattern):
        return [pattern]
    cutoff = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{ALIAS_ACTIVE_DAYS} days",),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT name FROM peers WHERE last_seen_at >= ? ORDER BY name",
        (cutoff,),
    ).fetchall()
    matched = [r["name"] for r in rows if fnmatch.fnmatchcase(r["name"], pattern)]
    return matched[:ALIAS_MAX_RECIPIENTS]

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
                claimed_by TEXT,
                claimed_until TEXT,
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
        # messages-table forward-compat ALTERs + partial indexes are owned by
        # mailbox_migrations now. Centralized, versioned, idempotent on fresh
        # and legacy DBs. See mailbox_migrations.MIGRATIONS for the list.
        c.execute(
            "INSERT INTO peers(name, last_seen_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (NAME,),
        )


def _init_fts() -> None:
    """Create FTS5 virtual table + triggers + backfill any existing messages.
    Silently skip if FTS5 not compiled into this Python's sqlite3 build.
    """
    with _connect() as c:
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                      "USING fts5(body, content='messages', "
                      "content_rowid='id', tokenize='unicode61')")
        except sqlite3.OperationalError:
            return  # FTS5 not available — search() will raise at call time
        c.executescript("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, body)
                    VALUES('delete', old.id, old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, body)
                    VALUES('delete', old.id, old.body);
                INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
            END;
        """)
        c.execute(
            "INSERT INTO messages_fts(rowid, body) "
            "SELECT id, body FROM messages "
            "WHERE id NOT IN (SELECT rowid FROM messages_fts)"
        )


if not REMOTE:
    _init_db()
    # Versioned migrations for messages-table ALTERs (has_attachments,
    # in_reply_to, expires_at, …). Run after _init_db so the table exists.
    mailbox_migrations.apply(DB_PATH)
    mailbox_audit.init_schema(DB_PATH)
    _init_fts()
    mailbox_reactions.init_schema(DB_PATH)
    mailbox_scheduled.init_schema(DB_PATH)


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


import re as _re_ttl
from datetime import datetime as _datetime_ttl, timedelta as _timedelta_ttl, timezone as _timezone_ttl

_TTL_RELATIVE = _re_ttl.compile(r"^(\d+)([mhd])$")


def _resolve_expires_at(spec: str | None) -> str | None:
    """Parse expires_at arg into an ISO 8601 UTC string, or None.

    Accepts:
      - None / "" → None (no expiry)
      - ISO 8601 with `Z` or `+00:00` (`2026-05-25T00:00:00Z`) — pass through
      - Relative: `30m`, `1h`, `7d` (computed from now in UTC)
    """
    if spec is None or spec == "":
        return None
    m = _TTL_RELATIVE.match(spec)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "m":
            delta = _timedelta_ttl(minutes=n)
        elif unit == "h":
            delta = _timedelta_ttl(hours=n)
        else:
            delta = _timedelta_ttl(days=n)
        ts = _datetime_ttl.now(_timezone_ttl.utc) + delta
        # SQLite-compatible ISO 8601 with millisecond precision and Z suffix
        return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
    # Assume ISO; lex-compare in SQL is correct for sorted ISO strings.
    return spec


# ---------- MCP server & tools ----------

mcp = FastMCP("mailbox")


@mcp.tool()
def send(to: str, body: str, files: list[str] | None = None,
         in_reply_to: int | None = None,
         expires_at: str | None = None,
         deliver_at: str | None = None) -> dict:
    """Send a message (optionally with file attachments) to another Claude Code instance.

    Args:
        to: recipient's CLAUDE_MAILBOX_NAME (e.g. "wiki", "koatag@LAPTOP-XYZ789").
            ALIAS / fanout: pass a glob like "koatag*" or "*-frontend" to send
            to all matching active peers (heartbeat within 7 days). Glob magic
            chars are `*`, `?`, `[`. Capped at 32 recipients. Empty match → error.
        body: message text
        files: optional list of host filesystem paths to attach. Each file up to
               100 MB, total payload up to 500 MB. For folder transfer, zip
               first then attach the zip.
        in_reply_to: optional message id this message is a reply to. Used by
               mailbox-dump tree view to render conversation threads. Pass the
               `id` from a prior inbox() entry. No FK enforcement — if the
               parent was retention-pruned, the field becomes a broken chain
               (rendered as orphan in dump).
        expires_at: optional TTL for ephemeral messages. Retention sweep deletes
               messages whose expires_at < now, regardless of read state. Accepts
               ISO 8601 (`2026-05-25T00:00:00Z`) or relative shorthand `30m` /
               `1h` / `7d` (computed from now). None or omitted = no expiry.
               Useful for status pings / progress updates that have no value
               beyond the next sweep.
        deliver_at: optional scheduled-send time. If set, message enters the
               scheduled_messages queue; a daemon delivers it (inserts into
               messages, fires SSE / FTS / audit) when deliver_at <= now.
               Accepts ISO 8601 or relative shorthand. Default tick is 30s so
               minimum useful delay is ~30 seconds. NOT compatible with files=
               (raises) — schedule text-only for now.

    Returns:
        Literal `to`: {id, sent_at, from, to, in_reply_to?, expires_at?, attachments?: [...]}
        Glob `to` (fanout): {fanout: True, pattern, matched_peers, count,
                             messages: [{id, sent_at, to, attachments?}, ...],
                             from, in_reply_to?, expires_at?}
    """
    resolved_expires_at = _resolve_expires_at(expires_at)
    # Scheduled-send intercept — if deliver_at given, defer until daemon delivers.
    # files + deliver_at combo is not supported in this pass; we error early.
    if deliver_at:
        if files:
            raise RuntimeError(
                "deliver_at + files: not supported. Schedule a text message "
                "or send files immediately."
            )
        try:
            resolved_deliver_at = mailbox_scheduled.parse_deliver_at(deliver_at)
        except ValueError as e:
            raise RuntimeError(str(e))
        if REMOTE:
            body_payload_s: dict = {"from": NAME, "to": to, "body": body,
                                     "deliver_at": resolved_deliver_at}
            if in_reply_to is not None:
                body_payload_s["in_reply_to"] = in_reply_to
            if resolved_expires_at is not None:
                body_payload_s["expires_at"] = resolved_expires_at
            r = _remote("POST", "/send", body_payload_s)
            return {**r, "from": NAME}

        # Local mode
        is_pattern = _is_alias_pattern(to)
        with _connect() as c:
            recipients = _resolve_alias(c, to)
        if not recipients:
            raise RuntimeError(
                f"no active peers (heartbeat ≤{ALIAS_ACTIVE_DAYS}d) match pattern '{to}'")
        queued: list[dict] = []
        for to_name in recipients:
            q = mailbox_scheduled.enqueue(
                DB_PATH, from_name=NAME, to_name=to_name,
                body=body, deliver_at=resolved_deliver_at,
                in_reply_to=in_reply_to, expires_at=resolved_expires_at,
            )
            queued.append({"scheduled_id": q["id"], "to": to_name,
                           "deliver_at": q["deliver_at"]})
        for q in queued:
            mailbox_audit.log_event(
                DB_PATH, actor=NAME, action="send", target=q["to"],
                payload={"scheduled_id": q["scheduled_id"],
                         "deliver_at": q["deliver_at"],
                         "body_len": len(body),
                         "in_reply_to": in_reply_to,
                         "expires_at": resolved_expires_at,
                         "scheduled": True,
                         "alias_pattern": to if is_pattern else None},
            )
        return {
            "scheduled": True, "deliver_at": resolved_deliver_at,
            "count": len(queued), "items": queued,
            "fanout": is_pattern,
            "matched_peers": recipients if is_pattern else None,
            "from": NAME,
        }
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
            if resolved_expires_at is not None:
                body_payload["expires_at"] = resolved_expires_at
            r = _remote_multipart(
                "/send-file",
                body_payload,
                file_parts,
            )
            if r.get("fanout"):
                return {**r, "from": NAME, "to": to, "in_reply_to": in_reply_to}
            return {
                "id": r["id"], "sent_at": r["sent_at"], "from": NAME, "to": to,
                "in_reply_to": in_reply_to,
                "expires_at": resolved_expires_at,
                "attachments": r["attachments"],
            }

        # Local mode: write blobs once (sha dedup), then per-recipient rows
        is_pattern = _is_alias_pattern(to)
        written: list[dict] = []
        for fname, mime, data in file_parts:
            sha, size = _write_blob(data)
            written.append({"filename": fname, "mime": mime, "size": size, "sha256": sha})
        with _connect() as c:
            recipients = _resolve_alias(c, to)
            if not recipients:
                raise RuntimeError(
                    f"no active peers (heartbeat ≤{ALIAS_ACTIVE_DAYS}d) match "
                    f"pattern '{to}'")
            fanout_results: list[dict] = []
            for to_name in recipients:
                row = c.execute(
                    "INSERT INTO messages(from_name, to_name, body, has_attachments, in_reply_to, expires_at) "
                    "VALUES(?, ?, ?, 1, ?, ?) RETURNING id, sent_at",
                    (NAME, to_name, body, in_reply_to, resolved_expires_at),
                ).fetchone()
                msg_id = row["id"]
                attach_rows = []
                for w in written:
                    r2 = c.execute(
                        "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
                        "VALUES(?, ?, ?, ?, ?) RETURNING id",
                        (msg_id, w["filename"], w["mime"], w["size"], w["sha256"]),
                    ).fetchone()
                    attach_rows.append({
                        "id": r2["id"], "filename": w["filename"], "mime": w["mime"],
                        "size": w["size"], "sha256": w["sha256"],
                    })
                fanout_results.append({"id": msg_id, "sent_at": row["sent_at"],
                                       "to": to_name, "attachments": attach_rows})

        for rec in fanout_results:
            mailbox_audit.log_event(
                DB_PATH, actor=NAME, action="send", target=rec["to"],
                payload={"msg_id": rec["id"], "body_len": len(body),
                         "files_count": len(rec["attachments"]),
                         "in_reply_to": in_reply_to,
                         "expires_at": resolved_expires_at,
                         "alias_pattern": to if is_pattern else None},
            )

        if is_pattern:
            return {
                "fanout": True, "pattern": to, "matched_peers": recipients,
                "count": len(fanout_results), "messages": fanout_results,
                "from": NAME, "in_reply_to": in_reply_to,
                "expires_at": resolved_expires_at,
            }
        r0 = fanout_results[0]
        return {
            "id": r0["id"], "sent_at": r0["sent_at"], "from": NAME, "to": to,
            "in_reply_to": in_reply_to,
            "expires_at": resolved_expires_at,
            "attachments": r0["attachments"],
        }

    # text-only path
    if REMOTE:
        body_payload2: dict = {"from": NAME, "to": to, "body": body}
        if in_reply_to is not None:
            body_payload2["in_reply_to"] = in_reply_to
        if resolved_expires_at is not None:
            body_payload2["expires_at"] = resolved_expires_at
        r = _remote("POST", "/send", body_payload2)
        # Hub handles fanout — surface its shape back. Single recipient gets
        # {id, sent_at, ...}; pattern gets {fanout: true, messages: [...]}.
        if r.get("fanout"):
            return {**r, "from": NAME, "to": to, "in_reply_to": in_reply_to}
        return {"id": r["id"], "sent_at": r["sent_at"], "from": NAME, "to": to,
                "in_reply_to": in_reply_to,
                "expires_at": resolved_expires_at}

    is_pattern = _is_alias_pattern(to)
    with _connect() as c:
        recipients = _resolve_alias(c, to)
        if not recipients:
            raise RuntimeError(
                f"no active peers (heartbeat ≤{ALIAS_ACTIVE_DAYS}d) match "
                f"pattern '{to}'")
        inserted: list[dict] = []
        for to_name in recipients:
            row = c.execute(
                "INSERT INTO messages(from_name, to_name, body, in_reply_to, expires_at) "
                "VALUES(?, ?, ?, ?, ?) RETURNING id, sent_at",
                (NAME, to_name, body, in_reply_to, resolved_expires_at),
            ).fetchone()
            inserted.append({"id": row["id"], "sent_at": row["sent_at"], "to": to_name})

    for rec in inserted:
        mailbox_audit.log_event(
            DB_PATH, actor=NAME, action="send", target=rec["to"],
            payload={"msg_id": rec["id"], "body_len": len(body),
                     "files_count": 0, "in_reply_to": in_reply_to,
                     "expires_at": resolved_expires_at,
                     "alias_pattern": to if is_pattern else None},
        )

    if is_pattern:
        return {
            "fanout": True, "pattern": to, "matched_peers": recipients,
            "count": len(inserted), "messages": inserted,
            "from": NAME, "in_reply_to": in_reply_to,
            "expires_at": resolved_expires_at,
        }
    r0 = inserted[0]
    return {"id": r0["id"], "sent_at": r0["sent_at"], "from": NAME, "to": to,
            "in_reply_to": in_reply_to, "expires_at": resolved_expires_at}


@mcp.tool()
def inbox(unread_only: bool = True, limit: int = 50,
          claimable_only: bool = False) -> list[dict]:
    """Fetch messages addressed to this instance.

    Args:
        unread_only: if True (default), only return messages not yet marked read
        limit: max messages to return (default 50)
        claimable_only: if True, skip messages currently claimed by *another*
                        agent within the visibility-timeout window. Useful for
                        worker-loop patterns to avoid double-processing.
                        Messages you've claimed yourself are still returned.

    Returns:
        List of {id, from, body, sent_at, read_at, claimed_by, claimed_until,
                 in_reply_to, expires_at, attachments: [...], reactions: [...]}.
    """
    if REMOTE:
        unread_flag = "1" if unread_only else "0"
        claimable_flag = "1" if claimable_only else "0"
        r = _remote("GET",
                    f"/inbox?name={NAME}&unread={unread_flag}&limit={limit}"
                    f"&claimable={claimable_flag}")
        return [
            {"id": m["id"], "from": m["from_name"], "body": m["body"],
             "sent_at": m["sent_at"], "read_at": m["read_at"],
             "claimed_by": m.get("claimed_by"),
             "claimed_until": m.get("claimed_until"),
             "in_reply_to": m.get("in_reply_to"),
             "expires_at": m.get("expires_at"),
             "attachments": m.get("attachments", []),
             "reactions": m.get("reactions", [])}
            for m in r["messages"]
        ]

    sql = ("SELECT id, from_name, body, sent_at, read_at, has_attachments, "
           "in_reply_to, expires_at, claimed_by, claimed_until "
           "FROM messages WHERE to_name = ?")
    params: list = [NAME]
    if unread_only:
        sql += " AND read_at IS NULL"
    if claimable_only:
        # Skip claims by OTHER agents still within window; show mine + unclaimed + expired
        sql += (" AND (claimed_by IS NULL OR claimed_by = ? "
                "OR claimed_until < strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        params.append(NAME)
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
        reactions_by_msg = mailbox_reactions.list_for_messages(
            DB_PATH, [r["id"] for r in rows],
        )
        for r in rows:
            out.append({
                "id": r["id"], "from": r["from_name"], "body": r["body"],
                "sent_at": r["sent_at"], "read_at": r["read_at"],
                "claimed_by": r["claimed_by"],
                "claimed_until": r["claimed_until"],
                "in_reply_to": r["in_reply_to"],
                "expires_at": r["expires_at"],
                "attachments": atts_by_msg.get(r["id"], []),
                "reactions": reactions_by_msg.get(r["id"], []),
            })
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="inbox",
        payload={"unread_only": unread_only, "limit": limit, "returned": len(out)},
    )
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
        # Also auto-release any claim — mark_read implies done processing.
        cur = c.execute(
            f"UPDATE messages SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            f"claimed_by = NULL, claimed_until = NULL "
            f"WHERE id IN ({qmarks}) AND to_name = ? AND read_at IS NULL",
            list(ids) + [NAME],
        )
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="mark_read",
        payload={"ids": list(ids), "marked": cur.rowcount},
    )
    return {"marked": cur.rowcount}


@mcp.tool()
def claim(message_id: int, ttl_seconds: int = 600) -> dict:
    """Claim a message for exclusive processing (visibility timeout).

    Marks the message as "being processed by you" for ttl_seconds. Other agents
    that pass claimable_only=True to inbox() will skip it during that window.
    Re-claiming a message you already hold refreshes its TTL.

    Args:
        message_id: id from inbox()[].id. Must be addressed to you.
        ttl_seconds: claim duration (default 600 = 10 min). Caps at 86400 (24h).

    Returns:
        {ok: True, message_id, claimed_by, claimed_until}
        OR raises if already claimed by another agent within window.
    """
    ttl_seconds = min(max(1, ttl_seconds), 86400)
    if REMOTE:
        r = _remote("POST", "/claim",
                    {"actor": NAME, "message_id": message_id, "ttl_seconds": ttl_seconds})
        return r

    with _connect() as c:
        row = c.execute(
            "UPDATE messages SET claimed_by=?, "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) "
            "WHERE id=? AND to_name=? "
            "AND (claimed_by IS NULL OR claimed_by=? "
            "  OR claimed_until < strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "RETURNING id, claimed_by, claimed_until",
            (NAME, f"+{ttl_seconds} seconds", message_id, NAME, NAME),
        ).fetchone()
    if not row:
        # Find out why — either not addressed to NAME, or claimed by other
        with _connect() as c:
            existing = c.execute(
                "SELECT to_name, claimed_by, claimed_until FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
        if not existing:
            raise RuntimeError(f"message {message_id} not found")
        if existing["to_name"] != NAME:
            raise RuntimeError(
                f"message {message_id} addressed to {existing['to_name']}, not {NAME}")
        raise RuntimeError(
            f"message {message_id} claimed by {existing['claimed_by']} "
            f"until {existing['claimed_until']}")
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="claim", target=str(message_id),
        payload={"ttl_seconds": ttl_seconds, "claimed_until": row["claimed_until"]},
    )
    return {"ok": True, "message_id": row["id"],
            "claimed_by": row["claimed_by"], "claimed_until": row["claimed_until"]}


@mcp.tool()
def release(message_id: int) -> dict:
    """Release a claim you hold on a message. Idempotent."""
    if REMOTE:
        return _remote("POST", "/release",
                       {"actor": NAME, "message_id": message_id})
    with _connect() as c:
        cur = c.execute(
            "UPDATE messages SET claimed_by=NULL, claimed_until=NULL "
            "WHERE id=? AND claimed_by=?",
            (message_id, NAME),
        )
        released = cur.rowcount > 0
    if released:
        mailbox_audit.log_event(
            DB_PATH, actor=NAME, action="release", target=str(message_id),
            payload={},
        )
    return {"ok": True, "message_id": message_id, "released": released}


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
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="peers",
        payload={"count": len(rows)},
    )
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
        mailbox_audit.log_event(
            DB_PATH, actor=NAME, action="download",
            target=str(attachment_id),
            payload={"error": "not_found"}, ok=False,
        )
        raise RuntimeError(f"attachment {attachment_id} not found")
    assert ATTACHMENTS_DIR is not None
    src = ATTACHMENTS_DIR / row["sha256"][:2] / row["sha256"]
    if not src.exists():
        mailbox_audit.log_event(
            DB_PATH, actor=NAME, action="download",
            target=str(attachment_id),
            payload={"error": "blob_missing", "expected": str(src)}, ok=False,
        )
        raise RuntimeError(f"blob missing at {src}")
    save_path.write_bytes(src.read_bytes())
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="download",
        target=str(attachment_id),
        payload={"size": row["size"], "sha256": row["sha256"],
                 "filename": row["filename"]},
    )
    return {
        "path": str(save_path.absolute()),
        "size": row["size"],
        "sha256": row["sha256"],
    }


@mcp.tool()
def search(query: str, scope: str = "inbox", limit: int = 50) -> list[dict]:
    """Full-text search messages using SQLite FTS5.

    Args:
        query: FTS5 MATCH expression. Supports phrase search ("foo bar"),
               boolean (foo AND bar / foo OR bar / foo NOT bar), prefix (foo*),
               and column-aware NEAR(...). Default tokenizer is unicode61
               (good with CJK as long as words are space-separated; for fine-
               grained Chinese tokenization a custom tokenizer would be needed).
        scope: "inbox" (default — messages addressed to you),
               "sent" (messages you sent),
               "all" (no name filter — supervisor view).
        limit: max results (default 50, max 200).

    Returns:
        List of {id, from, to, snippet, sent_at, has_attachments, in_reply_to,
        rank} sorted by relevance (lower rank = better match, per bm25).
        `snippet` wraps matches in <b>...</b> and trims context to ~64 chars.
    """
    if scope not in ("inbox", "sent", "all"):
        raise RuntimeError(f"scope must be inbox/sent/all, got {scope!r}")
    limit = min(max(1, limit), 200)

    if REMOTE:
        import urllib.parse
        params = f"?q={urllib.parse.quote(query)}&scope={scope}&limit={limit}"
        if scope != "all":
            params += f"&name={urllib.parse.quote(NAME)}"
        r = _remote("GET", f"/search{params}")
        return r["results"]

    sql = (
        "SELECT m.id, m.from_name, m.to_name, "
        "snippet(messages_fts, 0, '<b>', '</b>', '...', 64) AS snippet, "
        "m.sent_at, m.has_attachments, m.in_reply_to, "
        "bm25(messages_fts) AS rank "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
        "WHERE messages_fts MATCH ?"
    )
    params_local: list = [query]
    if scope == "inbox":
        sql += " AND m.to_name = ?"
        params_local.append(NAME)
    elif scope == "sent":
        sql += " AND m.from_name = ?"
        params_local.append(NAME)
    sql += " ORDER BY rank LIMIT ?"
    params_local.append(limit)

    with _connect() as c:
        try:
            rows = c.execute(sql, params_local).fetchall()
        except sqlite3.OperationalError as e:
            raise RuntimeError(f"FTS5 search failed (query={query!r}): {e}")
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="search",
        payload={"query": query, "scope": scope, "limit": limit, "count": len(rows)},
    )
    return [
        {"id": r["id"], "from": r["from_name"], "to": r["to_name"],
         "snippet": r["snippet"], "sent_at": r["sent_at"],
         "has_attachments": r["has_attachments"], "in_reply_to": r["in_reply_to"],
         "rank": r["rank"]}
        for r in rows
    ]


@mcp.tool()
def whoami() -> dict:
    """Return this instance's identity and where it reads/writes."""
    if REMOTE:
        return {"name": NAME, "mode": "remote", "hub": REMOTE}
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="whoami",
        payload={"mode": "local"},
    )
    return {"name": NAME, "mode": "local", "db_path": str(DB_PATH.absolute())}


@mcp.tool()
def react(message_id: int, emoji: str) -> dict:
    """Add a reaction (emoji) to a mailbox message.

    Lightweight ack/triage signal — use instead of sending a full reply when
    you just want to acknowledge ("got it" = ✅) or flag ("urgent" = 🔥).

    Args:
        message_id: id from inbox()[].id of the message to react to.
        emoji: 1..32 chars freeform — convention is a single emoji, but any
               short label works (e.g. "ack", "👀").

    Returns:
        {added: bool, id, created_at}. added=False means a reaction with the
        same (message_id, this_actor, emoji) already exists (idempotent).
    """
    if REMOTE:
        r = _remote("POST", "/react",
                    {"actor": NAME, "message_id": message_id, "emoji": emoji})
        return r

    result = mailbox_reactions.react(DB_PATH, message_id, NAME, emoji)
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="react", target=str(message_id),
        payload={"emoji": emoji, "added": result["added"]},
    )
    return result


@mcp.tool()
def unreact(message_id: int, emoji: str) -> dict:
    """Remove a reaction previously added by this instance.

    Args:
        message_id: id of the message to remove the reaction from.
        emoji: exact emoji string previously passed to react().

    Returns:
        {removed: int} — 0 if nothing matched, 1 if removed.
    """
    if REMOTE:
        r = _remote("POST", "/unreact",
                    {"actor": NAME, "message_id": message_id, "emoji": emoji})
        return r

    removed = mailbox_reactions.unreact(DB_PATH, message_id, NAME, emoji)
    mailbox_audit.log_event(
        DB_PATH, actor=NAME, action="unreact", target=str(message_id),
        payload={"emoji": emoji, "removed": removed},
    )
    return {"removed": removed}


if __name__ == "__main__":
    mcp.run()
