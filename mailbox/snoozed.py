"""Inbox snooze for mailbox messages.

A snoozed message is hidden from default `inbox()` until `snoozed_until`
elapses, then it pops back up. Use case: "deal with this in 1 hour" /
"remind me tomorrow" without re-sending.

Behavior contracts:
  - inbox() default: filter out rows where snoozed_until > now
  - inbox(include_snoozed=True): show all
  - mark_read does NOT auto-unsnooze (different concerns: snooze hides,
    mark_read flags processed). Snoozed-and-read messages sleep + then
    appear again as read.
  - Retention sweep does NOT exempt snoozed messages — TTL / read-window
    still applies. Use pin for "keep around forever".
  - Snooze + claim: independent. Claim still locks; snooze hides for the
    snoozing actor's view. (Snooze is per-message global, not per-actor;
    keep schema simple.)

Schema column added by mailbox.migrations v007:
  ALTER TABLE messages ADD COLUMN snoozed_until TEXT;
  CREATE INDEX idx_messages_snoozed ON messages(snoozed_until)
    WHERE snoozed_until IS NOT NULL;

Wake time accepts ISO 8601 or relative shorthand (`30m`, `1h`, `7d`),
parsed via parse_until().
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_RELATIVE = re.compile(r"^(\d+)([mhd])$")


def parse_until(value: str) -> str:
    """Parse a `snooze` until value into an ISO 8601 UTC string.

    Accepts:
      - ISO 8601 (`2026-05-23T01:00:00Z`) — passed through (sqlite lex compare OK)
      - Relative shorthand (`30m`, `1h`, `7d`) — computed from now in UTC

    Raises ValueError on bad input.
    """
    if not value:
        raise ValueError("snooze 'until' is required")
    m = _RELATIVE.match(value)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "m":
            delta = timedelta(minutes=n)
        elif unit == "h":
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        ts = datetime.now(timezone.utc) + delta
        return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
    # Assume ISO — lex-compare in SQL works for sorted ISO strings.
    return value


def snooze(db_path: Path, message_id: int, actor: str, until: str) -> dict:
    """Snooze a message until the given time.

    Returns {snoozed_until, id, actor, was_snoozed}.
    Raises FileNotFoundError if message_id missing, ValueError on bad until.
    """
    resolved_until = parse_until(until)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT snoozed_until FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"message {message_id} not found")
        was_snoozed = row["snoozed_until"] is not None
        conn.execute(
            "UPDATE messages SET snoozed_until = ? WHERE id = ?",
            (resolved_until, message_id),
        )
        conn.commit()
        return {
            "snoozed_until": resolved_until,
            "id": message_id,
            "actor": actor,
            "was_snoozed": was_snoozed,
        }
    finally:
        conn.close()


def unsnooze(db_path: Path, message_id: int, actor: str) -> dict:
    """Clear the snooze on a message. Returns {id, actor, was_snoozed}."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT snoozed_until FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"message {message_id} not found")
        was_snoozed = row["snoozed_until"] is not None
        if was_snoozed:
            conn.execute(
                "UPDATE messages SET snoozed_until = NULL WHERE id = ?",
                (message_id,),
            )
            conn.commit()
        return {
            "id": message_id,
            "actor": actor,
            "was_snoozed": was_snoozed,
        }
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability — counts of active snoozes.

    Returns {snoozed_active: int, snoozed_woken_pending_inbox_poll: int}.
    Active = snoozed_until > now. Woken = snoozed_until set but <= now
    (message visible again on next inbox poll, but column still set).
    """
    out = {"snoozed_active": 0, "snoozed_woken_pending_inbox_poll": 0}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            out["snoozed_active"] = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE snoozed_until > strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
            out["snoozed_woken_pending_inbox_poll"] = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE snoozed_until IS NOT NULL "
                "AND snoozed_until <= strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()
