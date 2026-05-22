"""Passive audit log for mailbox operations.

Imported by:
  - mailbox-server.py (REST endpoints log every /send /inbox /mark_read /attachment hit)
  - server.py (MCP tools log every send / inbox / mark_read / download / whoami call)
  - mailbox-audit.py (CLI: --tail / --since / --actor / --action / --stats)

Naming: hyphenless module so Python can `from mailbox_audit import ...`.

Design choices (locked 2026-05-23):
  - Single SQLite table `audit_log` alongside messages/peers/attachments
  - Schema is append-only; no UPDATE/DELETE from app code (retention sweep does its own pruning)
  - `payload_json` is a TEXT blob — variable shape per action, fully self-describing
  - All log_event() calls catch exceptions internally — audit must NEVER break the
    operation it's auditing. Failures stderr-printed, never raised.
  - DDL is `CREATE TABLE IF NOT EXISTS` — same idempotent pattern as messages/peers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Action vocabulary. Keep this list as the canonical set; CLI/--action validates against it.
# When adding actions, also wire them at the call site (server.py / mailbox-server.py).
ACTIONS = frozenset({
    "send",       # any send (text-only or with files)
    "inbox",      # inbox poll
    "mark_read",  # mark messages read
    "download",   # attachment fetched
    "whoami",     # identity probe
    "peers",      # peer list
    "search",     # FTS5 full-text search (added 2026-05-23 by wiki/FTS5)
    "react",      # add a reaction (emoji) to a message
    "unreact",    # remove a reaction
    "rate_limit_rejected",  # request denied by rate limiter (429)
    "pin",        # pin a message (top of inbox, exempt from retention)
    "unpin",      # remove pin
    "snooze",     # hide message from inbox until wake_at time
    "unsnooze",   # remove snooze immediately
    "forward",    # forward an existing message to another recipient
})

DEFAULT_TAIL_LIMIT = 50


def _is_disabled() -> bool:
    """Kill-switch for hot-path audit writes.

    Set MAILBOX_AUDIT_DISABLED=1 to skip all log_event() inserts. Reads (query_audit,
    stats, CLI) are unaffected — they just return empty if the table was never written.
    """
    return os.environ.get("MAILBOX_AUDIT_DISABLED", "").strip() in ("1", "true", "yes")


def init_schema(db_path: Path) -> None:
    """Idempotent DDL — create audit_log table + indexes if missing.

    Safe to call multiple times; safe under concurrent calls (CREATE IF NOT EXISTS).
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                actor        TEXT NOT NULL,
                action       TEXT NOT NULL,
                target       TEXT,
                payload_json TEXT,
                ok           INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
            CREATE INDEX IF NOT EXISTS idx_audit_actor_ts ON audit_log(actor, ts);
            CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_log(action, ts);
        """)
        conn.commit()
    finally:
        conn.close()


def log_event(
    db_path: Path,
    actor: str,
    action: str,
    target: str | None = None,
    payload: dict | None = None,
    ok: bool = True,
) -> None:
    """Insert one audit row. Best-effort — never raises.

    Args:
        db_path: path to mailbox.db
        actor: who did this (e.g. "wiki", "koatag@LAPTOP", or "rest:peer-name")
        action: one of ACTIONS
        target: optional free-form identifier (peer name, message id, attachment id)
        payload: optional dict — JSON-serialized into the column
        ok: True for successful op, False if the op failed (still log for forensics)
    """
    if _is_disabled():
        return
    try:
        payload_str = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute(
                "INSERT INTO audit_log(actor, action, target, payload_json, ok) "
                "VALUES(?, ?, ?, ?, ?)",
                (actor, action, target, payload_str, 1 if ok else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Audit must never break the audited operation. Stderr-log + swallow.
        print(f"[audit] log_event failed: {type(e).__name__}: {e}", file=sys.stderr)


def query_audit(
    db_path: Path,
    since: str | None = None,
    until: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = DEFAULT_TAIL_LIMIT,
    order_desc: bool = True,
) -> list[dict]:
    """Read audit rows with optional filters.

    Args:
        since: ISO timestamp lower bound (exclusive); rows with ts > since
        until: ISO timestamp upper bound (exclusive); rows with ts < until
        actor: exact-match filter
        action: exact-match filter (must be in ACTIONS or returns [])
        limit: max rows returned (default 50)
        order_desc: True (default) = newest first; False = oldest first

    Returns list of {id, ts, actor, action, target, payload, ok}.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list = []
        if since:
            clauses.append("ts > ?")
            params.append(since)
        if until:
            clauses.append("ts < ?")
            params.append(until)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if action:
            clauses.append("action = ?")
            params.append(action)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if order_desc else "ASC"
        sql = f"SELECT id, ts, actor, action, target, payload_json, ok FROM audit_log{where} ORDER BY id {order} LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Table doesn't exist yet — return empty cleanly so callers don't choke
            # on a brand-new db that hasn't called init_schema().
            return []
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"]) if r["payload_json"] else None
            except json.JSONDecodeError:
                payload = {"_raw": r["payload_json"]}
            out.append({
                "id": r["id"], "ts": r["ts"], "actor": r["actor"],
                "action": r["action"], "target": r["target"],
                "payload": payload, "ok": bool(r["ok"]),
            })
        return out
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability stats for /health and CLI --stats.

    Returns: {audit_count, audit_first_at, audit_last_at, by_action: {action: count}}
    """
    if not db_path.exists():
        return {
            "audit_count": 0, "audit_first_at": None, "audit_last_at": None,
            "by_action": {},
        }
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        except sqlite3.OperationalError:
            return {
                "audit_count": 0, "audit_first_at": None, "audit_last_at": None,
                "by_action": {},
            }
        if total == 0:
            return {
                "audit_count": 0, "audit_first_at": None, "audit_last_at": None,
                "by_action": {},
            }
        first_at = conn.execute("SELECT MIN(ts) FROM audit_log").fetchone()[0]
        last_at = conn.execute("SELECT MAX(ts) FROM audit_log").fetchone()[0]
        by_action = {
            row[0]: row[1] for row in conn.execute(
                "SELECT action, COUNT(*) FROM audit_log GROUP BY action"
            ).fetchall()
        }
        return {
            "audit_count": total,
            "audit_first_at": first_at,
            "audit_last_at": last_at,
            "by_action": by_action,
        }
    finally:
        conn.close()


def format_summary(rows: list[dict]) -> str:
    """One-line per-row stderr formatter for CLI tail mode."""
    out = []
    for r in rows:
        ok_marker = "" if r["ok"] else " [FAIL]"
        target = f" target={r['target']}" if r["target"] else ""
        out.append(f"{r['ts']} {r['actor']:<20} {r['action']:<10}{target}{ok_marker}")
    return "\n".join(out)
