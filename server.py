# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0.0"]
# ///
"""
Claude Code Mailbox — Cross-instance message queue via MCP.

Each Claude Code instance spawns its own copy of this stdio MCP server,
but they all read/write the same SQLite file, enabling async message
passing between sessions.

Configuration via env vars (set in each project's .mcp.json):
  CLAUDE_MAILBOX_NAME  — this instance's identity (REQUIRED, e.g. "wiki", "koatag")
  CLAUDE_MAILBOX_DB    — SQLite file path (default: ~/.claude-mailbox.db)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------- Configuration ----------

NAME = os.environ.get("CLAUDE_MAILBOX_NAME")
if not NAME:
    raise RuntimeError(
        "CLAUDE_MAILBOX_NAME env var must be set in your .mcp.json "
        "(e.g. 'wiki', 'koatag') so the mailbox knows your identity."
    )

DB_PATH = Path(
    os.environ.get(
        "CLAUDE_MAILBOX_DB",
        str(Path.home() / ".claude-mailbox.db"),
    )
)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------- DB helpers ----------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
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


_init_db()


# ---------- MCP server & tools ----------

mcp = FastMCP("mailbox")


@mcp.tool()
def send(to: str, body: str) -> dict:
    """Send a message to another Claude Code instance.

    The message is queued in the shared SQLite mailbox and will be
    delivered the next time the recipient calls `inbox`.

    Args:
        to: recipient's CLAUDE_MAILBOX_NAME (e.g. "wiki", "koatag")
        body: message text

    Returns:
        {id, sent_at, from, to}
    """
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO messages(from_name, to_name, body) "
            "VALUES(?, ?, ?) RETURNING id, sent_at",
            (NAME, to, body),
        )
        row = cur.fetchone()
    return {
        "id": row["id"],
        "sent_at": row["sent_at"],
        "from": NAME,
        "to": to,
    }


@mcp.tool()
def inbox(unread_only: bool = True, limit: int = 50) -> list[dict]:
    """Fetch messages addressed to this instance.

    Args:
        unread_only: if True (default), only return messages not yet marked read
        limit: max messages to return (default 50)

    Returns:
        List of {id, from, body, sent_at, read_at}
    """
    sql = "SELECT id, from_name, body, sent_at, read_at FROM messages WHERE to_name = ?"
    params: list = [NAME]
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    with _connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "from": r["from_name"],
            "body": r["body"],
            "sent_at": r["sent_at"],
            "read_at": r["read_at"],
        }
        for r in rows
    ]


@mcp.tool()
def mark_read(ids: list[int]) -> dict:
    """Mark one or more messages as read.

    Only messages addressed to this instance can be marked.

    Args:
        ids: list of message IDs to mark read

    Returns:
        {marked: int}
    """
    if not ids:
        return {"marked": 0}
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
    """List all mailbox peers (instances that have ever connected).

    Returns:
        List of {name, last_seen_at}, sorted by last_seen desc
    """
    with _connect() as c:
        rows = c.execute(
            "SELECT name, last_seen_at FROM peers ORDER BY last_seen_at DESC"
        ).fetchall()
    return [{"name": r["name"], "last_seen_at": r["last_seen_at"]} for r in rows]


@mcp.tool()
def whoami() -> dict:
    """Return this instance's identity and the DB file location."""
    return {
        "name": NAME,
        "db_path": str(DB_PATH.absolute()),
    }


if __name__ == "__main__":
    mcp.run()
