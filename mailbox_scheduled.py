"""Scheduled-send queue for mailbox.

A new "scheduled_messages" table holds messages waiting for a delivery time.
A background thread polls every SCHEDULED_TICK_SECONDS; when deliver_at <= now,
the row is materialized into `messages` (triggering all the usual side effects
— FTS index, audit log, watcher SSE event, webhook fanout).

Why a separate table:
- Keeps `messages` clean of pre-delivery rows (no need to filter inbox queries
  by sent_at <= now everywhere).
- Lets `mailbox-scheduled.py` show + cancel pending deliveries without
  scanning the entire messages table.
- Once delivered, scheduled_messages.delivered_msg_id points to messages.id —
  the row stays as a forensic trail (joined for audit / dump purposes).

Imported by:
  - mailbox-server.py (daemon thread + /send accepts deliver_at)
  - mailbox-scheduled.py (CLI: list pending / cancel)
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEDULED_TICK_SECONDS = 30  # how often the daemon polls for pending deliveries

_RELATIVE_RE = re.compile(r"^(\d+)([smhd])$")


def init_schema(db_path: Path) -> None:
    """Idempotent DDL — safe on every server boot."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                deliver_at TEXT NOT NULL,
                in_reply_to INTEGER,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                delivered_msg_id INTEGER,
                cancelled_at TEXT
            );
        """)
        # Partial index for the daemon's hot query (pending rows only)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_pending "
            "ON scheduled_messages(deliver_at) "
            "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


def parse_deliver_at(spec: str | None) -> str | None:
    """Resolve `5m` / `2h` / `7d` / ISO into ISO 8601 (UTC).

    Returns None if spec is None / empty.
    Raises ValueError if spec is in the past after resolution.
    """
    if spec is None or not str(spec).strip():
        return None
    s = str(spec).strip()
    m = _RELATIVE_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"s": timedelta(seconds=n),
                 "m": timedelta(minutes=n),
                 "h": timedelta(hours=n),
                 "d": timedelta(days=n)}[unit]
        ts = datetime.now(timezone.utc) + delta
        out = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
        return out
    # Treat as ISO — basic sanity check
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", s):
        raise ValueError(f"deliver_at must be ISO 8601 (got {s!r}) or relative '5m'/'2h'/'7d'")
    return s


def enqueue(
    db_path: Path,
    from_name: str,
    to_name: str,
    body: str,
    deliver_at: str,
    in_reply_to: int | None = None,
    expires_at: str | None = None,
) -> dict:
    """Insert a pending scheduled message. deliver_at must already be ISO 8601."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        row = conn.execute(
            "INSERT INTO scheduled_messages(from_name, to_name, body, deliver_at, "
            "in_reply_to, expires_at) VALUES(?, ?, ?, ?, ?, ?) "
            "RETURNING id, created_at, deliver_at",
            (from_name, to_name, body, deliver_at, in_reply_to, expires_at),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "created_at": row["created_at"],
                "deliver_at": row["deliver_at"]}
    finally:
        conn.close()


def deliver_pending(db_path: Path) -> dict:
    """One pass: materialize all pending rows whose deliver_at <= now.

    Returns counter dict for the daemon's stderr summary.
    Each materialization is its own short transaction so partial progress is OK
    if the daemon dies mid-batch.
    """
    counters = {"delivered": 0, "delivered_ids": [], "scanned": 0}
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        now = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT id, from_name, to_name, body, deliver_at, in_reply_to, expires_at "
            "FROM scheduled_messages "
            "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL "
            "AND deliver_at <= ? ORDER BY deliver_at ASC",
            (now,),
        ).fetchall()
        counters["scanned"] = len(pending)

        for s in pending:
            # Insert into messages — same shape as a normal /send (peer
            # heartbeat NOT touched; the scheduled msg's sender may be inactive
            # at delivery time).
            row = conn.execute(
                "INSERT INTO messages(from_name, to_name, body, in_reply_to, expires_at) "
                "VALUES(?, ?, ?, ?, ?) RETURNING id",
                (s["from_name"], s["to_name"], s["body"],
                 s["in_reply_to"], s["expires_at"]),
            ).fetchone()
            conn.execute(
                "UPDATE scheduled_messages SET delivered_msg_id=? WHERE id=?",
                (row["id"], s["id"]),
            )
            conn.commit()
            counters["delivered"] += 1
            counters["delivered_ids"].append(row["id"])
    finally:
        conn.close()
    return counters


def list_pending(db_path: Path, include_delivered: bool = False) -> list[dict]:
    """Return scheduled_messages rows; pending first, then delivered/cancelled."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        if include_delivered:
            sql = ("SELECT * FROM scheduled_messages "
                   "ORDER BY (delivered_msg_id IS NULL) DESC, deliver_at ASC")
        else:
            sql = ("SELECT * FROM scheduled_messages "
                   "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL "
                   "ORDER BY deliver_at ASC")
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cancel(db_path: Path, scheduled_id: int) -> dict:
    """Mark a pending row as cancelled. Idempotent if already delivered/cancelled."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, delivered_msg_id, cancelled_at FROM scheduled_messages WHERE id=?",
            (scheduled_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "not found"}
        if row["delivered_msg_id"] is not None:
            return {"ok": False, "error": "already delivered",
                    "delivered_msg_id": row["delivered_msg_id"]}
        if row["cancelled_at"] is not None:
            return {"ok": True, "already_cancelled_at": row["cancelled_at"]}
        conn.execute(
            "UPDATE scheduled_messages SET cancelled_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (scheduled_id,),
        )
        conn.commit()
        return {"ok": True, "cancelled_id": scheduled_id}
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability stats — used by /health and CLI --stats."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_messages'"
        ).fetchone():
            return {"scheduled_pending": 0, "scheduled_delivered": 0,
                    "scheduled_cancelled": 0, "next_deliver_at": None}
        pending = conn.execute(
            "SELECT COUNT(*) FROM scheduled_messages "
            "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL"
        ).fetchone()[0]
        delivered = conn.execute(
            "SELECT COUNT(*) FROM scheduled_messages WHERE delivered_msg_id IS NOT NULL"
        ).fetchone()[0]
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM scheduled_messages WHERE cancelled_at IS NOT NULL"
        ).fetchone()[0]
        next_row = conn.execute(
            "SELECT MIN(deliver_at) FROM scheduled_messages "
            "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL"
        ).fetchone()
        return {
            "scheduled_pending": pending,
            "scheduled_delivered": delivered,
            "scheduled_cancelled": cancelled,
            "next_deliver_at": next_row[0] if next_row else None,
        }
    finally:
        conn.close()


def format_summary(counters: dict) -> str:
    return f"delivered {counters['delivered']} of {counters['scanned']} pending"
