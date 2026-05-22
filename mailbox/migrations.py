"""Versioned schema migrations for mailbox.db.

Centralizes the messages-table ALTERs that previously lived inline in
both server.py and mailbox-server.py db_init(). One canonical migration
list, one tracking table, two callers.

Imported by:
  - server.py (`apply()` from _init_db)
  - mailbox-server.py (`apply()` from db_init)
  - smoke_test_migrations.py (verifies fresh / partial / fully-migrated DBs)

Design:
  - `schema_migrations(version, name, applied_at)` tracks which migrations ran.
  - Each migration is a (version, name, fn) tuple. `fn(conn)` does the work.
  - Migrations are idempotent — they introspect state before mutating, so
    running against a fresh DB (where CREATE TABLE already included the
    columns) just marks them applied without ALTER.
  - The CREATE TABLE statement in callers KEEPS the new columns inline. This
    is "belt-and-suspenders": fresh DBs land with the full schema in one shot,
    legacy DBs get caught up by the migrations. Future migrations should
    follow the same pattern — add to the inline CREATE TABLE AND append a
    migration here for legacy upgrade.
  - Indexes are created in the migration (not in CREATE TABLE) — partial
    indexes on freshly-added columns are the original ALTER trap that
    motivated this refactor.

To add a migration:
  1. Append to MIGRATIONS list with next sequential version
  2. Add the column to the inline CREATE TABLE in server.py + mailbox-server.py
  3. Add a smoke case in smoke_test_migrations.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable


def init_schema(db_path: Path) -> None:
    """Create schema_migrations table if missing. Idempotent."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _messages_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}


def _migration_v001_has_attachments(conn: sqlite3.Connection) -> None:
    """messages.has_attachments — flag set when message has attachment rows.

    Added 2026-05-23 cross-device file attachment feature (commit 20dab91).
    Legacy DBs without it get the column with default 0; new attachments
    INSERT updates the flag manually via UPDATE.
    """
    if "has_attachments" not in _messages_columns(conn):
        conn.execute(
            "ALTER TABLE messages ADD COLUMN has_attachments "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migration_v002_in_reply_to(conn: sqlite3.Connection) -> None:
    """messages.in_reply_to + partial index.

    Added 2026-05-23 reply threading feature (commit 1bd2918). Partial index
    skips NULL rows since most messages aren't replies.
    """
    if "in_reply_to" not in _messages_columns(conn):
        conn.execute("ALTER TABLE messages ADD COLUMN in_reply_to INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to "
        "ON messages(in_reply_to) WHERE in_reply_to IS NOT NULL"
    )


def _migration_v003_expires_at(conn: sqlite3.Connection) -> None:
    """messages.expires_at + partial index.

    Added 2026-05-23 TTL feature (commit b73d14a). Partial index covers only
    rows with explicit TTL (most messages have NULL = never expires).
    """
    if "expires_at" not in _messages_columns(conn):
        conn.execute("ALTER TABLE messages ADD COLUMN expires_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_expires_at "
        "ON messages(expires_at) WHERE expires_at IS NOT NULL"
    )


def _migration_v004_claim(conn: sqlite3.Connection) -> None:
    """messages.claimed_by + claimed_until + partial index.

    Added 2026-05-23 message-claim / visibility-timeout feature. Lets one
    agent grab a message for exclusive processing for a TTL window so two
    workers don't pick up the same task. Partial index covers active claims
    only (NULL when unclaimed or claim expired-and-cleared).
    """
    cols = _messages_columns(conn)
    if "claimed_by" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN claimed_by TEXT")
    if "claimed_until" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN claimed_until TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_claimed_until "
        "ON messages(claimed_until) WHERE claimed_until IS NOT NULL"
    )


def _migration_v005_priority(conn: sqlite3.Connection) -> None:
    """messages.priority + partial index.

    Added 2026-05-23 priority-lanes feature. Per-message integer 0..9
    (default 0). Inbox queries order by priority DESC, id ASC so high-priority
    items surface first while FIFO is preserved within a priority band.
    Partial index covers only non-default priorities — most traffic is priority=0.
    """
    if "priority" not in _messages_columns(conn):
        conn.execute(
            "ALTER TABLE messages ADD COLUMN priority "
            "INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_priority "
        "ON messages(priority) WHERE priority > 0"
    )


# Canonical migration list. Append only; never re-order or delete.
# `version` must be sequential starting from 1.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "messages_has_attachments", _migration_v001_has_attachments),
    (2, "messages_in_reply_to_with_partial_index", _migration_v002_in_reply_to),
    (3, "messages_expires_at_with_partial_index", _migration_v003_expires_at),
    (4, "messages_claim_visibility_timeout", _migration_v004_claim),
    (5, "messages_priority_with_partial_index", _migration_v005_priority),
]


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()}
    except sqlite3.OperationalError:
        # Table doesn't exist yet — init_schema hasn't run. Caller should
        # init_schema first; we degrade silently here so callers can do it
        # in either order.
        return set()


def apply(db_path: Path) -> dict:
    """Run all unapplied migrations against `db_path`.

    Each migration is idempotent (introspects current state). If a column
    is already present from the CREATE TABLE inline schema, the ALTER is
    skipped but the migration row is still recorded — so the next boot
    doesn't try again.

    Returns counters: {applied: [(version, name), ...], skipped: [...]}.
    """
    init_schema(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        already = _applied_versions(conn)
        applied: list[tuple[int, str]] = []
        skipped: list[tuple[int, str]] = []
        for version, name, fn in MIGRATIONS:
            if version in already:
                skipped.append((version, name))
                continue
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
                (version, name),
            )
            conn.commit()
            applied.append((version, name))
        return {"applied": applied, "skipped": skipped}
    finally:
        conn.close()


def list_applied(db_path: Path) -> list[dict]:
    """List rows from schema_migrations newest-first. CLI / /health surface."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT version, name, applied_at FROM schema_migrations "
                "ORDER BY version DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability for /health: how many migrations applied + current head."""
    rows = list_applied(db_path)
    if not rows:
        return {
            "schema_migrations_applied": 0,
            "schema_latest_version": None,
            "schema_latest_name": None,
            "schema_total_known": len(MIGRATIONS),
        }
    # rows are DESC by version
    latest = rows[0]
    return {
        "schema_migrations_applied": len(rows),
        "schema_latest_version": latest["version"],
        "schema_latest_name": latest["name"],
        "schema_total_known": len(MIGRATIONS),
    }
