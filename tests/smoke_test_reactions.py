"""Smoke test for mailbox reactions.

Test plan:
  1. init_schema is idempotent + table+indexes shape
  2. react adds rows; UNIQUE constraint dedupes; returns added=False on 2nd
  3. unreact removes; returns 0 when no match
  4. list_for_messages batches reactions per message_id, missing keys are []
  5. stats counters
  6. emoji length validation (ValueError on empty or >MAX_EMOJI_LEN)
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from mailbox import reactions as mailbox_reactions  # noqa: E402


def test_1_ddl_idempotent(db: Path) -> None:
    print("\n[test 1] init_schema idempotent + table/indexes shape")
    mailbox_reactions.init_schema(db)
    mailbox_reactions.init_schema(db)  # no-op
    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "reactions" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reactions)")}
        assert {"id", "message_id", "actor", "emoji", "created_at"}.issubset(cols)
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='reactions'"
        )}
        # UNIQUE creates an auto-index. Plus our 2 explicit ones.
        assert "idx_reactions_message" in idx
        assert "idx_reactions_actor" in idx
    finally:
        conn.close()
    print("  table + 2 explicit indexes + auto UNIQUE index ok")


def test_2_react_dedup(db: Path) -> None:
    print("\n[test 2] react adds; UNIQUE constraint dedups; added flag")
    mailbox_reactions.init_schema(db)

    r1 = mailbox_reactions.react(db, message_id=1, actor="wiki", emoji="👍")
    assert r1["added"] is True
    assert r1["id"] >= 1

    r2 = mailbox_reactions.react(db, message_id=1, actor="wiki", emoji="👍")
    assert r2["added"] is False, f"second add should be no-op: {r2}"
    assert r2["id"] == r1["id"], "should report the existing row's id"

    # Different actor, same msg + emoji → new row
    r3 = mailbox_reactions.react(db, message_id=1, actor="koatag", emoji="👍")
    assert r3["added"] is True
    assert r3["id"] > r1["id"]

    # Same actor, same msg, different emoji → new row
    r4 = mailbox_reactions.react(db, message_id=1, actor="wiki", emoji="🔥")
    assert r4["added"] is True

    # Different msg → new row
    r5 = mailbox_reactions.react(db, message_id=2, actor="wiki", emoji="👍")
    assert r5["added"] is True
    print(f"  wrote 4 rows, deduped 1 — final count via stats:")
    s = mailbox_reactions.stats(db)
    assert s["reaction_count"] == 4, f"expected 4 reactions, got {s}"
    assert s["reaction_unique_emojis"] == 2  # 👍 and 🔥


def test_3_unreact(db: Path) -> None:
    print("\n[test 3] unreact removes; returns 0 on miss")
    mailbox_reactions.init_schema(db)
    mailbox_reactions.react(db, 1, "wiki", "👍")
    mailbox_reactions.react(db, 1, "koatag", "👍")

    assert mailbox_reactions.unreact(db, 1, "wiki", "👍") == 1
    assert mailbox_reactions.unreact(db, 1, "wiki", "👍") == 0, \
        "second unreact should be no-op"
    assert mailbox_reactions.unreact(db, 1, "ghost", "👻") == 0, \
        "nonexistent reaction should return 0"

    # Verify koatag's reaction still there
    rows = mailbox_reactions.list_for_messages(db, [1])
    assert len(rows[1]) == 1 and rows[1][0]["actor"] == "koatag"
    print("  unreact: 1 removed, 2 no-ops, koatag's reaction preserved")


def test_4_list_for_messages_batch(db: Path) -> None:
    print("\n[test 4] list_for_messages batches per id; missing keys empty")
    mailbox_reactions.init_schema(db)
    mailbox_reactions.react(db, 1, "wiki", "👍")
    mailbox_reactions.react(db, 1, "wiki", "🔥")
    mailbox_reactions.react(db, 1, "koatag", "👀")
    mailbox_reactions.react(db, 2, "wiki", "✅")
    # no reactions for msg 3

    result = mailbox_reactions.list_for_messages(db, [1, 2, 3])
    assert set(result.keys()) == {1, 2, 3}, \
        f"missing msg ids should still be keys: {result.keys()}"
    assert len(result[1]) == 3
    assert {r["emoji"] for r in result[1]} == {"👍", "🔥", "👀"}
    assert len(result[2]) == 1
    assert result[2][0]["emoji"] == "✅"
    assert result[3] == [], f"msg 3 has no reactions, expected empty list: {result[3]}"

    # Empty list returns empty dict
    assert mailbox_reactions.list_for_messages(db, []) == {}
    print(f"  msg 1: 3 reactions, msg 2: 1, msg 3: [], empty input: {{}}")


def test_5_stats(db: Path) -> None:
    print("\n[test 5] stats counters")
    mailbox_reactions.init_schema(db)
    s_empty = mailbox_reactions.stats(db)
    assert s_empty == {"reaction_count": 0, "reaction_unique_emojis": 0}

    mailbox_reactions.react(db, 1, "a", "👍")
    mailbox_reactions.react(db, 1, "b", "👍")
    mailbox_reactions.react(db, 2, "a", "🔥")
    mailbox_reactions.react(db, 3, "c", "👀")

    s = mailbox_reactions.stats(db)
    assert s["reaction_count"] == 4
    assert s["reaction_unique_emojis"] == 3
    print(f"  stats: {s}")


def test_6_emoji_validation(db: Path) -> None:
    print("\n[test 6] emoji length validation")
    mailbox_reactions.init_schema(db)
    # Empty
    try:
        mailbox_reactions.react(db, 1, "wiki", "")
    except ValueError as e:
        assert "1.." in str(e)
    else:
        raise AssertionError("expected ValueError on empty emoji")

    # Over MAX
    long_emoji = "x" * (mailbox_reactions.MAX_EMOJI_LEN + 1)
    try:
        mailbox_reactions.react(db, 1, "wiki", long_emoji)
    except ValueError:
        pass
    else:
        raise AssertionError(f"expected ValueError on {len(long_emoji)}-char emoji")

    # Exactly MAX_EMOJI_LEN is fine
    ok_emoji = "x" * mailbox_reactions.MAX_EMOJI_LEN
    result = mailbox_reactions.react(db, 1, "wiki", ok_emoji)
    assert result["added"] is True
    print(f"  empty rejected, {mailbox_reactions.MAX_EMOJI_LEN + 1}-char rejected, "
          f"{mailbox_reactions.MAX_EMOJI_LEN}-char accepted")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-reactions-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_ddl_idempotent,
                                 test_2_react_dedup,
                                 test_3_unreact,
                                 test_4_list_for_messages_batch,
                                 test_5_stats,
                                 test_6_emoji_validation), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL REACTION TESTS PASSED")
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
