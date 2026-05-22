"""Smoke test for mailbox_migrations.

Test plan:
  1. Fresh DB: init_schema + apply → all migrations recorded as 'applied'
  2. Already-migrated DB: apply twice → second call records 0 applied / N skipped
  3. Legacy DB (no schema_migrations table, only original messages columns)
     → apply runs all ALTERs, records all as applied
  4. Partial-legacy DB (messages already has has_attachments but not the
     others, AND schema_migrations table missing) → migrations 2 + 3 actually
     ALTER; migration 1 is a no-op but still recorded
  5. Indexes from migrations 2 and 3 exist after apply
  6. stats() returns latest version + count
  7. list_applied() returns rows newest-first
"""
import sqlite3
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import mailbox_migrations  # noqa: E402


def _create_full_messages_table(db: Path) -> None:
    """Simulate a fresh CREATE TABLE — all columns inline (as server.py does)."""
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            read_at TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            in_reply_to INTEGER
        );
    """)
    conn.commit()
    conn.close()


def _create_legacy_messages_table(db: Path) -> None:
    """Simulate a pre-2026-05-23 DB — only original messages columns."""
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            read_at TEXT
        );
    """)
    conn.commit()
    conn.close()


def _messages_columns(db: Path) -> set:
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    finally:
        conn.close()


def _indexes_on_messages(db: Path) -> set:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='messages'"
        ).fetchall()}
    finally:
        conn.close()


def test_1_fresh_db_full_schema(db: Path) -> None:
    """Fresh DB has all columns inline → apply marks them all applied."""
    print("\n[test 1] fresh DB with full schema inline → migrations all 'applied'")
    _create_full_messages_table(db)
    result = mailbox_migrations.apply(db)
    assert len(result["applied"]) == len(mailbox_migrations.MIGRATIONS), \
        f"expected {len(mailbox_migrations.MIGRATIONS)} applied, got {result}"
    assert result["skipped"] == [], f"unexpected skips: {result}"
    # Indexes from v002 and v003 should exist (migrations create them
    # regardless of column presence — they were not in the inline CREATE)
    idx = _indexes_on_messages(db)
    assert "idx_messages_in_reply_to" in idx, f"missing v002 index: {idx}"
    assert "idx_messages_expires_at" in idx, f"missing v003 index: {idx}"
    print(f"  applied {len(result['applied'])} migrations, "
          f"indexes ok: {sorted(idx - {'sqlite_autoindex_messages_1'})}")


def test_2_double_apply_idempotent(db: Path) -> None:
    """Second apply records 0 applied, all skipped."""
    print("\n[test 2] apply twice → second call is 0 applied / N skipped")
    _create_full_messages_table(db)
    first = mailbox_migrations.apply(db)
    second = mailbox_migrations.apply(db)
    assert second["applied"] == [], f"second apply should skip everything: {second}"
    assert len(second["skipped"]) == len(mailbox_migrations.MIGRATIONS)
    print(f"  first: {len(first['applied'])} applied | "
          f"second: 0 applied + {len(second['skipped'])} skipped")


def test_3_legacy_db_full_replay(db: Path) -> None:
    """Legacy DB (no new columns, no schema_migrations) → all ALTERs run."""
    print("\n[test 3] legacy DB → migrations run all ALTERs")
    _create_legacy_messages_table(db)
    pre_cols = _messages_columns(db)
    assert "has_attachments" not in pre_cols
    assert "in_reply_to" not in pre_cols
    assert "expires_at" not in pre_cols

    result = mailbox_migrations.apply(db)
    assert len(result["applied"]) == len(mailbox_migrations.MIGRATIONS)
    post_cols = _messages_columns(db)
    assert "has_attachments" in post_cols
    assert "in_reply_to" in post_cols
    assert "expires_at" in post_cols

    idx = _indexes_on_messages(db)
    assert "idx_messages_in_reply_to" in idx
    assert "idx_messages_expires_at" in idx
    print(f"  added 3 columns + 2 partial indexes to legacy DB")


def test_4_partial_legacy_db(db: Path) -> None:
    """Mid-migration DB: has v001 column already (some other path) but no
    schema_migrations table — apply should record v001 as applied without
    re-ALTER, then run v002 and v003 fresh."""
    print("\n[test 4] partial-legacy DB → v001 no-op recorded, v002+v003 ALTER")
    _create_legacy_messages_table(db)
    # Manually add has_attachments (simulate old inline ALTER from pre-migration code)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE messages ADD COLUMN has_attachments INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()

    result = mailbox_migrations.apply(db)
    # All registered migrations should be recorded as applied (v001 is a no-op
    # for the pre-existing column but still tracked).
    expected_n = len(mailbox_migrations.MIGRATIONS)
    assert len(result["applied"]) == expected_n
    assert {v for v, _ in result["applied"]} == {
        v for v, _, _ in mailbox_migrations.MIGRATIONS
    }

    # All migration-added columns should exist post-apply
    cols = _messages_columns(db)
    for c in ("has_attachments", "in_reply_to", "expires_at"):
        assert c in cols, f"missing column {c} after migration"

    # Second apply is a no-op
    second = mailbox_migrations.apply(db)
    assert second["applied"] == []
    print(f"  v001 idempotent on pre-existing column, {expected_n - 1} other migrations ALTERed cleanly")


def test_5_stats_and_list_applied(db: Path) -> None:
    """stats() reports head + count; list_applied returns newest-first."""
    print("\n[test 5] stats() and list_applied()")
    s_empty = mailbox_migrations.stats(db)
    assert s_empty["schema_migrations_applied"] == 0
    assert s_empty["schema_latest_version"] is None
    assert s_empty["schema_total_known"] == len(mailbox_migrations.MIGRATIONS)

    _create_full_messages_table(db)
    mailbox_migrations.apply(db)
    s = mailbox_migrations.stats(db)
    assert s["schema_migrations_applied"] == len(mailbox_migrations.MIGRATIONS)
    expected_head = mailbox_migrations.MIGRATIONS[-1][0]
    assert s["schema_latest_version"] == expected_head, \
        f"head should be {expected_head}, got {s}"
    assert s["schema_latest_name"] == mailbox_migrations.MIGRATIONS[-1][1]

    rows = mailbox_migrations.list_applied(db)
    # newest-first ordering
    versions = [r["version"] for r in rows]
    assert versions == sorted(versions, reverse=True), \
        f"list_applied not desc-ordered: {versions}"
    print(f"  head v{s['schema_latest_version']} = {s['schema_latest_name']}; "
          f"list desc ok")


def test_6_indexes_idempotent_across_runs(db: Path) -> None:
    """Re-running migrations doesn't error on existing partial indexes."""
    print("\n[test 6] partial-index CREATE IF NOT EXISTS robust across re-apply")
    _create_full_messages_table(db)
    for _ in range(3):
        result = mailbox_migrations.apply(db)
        # First run applies, subsequent runs skip — but the index-create
        # statements run unconditionally inside each migration to handle
        # legacy DBs that have the column but not the index. Re-running
        # against a DB where index already exists must not throw.
    idx = _indexes_on_messages(db)
    assert "idx_messages_in_reply_to" in idx
    assert "idx_messages_expires_at" in idx
    print(f"  ran apply() 3x; indexes stable, no IF-NOT-EXISTS race")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-migrations-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_fresh_db_full_schema,
                                 test_2_double_apply_idempotent,
                                 test_3_legacy_db_full_replay,
                                 test_4_partial_legacy_db,
                                 test_5_stats_and_list_applied,
                                 test_6_indexes_idempotent_across_runs), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL MIGRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(3)
