"""Read receipts — sender asks "tell me when it's actually read" (BACKLOG #3).

send(request_receipt=True) stamps messages.request_receipt=1. When the
recipient mark_read's such a message, the marking side auto-inserts a small
ack message back to the original sender:

    body = "receipt: msg #<id> read at <ts>"   (in_reply_to=<id>)

Loop-safety: the ack row is inserted with request_receipt=0, so reading a
receipt never generates another receipt.

Both writers (server.py MCP local mode, mailbox-server.py REST hub) call this
inside the SAME connection/transaction as the read_at UPDATE, in this order:
  1. collect_pending(conn, ids, reader?)  — BEFORE the UPDATE ("read_at IS
     NULL" is the newly-read filter; after the UPDATE it is gone, and a
     second mark_read of the same id must not re-send)
  2. UPDATE ... SET read_at ...
  3. insert_receipts(conn, pending)       — one ack per collected row
Callers then audit one action=read_receipt_sent event per receipt.
"""
from __future__ import annotations

import sqlite3


def collect_pending(conn: sqlite3.Connection, ids: list[int],
                    reader: str | None = None) -> list[dict]:
    """Rows among `ids` that are unread AND want a receipt. Call BEFORE the
    read_at UPDATE. `reader` narrows to messages addressed to that name
    (server.py local mode); hub passes None (its UPDATE has no to_name filter
    either)."""
    if not ids:
        return []
    qmarks = ",".join("?" * len(ids))
    sql = (f"SELECT id, from_name, to_name FROM messages "
           f"WHERE id IN ({qmarks}) AND read_at IS NULL AND request_receipt = 1")
    args: list = list(ids)
    if reader is not None:
        sql += " AND to_name = ?"
        args.append(reader)
    return [{"id": r["id"], "from_name": r["from_name"], "to_name": r["to_name"]}
            for r in conn.execute(sql, args).fetchall()]


def insert_receipts(conn: sqlite3.Connection, pending: list[dict]) -> list[dict]:
    """Insert one ack message per pending row (reader → original sender).

    Returns [{receipt_id, sent_at, to, for_msg_id}, ...] for audit logging.
    FTS sync rides the existing messages_fts INSERT trigger; SSE watchers pick
    the row up via their normal poll.
    """
    out: list[dict] = []
    if not pending:
        return out
    now = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    for p in pending:
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, in_reply_to, request_receipt) "
            "VALUES(?, ?, ?, ?, 0) RETURNING id, sent_at",
            (p["to_name"], p["from_name"],
             f"receipt: msg #{p['id']} read at {now}", p["id"]),
        ).fetchone()
        out.append({"receipt_id": row["id"], "sent_at": row["sent_at"],
                    "to": p["from_name"], "for_msg_id": p["id"]})
    return out
