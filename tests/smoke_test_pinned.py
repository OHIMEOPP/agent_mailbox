"""Smoke test for mailbox pin/unpin feature.

Test plan:
  1. pin idempotency — pinning twice returns was_already_pinned=True
  2. unpin idempotency — unpinning unpinned returns was_pinned=False
  3. pin missing message → FileNotFoundError
  4. list_pinned filters + ordering
  5. stats counters
  6. ORDER BY pinned DESC, priority DESC works (pinned beats high-priority)
  7. retention sweep skips pinned messages

Run from repo root or tests/ — sibling-path math handles both.
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Add repo root so `from mailbox import ...` works regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mailbox import pinned as mailbox_pinned  # noqa: E402
from mailbox import sweep as mailbox_sweep  # noqa: E402


def _create_messages_table_v6(db: Path) -> None:
    """Simulate the post-v006 schema with all message columns."""
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
            priority INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE peers (name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL);
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime TEXT,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
    """)
    conn.commit()
    conn.close()


def _insert_msg(db: Path, body: str, days_old: int = 0, read: bool = False,
                priority: int = 0, pinned: bool = False) -> int:
    conn = sqlite3.connect(str(db))
    try:
        sent_at = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{days_old} days",),
        ).fetchone()[0]
        read_at = sent_at if read else None
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at, read_at, priority, pinned) "
            "VALUES('hub', 'spoke', ?, ?, ?, ?, ?) RETURNING id",
            (body, sent_at, read_at, priority, 1 if pinned else 0),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def test_1_pin_idempotent(db: Path) -> None:
    print("\n[test 1] pin idempotency")
    _create_messages_table_v6(db)
    m = _insert_msg(db, "to be pinned")

    r1 = mailbox_pinned.pin(db, m, actor="alice")
    assert r1["pinned"] is True
    assert r1["was_already_pinned"] is False

    r2 = mailbox_pinned.pin(db, m, actor="bob")  # different actor — still no-op
    assert r2["pinned"] is True
    assert r2["was_already_pinned"] is True

    # Verify DB
    conn = sqlite3.connect(str(db))
    try:
        pinned_val = conn.execute("SELECT pinned FROM messages WHERE id=?", (m,)).fetchone()[0]
        assert pinned_val == 1
    finally:
        conn.close()
    print("  first pin → True/False, second pin → True/True ok")


def test_2_unpin_idempotent(db: Path) -> None:
    print("\n[test 2] unpin idempotency")
    _create_messages_table_v6(db)
    m = _insert_msg(db, "not pinned", pinned=False)
    m2 = _insert_msg(db, "is pinned", pinned=True)

    r1 = mailbox_pinned.unpin(db, m, actor="alice")  # was unpinned
    assert r1["pinned"] is False
    assert r1["was_pinned"] is False

    r2 = mailbox_pinned.unpin(db, m2, actor="alice")  # was pinned
    assert r2["pinned"] is False
    assert r2["was_pinned"] is True

    r3 = mailbox_pinned.unpin(db, m2, actor="bob")  # now unpinned again
    assert r3["was_pinned"] is False
    print("  unpin on unpinned/pinned/already-unpinned all idempotent ok")


def test_3_missing_message_raises(db: Path) -> None:
    print("\n[test 3] pin/unpin missing message raises")
    _create_messages_table_v6(db)

    try:
        mailbox_pinned.pin(db, message_id=99999, actor="alice")
    except FileNotFoundError as e:
        assert "99999" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError for missing message")

    try:
        mailbox_pinned.unpin(db, message_id=99999, actor="alice")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for missing message")
    print("  pin/unpin both raise FileNotFoundError for missing id")


def test_4_list_pinned(db: Path) -> None:
    print("\n[test 4] list_pinned filter + ordering")
    _create_messages_table_v6(db)
    m1 = _insert_msg(db, "ref-1", pinned=True)
    m2 = _insert_msg(db, "ref-2", pinned=True)
    _insert_msg(db, "normal", pinned=False)  # excluded
    m3 = _insert_msg(db, "ref-3", pinned=True)

    rows = mailbox_pinned.list_pinned(db)
    ids = [r["id"] for r in rows]
    # Newest first (DESC id)
    assert ids == [m3, m2, m1], ids
    print(f"  3 pinned, ordered newest-first: {ids}")


