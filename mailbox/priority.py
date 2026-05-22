"""Priority lanes for mailbox messages.

Per-message integer priority 0..9 (default 0). Higher = more urgent. Inbox
queries order by priority DESC then id ASC so high-priority items surface
first while preserving FIFO within a priority band.

Thin module — most of the work lives in inline SQL (server.py/mailbox-server.py
ORDER BY clauses) and the send() param. This module owns:
  - Priority validation (parse_priority)
  - Human label (priority_label) for CLI / dump rendering
  - /health stats (priority distribution across unread)

The schema column itself is added by mailbox_migrations.MIGRATIONS v005;
this module assumes that migration has run.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

PRIORITY_MIN = 0
PRIORITY_MAX = 9
PRIORITY_DEFAULT = 0

# Bucket boundaries for /health distribution and dump rendering.
# 0 = normal, 1-3 = elevated, 4-6 = high, 7-9 = critical.
BUCKETS = [
    ("normal", 0, 0),
    ("elevated", 1, 3),
    ("high", 4, 6),
    ("critical", 7, 9),
]


def parse_priority(value) -> int:
    """Coerce + validate a priority int. Accepts int, str digit, None.

    Raises ValueError if out of range or non-int. Returns PRIORITY_DEFAULT on
    None / empty string for caller convenience (matches the "no priority field
    sent" wire path).
    """
    if value is None or value == "":
        return PRIORITY_DEFAULT
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"priority must be int 0..{PRIORITY_MAX}, got {value!r}")
    if n < PRIORITY_MIN or n > PRIORITY_MAX:
        raise ValueError(f"priority must be 0..{PRIORITY_MAX}, got {n}")
    return n


def priority_label(p: int) -> str:
    """Human-readable bucket name for a numeric priority. Use in dump output."""
    for label, lo, hi in BUCKETS:
        if lo <= p <= hi:
            return label
    return "unknown"


def stats(db_path: Path) -> dict:
    """Distribution of priorities across UNREAD messages.

    Returns {priority_unread_total, priority_buckets: {label: count}}.
    Degrades silently to zeros on pre-migration DBs that lack the column.
    """
    out = {
        "priority_unread_total": 0,
        "priority_buckets": {label: 0 for label, _, _ in BUCKETS},
    }
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE read_at IS NULL"
            ).fetchone()
            out["priority_unread_total"] = row[0]
        except sqlite3.OperationalError:
            return out
        # Bucket counts — partial scans, each predicate constant
        try:
            for label, lo, hi in BUCKETS:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE read_at IS NULL AND priority BETWEEN ? AND ?",
                    (lo, hi),
                ).fetchone()[0]
                out["priority_buckets"][label] = cnt
        except sqlite3.OperationalError:
            # priority column not yet migrated — return totals but empty buckets
            pass
        return out
    finally:
        conn.close()
