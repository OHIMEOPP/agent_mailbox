"""Smoke test for mailbox/receipts.py (read receipts, BACKLOG #3).

Test plan:
  1. Migration v009 adds request_receipt to a legacy DB
  2. mark_read flow on a request_receipt=1 message → ack row inserted
     (reader → original sender, body "receipt: msg #X read at ...",
     in_reply_to=X, request_receipt=0)
  3. Re-mark of the same id → no second receipt (read_at IS NULL filter)
  4. Loop-safety: marking the receipt itself read → no new receipt
  5. reader filter: collect_pending(reader=other) skips messages not
     addressed to that reader
  6. Plain messages (request_receipt=0) → no receipt
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from mailbox import migrations as mailbox_migrations  # noqa: E402
from mailbox import receipts as mailbox_receipts  # noqa: E402


def _mk_db(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            read_at TEXT,
            in_reply_to INTEGER
        );
    """)
    conn.commit()
    conn.close()


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _mark_read(conn: sqlite3.Connection, ids: list[int],
               reader: str | None = None) -> list[dict]:
    """Mimic both servers' mark_read order: collect → UPDATE → insert."""
    pending = mailbox_receipts.collect_pending(conn, ids, reader=reader)
    qmarks = ",".join("?" * len(ids))
    sql = (f"UPDATE messages SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
           f"WHERE id IN ({qmarks}) AND read_at IS NULL")
    args: list = list(ids)
    if reader is not None:
        sql += " AND to_name = ?"
        args.append(reader)
    conn.execute(sql, args)
    receipts = mailbox_receipts.insert_receipts(conn, pending)
    conn.commit()
    return receipts


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mailbox-receipts-test-"))
    db = tmp / "mailbox.db"
    _mk_db(db)

    print("[test 1] migration v009 adds request_receipt on legacy DB")
    mailbox_migrations.init_schema(db)
    mailbox_migrations.apply(db)
    conn = _connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "request_receipt" in cols, f"missing column, got {cols}"
    print("  ok")

    print("[test 2] mark_read on request_receipt=1 → ack row")
    msg = conn.execute(
        "INSERT INTO messages(from_name, to_name, body, request_receipt) "
        "VALUES('alice', 'bob', 'urgent ping', 1) RETURNING id").fetchone()
    receipts = _mark_read(conn, [msg["id"]], reader="bob")
    assert len(receipts) == 1, receipts
    ack = conn.execute("SELECT * FROM messages WHERE id=?",
                       (receipts[0]["receipt_id"],)).fetchone()
    assert ack["from_name"] == "bob" and ack["to_name"] == "alice"
    assert ack["body"].startswith(f"receipt: msg #{msg['id']} read at ")
    assert ack["in_reply_to"] == msg["id"]
    assert ack["request_receipt"] == 0
    print(f"  ok — ack #{ack['id']}: {ack['body']}")

    print("[test 3] re-mark same id → no second receipt")
    receipts = _mark_read(conn, [msg["id"]], reader="bob")
    assert receipts == [], receipts
    print("  ok")

    print("[test 4] loop-safety: marking the receipt read → no new receipt")
    receipts = _mark_read(conn, [ack["id"]], reader="alice")
    assert receipts == [], receipts
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 2, f"expected 2 rows (msg+ack), got {n}"
    print("  ok")

    print("[test 5] reader filter: not-my-message → skipped")
    msg2 = conn.execute(
        "INSERT INTO messages(from_name, to_name, body, request_receipt) "
        "VALUES('alice', 'bob', 'ping 2', 1) RETURNING id").fetchone()
    pending = mailbox_receipts.collect_pending(conn, [msg2["id"]], reader="carol")
    assert pending == [], pending
    # hub mode (reader=None) still sees it
    pending = mailbox_receipts.collect_pending(conn, [msg2["id"]])
    assert len(pending) == 1
    conn.commit()
    print("  ok")

    print("[test 6] plain message (request_receipt=0) → no receipt")
    msg3 = conn.execute(
        "INSERT INTO messages(from_name, to_name, body) "
        "VALUES('alice', 'bob', 'normal') RETURNING id").fetchone()
    receipts = _mark_read(conn, [msg3["id"]], reader="bob")
    assert receipts == [], receipts
    print("  ok")

    conn.close()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
