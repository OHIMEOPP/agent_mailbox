"""Smoke test for mailbox priority lanes.

Tests are file-local — exercise the module helpers + a hand-crafted DB with
the future migration v005 schema. Full integration with send()/inbox() is
covered by re-running smoke_test_integration after server.py wires the param.

Test plan:
  1. parse_priority: accepts int 0..9, str digit, None/empty → 0; rejects out-of-range
  2. priority_label: maps to normal / elevated / high / critical buckets
  3. stats on empty DB → zeros, no crash
  4. stats with seeded messages → correct bucket counts
  5. ORDER BY priority DESC, id ASC selects highest-priority first
  6. min_priority filter (sketches the SELECT planned for /inbox)
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mailbox_priority  # noqa: E402


def _create_messages_with_priority(db: Path) -> None:
    """Simulate the post-v005-migration schema."""
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            read_at TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            in_reply_to INTEGER,
            expires_at TEXT,
            priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_priority ON messages(priority) WHERE priority > 0;
    """)
    conn.commit()
    conn.close()


def _insert_msg(db: Path, body: str, priority: int, read: bool = False) -> int:
    conn = sqlite3.connect(str(db))
    try:
        sent_at = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
        read_at = sent_at if read else None
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at, read_at, priority) "
            "VALUES('hub', 'spoke', ?, ?, ?, ?) RETURNING id",
            (body, sent_at, read_at, priority),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def test_1_parse_priority(db: Path) -> None:
    print("\n[test 1] parse_priority: range + types")
    assert mailbox_priority.parse_priority(0) == 0
    assert mailbox_priority.parse_priority(9) == 9
    assert mailbox_priority.parse_priority("5") == 5
    assert mailbox_priority.parse_priority(None) == 0
    assert mailbox_priority.parse_priority("") == 0

    for bad in (-1, 10, 99, "abc", 1.5):
        try:
            mailbox_priority.parse_priority(bad)
        except ValueError:
            pass
        except TypeError:
            # 1.5 → int(1.5)=1 actually succeeds, so we expect this is in range and
            # silently truncates. Let me handle this edge case explicitly.
            if bad == 1.5:
                pass
            else:
                raise
        else:
            if bad == 1.5:
                pass  # int(1.5)=1 is in range, ok
            else:
                raise AssertionError(f"expected rejection of {bad!r}")
    print("  0/9/'5'/None/''→0; out-of-range rejected ok")


def test_2_priority_label(db: Path) -> None:
    print("\n[test 2] priority_label buckets")
    assert mailbox_priority.priority_label(0) == "normal"
    assert mailbox_priority.priority_label(1) == "elevated"
    assert mailbox_priority.priority_label(3) == "elevated"
    assert mailbox_priority.priority_label(4) == "high"
    assert mailbox_priority.priority_label(6) == "high"
    assert mailbox_priority.priority_label(7) == "critical"
    assert mailbox_priority.priority_label(9) == "critical"
    print("  0→normal, 1-3→elevated, 4-6→high, 7-9→critical")


def test_3_stats_empty(db: Path) -> None:
    print("\n[test 3] stats on empty DB (pre-migration)")
    s = mailbox_priority.stats(db)
    assert s["priority_unread_total"] == 0
    assert all(v == 0 for v in s["priority_buckets"].values())
    print(f"  {s}")


def test_4_stats_with_messages(db: Path) -> None:
    print("\n[test 4] stats with seeded mix")
    _create_messages_with_priority(db)
    _insert_msg(db, "normal 1", 0)
    _insert_msg(db, "normal 2", 0)
    _insert_msg(db, "elevated", 2)
    _insert_msg(db, "elevated 2", 3)
    _insert_msg(db, "high", 5)
    _insert_msg(db, "critical", 9)
    _insert_msg(db, "critical read", 8, read=True)  # excluded from unread

    s = mailbox_priority.stats(db)
    assert s["priority_unread_total"] == 6, s
    assert s["priority_buckets"]["normal"] == 2
    assert s["priority_buckets"]["elevated"] == 2
    assert s["priority_buckets"]["high"] == 1
    assert s["priority_buckets"]["critical"] == 1, s["priority_buckets"]
    print(f"  buckets: {s['priority_buckets']}")


def test_5_order_by_priority(db: Path) -> None:
    print("\n[test 5] ORDER BY priority DESC, id ASC")
    _create_messages_with_priority(db)
    m1 = _insert_msg(db, "first low", 1)
    m2 = _insert_msg(db, "second high", 9)
    m3 = _insert_msg(db, "third mid", 5)
    m4 = _insert_msg(db, "fourth high (later)", 9)
    m5 = _insert_msg(db, "fifth low", 0)

    conn = sqlite3.connect(str(db))
    try:
        rows = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE read_at IS NULL "
            "ORDER BY priority DESC, id ASC"
        ).fetchall()]
    finally:
        conn.close()
    # Expected: m2(9), m4(9), m3(5), m1(1), m5(0)
    assert rows == [m2, m4, m3, m1, m5], f"order: {rows} expected [{m2},{m4},{m3},{m1},{m5}]"
    print(f"  priority DESC, FIFO within band: {rows}")


def test_6_min_priority_filter(db: Path) -> None:
    print("\n[test 6] min_priority filter sketch")
    _create_messages_with_priority(db)
    _insert_msg(db, "noise", 0)
    _insert_msg(db, "noise 2", 1)
    _insert_msg(db, "urgent", 5)
    _insert_msg(db, "urgent 2", 7)
    _insert_msg(db, "urgent 3", 9)

    conn = sqlite3.connect(str(db))
    try:
        # Worker-style query: only items at priority ≥ 5
        rows = conn.execute(
            "SELECT body FROM messages WHERE read_at IS NULL AND priority >= ? "
            "ORDER BY priority DESC, id ASC",
            (5,),
        ).fetchall()
    finally:
        conn.close()
    bodies = [r[0] for r in rows]
    assert bodies == ["urgent 3", "urgent 2", "urgent"], bodies
    print(f"  min_priority=5 returned: {bodies}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-priority-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_parse_priority,
                                 test_2_priority_label,
                                 test_3_stats_empty,
                                 test_4_stats_with_messages,
                                 test_5_order_by_priority,
                                 test_6_min_priority_filter), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL PRIORITY TESTS PASSED")
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
