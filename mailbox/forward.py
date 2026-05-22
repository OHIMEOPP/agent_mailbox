"""Message forwarding — copy an existing message to a new recipient with
original-sender attribution preserved.

Use case: "this conversation is relevant to koatag-frontend, forward thread"
without losing who said what. Different from a reply (in_reply_to keeps
the original recipient as the conversation root); forward branches off
to a new audience.

Wire format:
  Forward creates a new messages row where:
    - from_name   = the forwarder (CLAUDE_MAILBOX_NAME)
    - to_name     = the new recipient
    - body        = optional note + ">>> forwarded from <orig.from>" header
                    + original body
    - forwarded_from_msg_id = orig.id (FK-less, schema v008)
    - in_reply_to = NULL (forward starts a new chain — chain forward by
                    re-forwarding the forward)
    - priority    = orig.priority (inherit; can override via param)

Schema column added by mailbox.migrations v008:
  ALTER TABLE messages ADD COLUMN forwarded_from_msg_id INTEGER;
  CREATE INDEX idx_messages_forwarded_from ON messages(forwarded_from_msg_id)
    WHERE forwarded_from_msg_id IS NOT NULL;
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_FORWARD_HEADER = ">>> forwarded from {orig_from} (msg #{orig_id}) {orig_sent_at}\n"


def forward(
    db_path: Path,
    message_id: int,
    forwarder: str,
    to_name: str,
    note: str | None = None,
    inherit_priority: bool = True,
) -> dict:
    """Create a new message that copies the original with attribution.

    Args:
        db_path: mailbox DB
        message_id: id of message to forward
        forwarder: who is forwarding (will be from_name on the new row)
        to_name: new recipient name (literal — no alias glob)
        note: optional prefix added before the forward header
        inherit_priority: copy original.priority into the new row (default True)

    Returns:
        {id, sent_at, forwarded_from_msg_id, forwarded_to, forwarded_by}.

    Raises FileNotFoundError if the source message doesn't exist.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        orig = conn.execute(
            "SELECT id, from_name, to_name, body, sent_at, "
            "COALESCE(priority, 0) AS priority "
            "FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if orig is None:
            raise FileNotFoundError(f"message {message_id} not found")

        # Compose forwarded body — note (optional) + header + original body
        header = _FORWARD_HEADER.format(
            orig_from=orig["from_name"],
            orig_id=orig["id"],
            orig_sent_at=orig["sent_at"],
        )
        if note:
            new_body = note.rstrip() + "\n\n" + header + orig["body"]
        else:
            new_body = header + orig["body"]

        priority = orig["priority"] if inherit_priority else 0

        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, priority, "
            "forwarded_from_msg_id) VALUES(?, ?, ?, ?, ?) "
            "RETURNING id, sent_at",
            (forwarder, to_name, new_body, priority, message_id),
        ).fetchone()
        conn.commit()
        return {
            "id": row["id"],
            "sent_at": row["sent_at"],
            "forwarded_from_msg_id": message_id,
            "forwarded_to": to_name,
            "forwarded_by": forwarder,
            "inherited_priority": priority if inherit_priority else None,
        }
    finally:
        conn.close()


def list_forwards_of(db_path: Path, source_msg_id: int) -> list[dict]:
    """Find all forwarded copies derived from a source message.

    Useful for `mailbox-dump --tree`: render a chain showing where a message
    has been forwarded to. Returns [{id, from_name, to_name, sent_at}, ...].
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT id, from_name, to_name, sent_at "
                "FROM messages WHERE forwarded_from_msg_id = ? "
                "ORDER BY id ASC",
                (source_msg_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability counters: how many forwarded messages exist."""
    out = {"forwarded_count": 0, "forward_sources_count": 0}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            out["forwarded_count"] = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE forwarded_from_msg_id IS NOT NULL"
            ).fetchone()[0]
            out["forward_sources_count"] = conn.execute(
                "SELECT COUNT(DISTINCT forwarded_from_msg_id) FROM messages "
                "WHERE forwarded_from_msg_id IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()
