"""Smoke test for mute_peer feature.

Test plan:
  1. init_schema idempotent + UNIQUE constraint shape
  2. mute idempotent (re-mute returns was_already_muted=True)
  3. unmute returns was_muted accurately
  4. list_mutes per-actor isolation (alice's list != bob's list)
  5. stats counters
  6. inbox filter sketch: NOT IN (muted_list) hides matching from_name
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mailbox import mute as mailbox_mute  # noqa: E402


def _seed_messages(db: Path, rows: list[tuple[str, str, str]]) -> None:
    """Insert messages without going through migrations — minimal schema."""
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            read_at TEXT
        )
    """)
    for fn, tn, body in rows:
        conn.execute(
            "INSERT INTO messages(from_name, to_name, body) VALUES(?, ?, ?)",
            (fn, tn, body),
        )
    conn.commit()
    conn.close()


def test_1_ddl_idempotent(db: Path) -> None:
    print("\n[test 1] init_schema idempotent + UNIQUE constraint")
    mailbox_mute.init_schema(db)
    mailbox_mute.init_schema(db)  # no-op

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "mutes" in tables
        # Test UNIQUE directly via duplicate INSERT
        conn.execute("INSERT INTO mutes(actor, muted_peer) VALUES('alice', 'koatag')")
        try:
            conn.execute("INSERT INTO mutes(actor, muted_peer) VALUES('alice', 'koatag')")
            raise AssertionError("UNIQUE didn't fire")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
    print("  table + UNIQUE(actor, muted_peer) ok")


def test_2_mute_idempotent(db: Path) -> None:
    print("\n[test 2] mute idempotency")
    mailbox_mute.init_schema(db)
    r1 = mailbox_mute.mute(db, actor="alice", peer="koatag")
    assert r1["muted"] is True
    assert r1["was_already_muted"] is False

    r2 = mailbox_mute.mute(db, actor="alice", peer="koatag")
    assert r2["muted"] is True
    assert r2["was_already_muted"] is True

    # Different actor for same peer is independent
    r3 = mailbox_mute.mute(db, actor="bob", peer="koatag")
    assert r3["was_already_muted"] is False
    print("  alice mutes koatag (new), re-mute (was already), bob mutes koatag (new)")


def test_3_unmute_returns_was_muted(db: Path) -> None:
    print("\n[test 3] unmute reports was_muted accurately")
    mailbox_mute.init_schema(db)
    mailbox_mute.mute(db, "alice", "koatag")

    r1 = mailbox_mute.unmute(db, "alice", "koatag")
    assert r1["muted"] is False
    assert r1["was_muted"] is True

    r2 = mailbox_mute.unmute(db, "alice", "koatag")  # already unmuted
    assert r2["was_muted"] is False

    r3 = mailbox_mute.unmute(db, "alice", "never-existed")  # never muted
    assert r3["was_muted"] is False
    print("  was_muted: True / False / False ok")


def test_4_list_mutes_per_actor(db: Path) -> None:
    print("\n[test 4] per-actor mute list isolation")
    mailbox_mute.init_schema(db)
    mailbox_mute.mute(db, "alice", "koatag")
    mailbox_mute.mute(db, "alice", "stranger-conv")
    mailbox_mute.mute(db, "bob", "wiki")

    alice_list = mailbox_mute.list_mutes(db, "alice")
    bob_list = mailbox_mute.list_mutes(db, "bob")
    eve_list = mailbox_mute.list_mutes(db, "eve")  # no mutes

    assert alice_list == ["koatag", "stranger-conv"], alice_list
    assert bob_list == ["wiki"], bob_list
    assert eve_list == []
    print(f"  alice={alice_list}, bob={bob_list}, eve={eve_list}")


def test_5_stats(db: Path) -> None:
    print("\n[test 5] stats counters")
    mailbox_mute.init_schema(db)
    s0 = mailbox_mute.stats(db)
    assert s0 == {"mute_count": 0, "muting_actors": 0}

    mailbox_mute.mute(db, "alice", "koatag")
    mailbox_mute.mute(db, "alice", "stranger-conv")
    mailbox_mute.mute(db, "bob", "wiki")
    mailbox_mute.mute(db, "bob", "koatag")  # bob also mutes koatag

    s = mailbox_mute.stats(db)
    assert s["mute_count"] == 4, s
    assert s["muting_actors"] == 2, s
    print(f"  {s}")


def test_6_inbox_filter(db: Path) -> None:
    print("\n[test 6] inbox NOT IN (muted_list) filter sketch")
    mailbox_mute.init_schema(db)
    _seed_messages(db, [
        ("wiki", "alice", "from wiki"),
        ("koatag", "alice", "from koatag (will be muted)"),
        ("stranger-conv", "alice", "from stranger"),
        ("koatag", "bob", "to bob, alice doesn't see"),
    ])

    # alice mutes koatag
    mailbox_mute.mute(db, "alice", "koatag")
    muted = mailbox_mute.list_mutes(db, "alice")
    assert muted == ["koatag"]

    # Inbox query with NOT IN filter (mirrors server-side logic)
    placeholders = ",".join("?" * len(muted))
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            f"SELECT from_name, body FROM messages "
            f"WHERE to_name = 'alice' AND from_name NOT IN ({placeholders}) "
            f"ORDER BY id ASC",
            muted,
        ).fetchall()
    finally:
        conn.close()
    bodies = [r[1] for r in rows]
    assert bodies == ["from wiki", "from stranger"], bodies

    # Without filter (include_muted=True) — all 3 alice mails
    conn = sqlite3.connect(str(db))
    try:
        all_alice = [r[0] for r in conn.execute(
            "SELECT body FROM messages WHERE to_name='alice' ORDER BY id ASC"
        ).fetchall()]
    finally:
        conn.close()
    assert len(all_alice) == 3
    print(f"  filtered={bodies}, all={len(all_alice)} (include_muted=True)")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-mute-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_ddl_idempotent,
                                 test_2_mute_idempotent,
                                 test_3_unmute_returns_was_muted,
                                 test_4_list_mutes_per_actor,
                                 test_5_stats,
                                 test_6_inbox_filter), start=1):
            sub = workdir / f"t{i}"
            sub.mkdir()
            db = sub / "mailbox.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL MUTE TESTS PASSED")
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