def test_5_stats(db: Path) -> None:
    print("\n[test 5] stats counters")
    _create_messages_table_v6(db)
    s0 = mailbox_pinned.stats(db)
    assert s0 == {"pinned_count": 0, "pinned_recipients": 0}

    # Insert to different recipients (hard-coded helper sends all to 'spoke';
    # use direct INSERT for variety)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        INSERT INTO messages(from_name, to_name, body, sent_at, pinned)
            VALUES('a', 'x', 'pinned-x-1', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 1);
        INSERT INTO messages(from_name, to_name, body, sent_at, pinned)
            VALUES('a', 'x', 'pinned-x-2', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 1);
        INSERT INTO messages(from_name, to_name, body, sent_at, pinned)
            VALUES('a', 'y', 'pinned-y-1', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 1);
        INSERT INTO messages(from_name, to_name, body, sent_at, pinned)
            VALUES('a', 'z', 'unpinned', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 0);
    """)
    conn.commit()
    conn.close()

    s = mailbox_pinned.stats(db)
    assert s["pinned_count"] == 3, s
    assert s["pinned_recipients"] == 2, s  # x and y
    print(f"  {s}")


def test_6_order_by_pinned_then_priority(db: Path) -> None:
    print("\n[test 6] ORDER BY pinned DESC, priority DESC, id ASC")
    _create_messages_table_v6(db)
    m1 = _insert_msg(db, "normal low", priority=0)
    m2 = _insert_msg(db, "very urgent", priority=9)  # high priority, not pinned
    m3 = _insert_msg(db, "pinned low", priority=1, pinned=True)
    m4 = _insert_msg(db, "pinned high", priority=8, pinned=True)
    m5 = _insert_msg(db, "normal mid", priority=5)

    conn = sqlite3.connect(str(db))
    try:
        rows = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE read_at IS NULL "
            "ORDER BY pinned DESC, priority DESC, id ASC"
        ).fetchall()]
    finally:
        conn.close()
    # Expected: pinned high (m4) > pinned low (m3) > urgent (m2) > mid (m5) > low (m1)
    expected = [m4, m3, m2, m5, m1]
    assert rows == expected, f"order mismatch: {rows} expected {expected}"
    print(f"  pinned beats priority: {rows}")


def test_7_retention_skips_pinned(db: Path) -> None:
    print("\n[test 7] retention sweep skips pinned messages")
    _create_messages_table_v6(db)
    attachments = db.parent / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)

    # Default cutoffs: read_days=7, unread_days=14
    m_pin_old_read = _insert_msg(db, "pinned 30d read", days_old=30, read=True, pinned=True)
    m_pin_old_unread = _insert_msg(db, "pinned 30d unread", days_old=30, read=False, pinned=True)
    m_unpin_old_read = _insert_msg(db, "normal 30d read", days_old=30, read=True, pinned=False)
    m_unpin_old_unread = _insert_msg(db, "normal 30d unread", days_old=30, read=False, pinned=False)
    m_fresh = _insert_msg(db, "fresh", days_old=0, read=False, pinned=False)

    counters = mailbox_sweep.sweep_all(db, attachments)
    # 2 normal-and-old should be deleted; pinned ones survive; fresh survives
    assert counters["read_messages_deleted"] == 1, counters
    assert counters["unread_messages_deleted"] == 1, counters

    conn = sqlite3.connect(str(db))
    try:
        surviving = {r[0] for r in conn.execute("SELECT id FROM messages")}
    finally:
        conn.close()
    assert m_pin_old_read in surviving, "pinned read message should survive"
    assert m_pin_old_unread in surviving, "pinned unread message should survive"
    assert m_unpin_old_read not in surviving
    assert m_unpin_old_unread not in surviving
    assert m_fresh in surviving
    print(f"  pinned old read+unread survived; unpinned old deleted; fresh kept")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-pinned-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_pin_idempotent,
                                 test_2_unpin_idempotent,
                                 test_3_missing_message_raises,
                                 test_4_list_pinned,
                                 test_5_stats,
                                 test_6_order_by_pinned_then_priority,
                                 test_7_retention_skips_pinned), start=1):
            sub = workdir / f"t{i}"
            sub.mkdir()
            db = sub / "mailbox.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL PINNED TESTS PASSED")
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
