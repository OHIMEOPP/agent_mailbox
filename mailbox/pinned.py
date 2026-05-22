"""Pin/unpin support for mailbox messages.

A pinned message stays at the top of the inbox (above priority order) and
is exempt from retention sweep — meant for "keep this around" references,
pending actions, important decisions.

Wire-up:
  - inbox SELECT prepends ORDER BY pinned DESC before priority DESC, id ASC
  - mailbox.sweep.sweep_all excludes pinned rows from read/unread cutoff
  - server.py + mailbox-server.py expose pin/unpin tools/endpoints
  - audit log records pin/unpin via existing ACTIONS vocabulary

Schema column added by mailbox.migrations v006:
  ALTER TABLE messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
  CREATE INDEX idx_messages_pinned ON messages(pinned) WHERE pinned = 1;
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def pin(db_path: Path, message_id: int, actor: str) -> dict:
    """Pin a message. Returns {pinned: bool, was_already_pinned: bool, id}.

    Idempotent — pinning an already-pinned message returns
    was_already_pinned=True without UPDATE side-effect.
    Raises FileNotFoundError if message_id doesn't exist.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pinned FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"message {message_id} not found")
        was_pinned = bool(row["pinned"])
        if not was_pinned:
            conn.execute(
                "UPDATE messages SET pinned = 1 WHERE id = ?", (message_id,)
            )
            conn.commit()
        return {
            "pinned": True,
            "was_already_pinned": was_pinned,
            "id": message_id,
            "actor": actor,
        }
    finally:
        conn.close()


def unpin(db_path: Path, message_id: int, actor: str) -> dict:
    """Unpin a message. Returns {pinned: bool, was_pinned: bool, id}.

    Idempotent — unpinning an unpinned message returns was_pinned=False
    without UPDATE side-effect. Raises FileNotFoundError if missing.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pinned FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"message {message_id} not found")
        was_pinned = bool(row["pinned"])
        if was_pinned:
            conn.execute(
                "UPDATE messages SET pinned = 0 WHERE id = ?", (message_id,)
            )
            conn.commit()
        return {
            "pinned": False,
            "was_pinned": was_pinned,
            "id": message_id,
            "actor": actor,
        }
    finally:
        conn.close()


def list_pinned(db_path: Path, recipient: str | None = None,
                limit: int = 50) -> list[dict]:
    """List pinned messages, newest-first. Optional recipient filter."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, from_name, to_name, body, sent_at, read_at, "
               "in_reply_to, expires_at, priority "
               "FROM messages WHERE pinned = 1")
        params: list = []
        if recipient:
            sql += " AND to_name = ?"
            params.append(recipient)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # column not migrated yet — defensive return
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability counters for /health.

    Returns {pinned_count, pinned_recipients: int}.
    """
    out = {"pinned_count": 0, "pinned_recipients": 0}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            out["pinned_count"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE pinned = 1"
            ).fetchone()[0]
            out["pinned_recipients"] = conn.execute(
                "SELECT COUNT(DISTINCT to_name) FROM messages WHERE pinned = 1"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()
