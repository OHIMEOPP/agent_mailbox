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

import json
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
if not REMOTE:
    DB_PATH = Path(
        os.environ.get(
            "CLAUDE_MAILBOX_DB",
            str(Path.home() / ".claude" / "mailbox" / "mailbox.db"),
        )
    )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------- Remote (REST) helper ----------

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


# ---------- Local DB helpers ----------

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
                read_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_to_unread
                ON messages(to_name, read_at);

            CREATE TABLE IF NOT EXISTS peers (
                name          TEXT PRIMARY KEY,
                last_seen_at  TEXT NOT NULL
            );
            """
        )
        c.execute(
            "INSERT INTO peers(name, last_seen_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (NAME,),
        )


if not REMOTE:
    _init_db()


# ---------- MCP server & tools ----------

mcp = FastMCP("mailbox")


@mcp.tool()
def send(to: str, body: str) -> dict:
    """Send a message to another Claude Code instance.

    Dispatched via REST to remote hub if CLAUDE_MAILBOX_REMOTE is set, else
    written directly to local SQLite.

    Args:
        to: recipient's CLAUDE_MAILBOX_NAME (e.g. "wiki", "koatag")
        body: message text

    Returns:
        {id, sent_at, from, to}
    """
    if REMOTE:
        r = _remote("POST", "/send", {"from": NAME, "to": to, "body": body})
        return {"id": r["id"], "sent_at": r["sent_at"], "from": NAME, "to": to}

    with _connect() as c:
        row = c.execute(
            "INSERT INTO messages(from_name, to_name, body) "
            "VALUES(?, ?, ?) RETURNING id, sent_at",
            (NAME, to, body),
        ).fetchone()
    return {"id": row["id"], "sent_at": row["sent_at"], "from": NAME, "to": to}


@mcp.tool()
def inbox(unread_only: bool = True, limit: int = 50) -> list[dict]:
    """Fetch messages addressed to this instance.

    Args:
        unread_only: if True (default), only return messages not yet marked read
        limit: max messages to return (default 50)

    Returns:
        List of {id, from, body, sent_at, read_at}
    """
    if REMOTE:
        unread_flag = "1" if unread_only else "0"
        r = _remote("GET", f"/inbox?name={NAME}&unread={unread_flag}&limit={limit}")
        return [
            {"id": m["id"], "from": m["from_name"], "body": m["body"],
             "sent_at": m["sent_at"], "read_at": m["read_at"]}
            for m in r["messages"]
        ]

    sql = "SELECT id, from_name, body, sent_at, read_at FROM messages WHERE to_name = ?"
    params: list = [NAME]
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    with _connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [
        {"id": r["id"], "from": r["from_name"], "body": r["body"],
         "sent_at": r["sent_at"], "read_at": r["read_at"]}
        for r in rows
    ]


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
def whoami() -> dict:
    """Return this instance's identity and where it reads/writes."""
    if REMOTE:
        return {"name": NAME, "mode": "remote", "hub": REMOTE}
    return {"name": NAME, "mode": "local", "db_path": str(DB_PATH.absolute())}


if __name__ == "__main__":
    mcp.run()
