"""Reactions on mailbox messages.

Lightweight ack/triage signal — "👍" / "✅" / "🔥" / "👀" — without needing to
send a full reply. Reduces channel noise for "got it" style acknowledgements.

Imported by:
  - server.py (MCP react/unreact tools, inbox attach)
  - mailbox-server.py (REST /react /unreact endpoints, inbox/SSE attach)
  - smoke_test_reactions.py

Naming: hyphenless module so Python can `from mailbox_reactions import ...`.

Schema (idempotent DDL):
  reactions(id, message_id, actor, emoji, created_at)
  UNIQUE(message_id, actor, emoji)  -- one actor / one emoji / one message = one row

UNIQUE constraint means `react()` twice from same actor+same emoji is a no-op
(catches integrity error). Toggling is explicit via `unreact()`.

`emoji` is a freeform TEXT — clients are responsible for picking sane values.
Validation cap at MAX_EMOJI_LEN to keep this from becoming a body-text channel.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MAX_EMOJI_LEN = 32  # sanity cap; real emojis are ≤ ~7 bytes in UTF-8


def init_schema(db_path: Path) -> None:
    """Idempotent DDL — reactions table + indexes."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                actor      TEXT NOT NULL,
                emoji      TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(message_id, actor, emoji)
            );
            CREATE INDEX IF NOT EXISTS idx_reactions_message
                ON reactions(message_id);
            CREATE INDEX IF NOT EXISTS idx_reactions_actor
                ON reactions(actor);
        """)
        conn.commit()
    finally:
        conn.close()


def react(db_path: Path, message_id: int, actor: str, emoji: str) -> dict:
    """Add a reaction. Idempotent on (message_id, actor, emoji) — re-reacting
    is a no-op that returns `{added: False, id: <existing>}`.

    Raises ValueError if `emoji` exceeds MAX_EMOJI_LEN.
    """
    if not emoji or len(emoji) > MAX_EMOJI_LEN:
        raise ValueError(f"emoji must be 1..{MAX_EMOJI_LEN} chars, got {len(emoji)}")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "INSERT INTO reactions(message_id, actor, emoji) "
                "VALUES(?, ?, ?) RETURNING id, created_at",
                (message_id, actor, emoji),
            ).fetchone()
            conn.commit()
            return {"added": True, "id": row["id"], "created_at": row["created_at"]}
        except sqlite3.IntegrityError:
            # Already exists. Look up the existing row for a useful return.
            row = conn.execute(
                "SELECT id, created_at FROM reactions "
                "WHERE message_id=? AND actor=? AND emoji=?",
                (message_id, actor, emoji),
            ).fetchone()
            return {"added": False, "id": row["id"] if row else None,
                    "created_at": row["created_at"] if row else None}
    finally:
        conn.close()


def unreact(db_path: Path, message_id: int, actor: str, emoji: str) -> int:
    """Remove a reaction. Returns rowcount (0 if no match)."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        cur = conn.execute(
            "DELETE FROM reactions WHERE message_id=? AND actor=? AND emoji=?",
            (message_id, actor, emoji),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_for_messages(db_path: Path, message_ids: list[int]) -> dict[int, list[dict]]:
    """Fetch reactions for a batch of messages, keyed by message_id.

    Used by inbox() / SSE / search to attach reactions per row in one round-trip.
    Returns dict[int, list[{actor, emoji, created_at}]]. Missing message_ids
    get empty lists (caller-friendly default).
    """
    if not message_ids:
        return {}
    out: dict[int, list[dict]] = {mid: [] for mid in message_ids}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(message_ids))
        try:
            rows = conn.execute(
                f"SELECT message_id, actor, emoji, created_at "
                f"FROM reactions WHERE message_id IN ({placeholders}) "
                f"ORDER BY message_id, id",
                message_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            return out  # table not initialized yet
        for r in rows:
            out[r["message_id"]].append({
                "actor": r["actor"], "emoji": r["emoji"],
                "created_at": r["created_at"],
            })
        return out
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability for /health."""
    if not db_path.exists():
        return {"reaction_count": 0, "reaction_unique_emojis": 0}
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            total = conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
            unique = conn.execute("SELECT COUNT(DISTINCT emoji) FROM reactions").fetchone()[0]
            return {"reaction_count": total, "reaction_unique_emojis": unique}
        except sqlite3.OperationalError:
            return {"reaction_count": 0, "reaction_unique_emojis": 0}
    finally:
        conn.close()
