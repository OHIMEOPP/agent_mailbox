"""Smoke test for mailbox forward feature.

Test plan:
  1. forward creates new message with forwarded_from_msg_id set + header body
  2. note prefix prepended before header
  3. missing source message → FileNotFoundError
  4. inherit_priority True copies; False resets to 0
  5. list_forwards_of finds forwarded copies
  6. stats counters
  7. chain forward (forward of a forward) — schema works, count goes up
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mailbox import forward as mailbox_forward  # noqa: E402


def _create_messages_table_v8(db: Path) -> None:
    """Schema mirror post-v008."""
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            read_at TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            forwarded_from_msg_id INTEGER
        );
    """)
    conn.commit()
    conn.close()


def _insert_msg(db: Path, from_name: str, to_name: str, body: str,
                priority: int = 0) -> int:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body, priority) "
            "VALUES(?, ?, ?, ?) RETURNING id",
            (from_name, to_name, body, priority),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def _read_row(db: Path, msg_id: int) -> dict:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())
    finally:
        conn.close()


def test_1_basic_forward(db: Path) -> None:
    print("\n[test 1] basic forward roundtrip")
    _create_messages_table_v8(db)
    src = _insert_msg(db, "alice", "bob", "kickoff message body")

    r = mailbox_forward.forward(db, src, forwarder="bob", to_name="carol")
    assert r["forwarded_from_msg_id"] == src
    assert r["forwarded_to"] == "carol"
    assert r["forwarded_by"] == "bob"

    new_row = _read_row(db, r["id"])
    assert new_row["from_name"] == "bob"
    assert new_row["to_name"] == "carol"
    assert new_row["forwarded_from_msg_id"] == src
    assert ">>> forwarded from alice (msg #" in new_row["body"]
    assert "kickoff message body" in new_row["body"]
    print(f"  new msg {r['id']}, header + body preserved ok")


def test_2_with_note(db: Path) -> None:
    print("\n[test 2] note prefix prepended")
    _create_messages_table_v8(db)
    src = _insert_msg(db, "alice", "bob", "original")
    r = mailbox_forward.forward(db, src, forwarder="bob", to_name="carol",
                                  note="FYI thread you should know about")
    new_row = _read_row(db, r["id"])
    body = new_row["body"]
    # Note before header
    note_pos = body.index("FYI thread you should know about")
    header_pos = body.index(">>> forwarded from alice")
    orig_pos = body.index("original")
    assert note_pos < header_pos < orig_pos, \
        f"order: note={note_pos} header={header_pos} orig={orig_pos}"
    print(f"  note -> header -> original body ordering ok")


def test_3_missing_source_raises(db: Path) -> None:
    print("\n[test 3] missing source → FileNotFoundError")
    _create_messages_table_v8(db)
    try:
        mailbox_forward.forward(db, 99999, forwarder="bob", to_name="carol")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")
    print("  raise ok")


def test_4_inherit_priority(db: Path) -> None:
    print("\n[test 4] inherit_priority True copies; False zeros out")
    _create_messages_table_v8(db)
    src = _insert_msg(db, "alice", "bob", "urgent body", priority=9)

    r_inherit = mailbox_forward.forward(db, src, "bob", "carol",
                                          inherit_priority=True)
    inherit_row = _read_row(db, r_inherit["id"])
    assert inherit_row["priority"] == 9, inherit_row

    r_no_inherit = mailbox_forward.forward(db, src, "bob", "dave",
                                             inherit_priority=False)
    no_inherit_row = _read_row(db, r_no_inherit["id"])
    assert no_inherit_row["priority"] == 0
    print(f"  inherit=True: P9, inherit=False: P0")


def test_5_list_forwards_of(db: Path) -> None:
    print("\n[test 5] list_forwards_of finds all forwarded copies")
    _create_messages_table_v8(db)
    src = _insert_msg(db, "alice", "bob", "original")
    f1 = mailbox_forward.forward(db, src, "bob", "carol")
    f2 = mailbox_forward.forward(db, src, "bob", "dave")
    # Forward something else; should NOT appear in src's list
    other = _insert_msg(db, "eve", "frank", "unrelated")
    mailbox_forward.forward(db, other, "frank", "grace")

    forwards = mailbox_forward.list_forwards_of(db, src)
    ids = [f["id"] for f in forwards]
    assert ids == [f1["id"], f2["id"]], ids
    print(f"  found 2 forwards of src={src}: {ids}")


def test_6_stats(db: Path) -> None:
    print("\n[test 6] stats counters")
    _create_messages_table_v8(db)
    s0 = mailbox_forward.stats(db)
    assert s0 == {"forwarded_count": 0, "forward_sources_count": 0}

    src_a = _insert_msg(db, "alice", "bob", "a")
    src_b = _insert_msg(db, "alice", "bob", "b")
    mailbox_forward.forward(db, src_a, "bob", "carol")
    mailbox_forward.forward(db, src_a, "bob", "dave")  # 2nd forward of src_a
    mailbox_forward.forward(db, src_b, "bob", "carol")  # 1st of src_b

    s = mailbox_forward.stats(db)
    assert s["forwarded_count"] == 3, s
    assert s["forward_sources_count"] == 2, s
    print(f"  {s}")


def test_7_chain_forward(db: Path) -> None:
    print("\n[test 7] chain forward (forward-of-forward)")
    _create_messages_table_v8(db)
    src = _insert_msg(db, "alice", "bob", "original kickoff")
    f1 = mailbox_forward.forward(db, src, "bob", "carol")
    # Carol re-forwards what she received to dave
    f2 = mailbox_forward.forward(db, f1["id"], "carol", "dave")

    new_row = _read_row(db, f2["id"])
    # The forward-of-forward's forwarded_from_msg_id points at f1, not src
    assert new_row["forwarded_from_msg_id"] == f1["id"]
    # Body retains both layers — the original "kickoff" content still there
    assert "original kickoff" in new_row["body"]
    assert ">>> forwarded from bob" in new_row["body"]
    print(f"  src={src} → f1={f1['id']} → f2={f2['id']}; body retains all layers")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-forward-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_basic_forward,
                                 test_2_with_note,
                                 test_3_missing_source_raises,
                                 test_4_inherit_priority,
                                 test_5_list_forwards_of,
                                 test_6_stats,
                                 test_7_chain_forward), start=1):
            sub = workdir / f"t{i}"
            sub.mkdir()
            db = sub / "mailbox.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL FORWARD TESTS PASSED")
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
