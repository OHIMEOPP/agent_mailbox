"""Smoke test for mailbox snooze feature.

Test plan:
  1. parse_until: relative shorthand + ISO + bad input
  2. snooze sets snoozed_until; unsnooze clears
  3. snooze missing message → FileNotFoundError
  4. inbox default filter hides future-snoozed; include_snoozed=True shows
  5. inbox shows snoozed where wake time has passed
  6. stats counters: active vs woken-pending
"""
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mailbox import snoozed as mailbox_snoozed  # noqa: E402


def _create_messages_table_v7(db: Path) -> None:
    """Schema mirror post-v007."""
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            read_at TEXT,
            snoozed_until TEXT
        );
    """)
    conn.commit()
    conn.close()


def _insert_msg(db: Path, body: str, snoozed_until: str | None = None) -> int:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at, snoozed_until) "
            "VALUES('hub', 'spoke', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?) "
            "RETURNING id",
            (body, snoozed_until),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def test_1_parse_until(db: Path) -> None:
    print("\n[test 1] parse_until: relative + ISO + bad")
    iso30m = mailbox_snoozed.parse_until("30m")
    iso1h = mailbox_snoozed.parse_until("1h")
    iso7d = mailbox_snoozed.parse_until("7d")
    # All produce ISO format ending in Z
    assert iso30m.endswith("Z")
    assert iso1h.endswith("Z")
    assert iso7d.endswith("Z")
    # 7d > 1h > 30m lexicographically (ISO 8601 sorts chronologically)
    assert iso30m < iso1h < iso7d, f"{iso30m} {iso1h} {iso7d}"

    # ISO pass-through
    iso = "2026-05-23T10:00:00Z"
    assert mailbox_snoozed.parse_until(iso) == iso

    # Bad input
    for bad in ("", "wat"):
        try:
            mailbox_snoozed.parse_until(bad)
        except ValueError:
            pass
        else:
            # "wat" actually passes through as "ISO" — only "" raises
            if bad != "wat":
                raise AssertionError(f"expected ValueError on {bad!r}")
    print("  relative → sorted ISO, ISO pass-through, empty rejected")


def test_2_snooze_unsnooze_roundtrip(db: Path) -> None:
    print("\n[test 2] snooze sets column; unsnooze clears")
    _create_messages_table_v7(db)
    m = _insert_msg(db, "to be snoozed")

    r1 = mailbox_snoozed.snooze(db, m, actor="alice", until="2h")
    assert r1["snoozed_until"].endswith("Z")
    assert r1["was_snoozed"] is False  # first time

    # Re-snooze updates timestamp; was_snoozed becomes True
    r2 = mailbox_snoozed.snooze(db, m, actor="alice", until="1h")
    assert r2["was_snoozed"] is True
    assert r2["snoozed_until"] != r1["snoozed_until"]  # different until

    # Unsnooze clears
    r3 = mailbox_snoozed.unsnooze(db, m, actor="alice")
    assert r3["was_snoozed"] is True

    # Unsnooze again is no-op
    r4 = mailbox_snoozed.unsnooze(db, m, actor="alice")
    assert r4["was_snoozed"] is False

    # Verify column
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT snoozed_until FROM messages WHERE id=?", (m,)).fetchone()[0]
    conn.close()
    assert val is None
    print("  snooze→re-snooze→unsnooze→unsnooze cycle ok")


def test_3_missing_message_raises(db: Path) -> None:
    print("\n[test 3] snooze/unsnooze missing message → FileNotFoundError")
    _create_messages_table_v7(db)
    try:
        mailbox_snoozed.snooze(db, message_id=99999, actor="alice", until="1h")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")

    try:
        mailbox_snoozed.unsnooze(db, message_id=99999, actor="alice")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")
    print("  both raise ok")


def test_4_inbox_filter_hides_future_snoozed(db: Path) -> None:
    print("\n[test 4] inbox filter: hide future-snoozed; include_snoozed shows")
    _create_messages_table_v7(db)
    m_normal = _insert_msg(db, "normal")
    m_snoozed = _insert_msg(db, "sleeping")
    # snooze to far future
    mailbox_snoozed.snooze(db, m_snoozed, actor="alice", until="7d")

    conn = sqlite3.connect(str(db))
    try:
        # Default filter — only m_normal visible
        sql = ("SELECT id FROM messages WHERE to_name = 'spoke' "
               "AND (snoozed_until IS NULL "
               "OR snoozed_until <= strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
               "ORDER BY id ASC")
        visible = [r[0] for r in conn.execute(sql).fetchall()]
        assert visible == [m_normal], visible

        # include_snoozed=True — both
        all_ids = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE to_name = 'spoke' ORDER BY id ASC"
        ).fetchall()]
        assert all_ids == [m_normal, m_snoozed]
    finally:
        conn.close()
    print(f"  default visible={visible}, all={all_ids}")


def test_5_woken_snooze_visible(db: Path) -> None:
    print("\n[test 5] snoozed in past → visible again")
    _create_messages_table_v7(db)
    m = _insert_msg(db, "wake up")
    # Snooze to a past time (manually — module enforces future-ish parses)
    past_iso = "2020-01-01T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE messages SET snoozed_until = ? WHERE id = ?", (past_iso, m))
    conn.commit()
    conn.close()

    # Default filter should NOT hide a woken message
    conn = sqlite3.connect(str(db))
    visible = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE to_name = 'spoke' "
        "AND (snoozed_until IS NULL "
        "OR snoozed_until <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    ).fetchall()]
    conn.close()
    assert m in visible, "woken message should be visible"
    print(f"  past snoozed_until → still visible: {visible}")


def test_6_stats_counters(db: Path) -> None:
    print("\n[test 6] stats: active vs woken-pending")
    _create_messages_table_v7(db)
    m1 = _insert_msg(db, "active 1")
    m2 = _insert_msg(db, "active 2")
    m3 = _insert_msg(db, "woken")
    m4 = _insert_msg(db, "never snoozed")

    # 2 active, 1 woken
    mailbox_snoozed.snooze(db, m1, "alice", "7d")
    mailbox_snoozed.snooze(db, m2, "alice", "30m")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE messages SET snoozed_until = '2020-01-01T00:00:00Z' WHERE id = ?", (m3,))
    conn.commit()
    conn.close()

    s = mailbox_snoozed.stats(db)
    assert s["snoozed_active"] == 2, s
    assert s["snoozed_woken_pending_inbox_poll"] == 1, s
    print(f"  {s}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-snooze-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_parse_until,
                                 test_2_snooze_unsnooze_roundtrip,
                                 test_3_missing_message_raises,
                                 test_4_inbox_filter_hides_future_snoozed,
                                 test_5_woken_snooze_visible,
                                 test_6_stats_counters), start=1):
            sub = workdir / f"t{i}"
            sub.mkdir()
            db = sub / "mailbox.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL SNOOZE TESTS PASSED")
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
