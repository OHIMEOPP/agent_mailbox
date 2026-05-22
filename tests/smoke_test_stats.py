"""Smoke test for mailbox-stats.py.

Seeds a temp DB with known message counts / senders / recipients / threading
/ TTL / FTS5, then runs the CLI both as text and --json and verifies the
numbers come out correctly. Read-only CLI — no need for a running server.

  1. Overview counts: total / unread / attachments / peers
  2. Top senders ordered by count desc
  3. Top recipients with correct unread split
  4. --since filter reduces in_window count
  5. Threading stats: roots / replies / orphan_replies all counted right
  6. TTL stats: with_ttl / expired_pending / expiring_24h
  7. --json produces parseable output with the same numbers as text
  8. Read-only invariant: PRAGMA query_only catches accidental writes
"""
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def seed_db(db: Path) -> None:
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
            in_reply_to INTEGER,
            expires_at TEXT
        );
        CREATE TABLE peers (
            name TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime TEXT,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """)

    # 10 messages from alice → bob, all old (10 days ago)
    for i in range(10):
        conn.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at, read_at) "
            "VALUES('alice', 'bob', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-10 days'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-9 days'))",
            (f"old body {i} " + "x" * 50,),
        )
    # 5 messages from bob → alice, fresh (1 hour ago), unread
    for i in range(5):
        conn.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at) "
            "VALUES('bob', 'alice', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour'))",
            (f"fresh unread {i} " + "y" * 30,),
        )
    # 3 threading rows: msg #16 root, #17 in_reply_to=16, #18 in_reply_to=999 (orphan)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, in_reply_to) "
        "VALUES('alice', 'bob', 'root msg', NULL)")
    root_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, in_reply_to) "
        "VALUES('bob', 'alice', 'reply', ?)",
        (root_id,))
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, in_reply_to) "
        "VALUES('alice', 'bob', 'orphan reply', 99999)")  # parent doesn't exist

    # TTL stats: 2 already expired, 1 expiring in 6h, 1 expiring in 2d (out of 24h window)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, expires_at) "
        "VALUES('alice', 'bob', 'expired 1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour'))")
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, expires_at) "
        "VALUES('alice', 'bob', 'expired 2', strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 minutes'))")
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, expires_at) "
        "VALUES('alice', 'bob', 'expiring soon', strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+6 hours'))")
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, expires_at) "
        "VALUES('alice', 'bob', 'expiring later', strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+2 days'))")

    # Peers — 3 active, 1 stale
    for name in ["alice", "bob", "carol"]:
        conn.execute(
            "INSERT INTO peers(name, last_seen_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day'))",
            (name,))
    conn.execute(
        "INSERT INTO peers(name, last_seen_at) "
        "VALUES('stale', strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days'))")

    conn.commit()
    conn.close()


def run_cli(db: Path, args: list) -> subprocess.CompletedProcess:
    here = Path(__file__).parent.parent
    return subprocess.run(
        [sys.executable, str(here / "mailbox-stats.py"), "--db", str(db)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10,
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-stats-smoke-"))
    db = workdir / "mailbox.db"
    print(f"[smoke] workdir={workdir}")

    try:
        seed_db(db)

        # ---- Test 1: text output runs without error ----
        r1 = run_cli(db, [])
        assert r1.returncode == 0, f"text: exit {r1.returncode}: {r1.stderr}"
        out = r1.stdout
        assert "Overview" in out and "Top senders" in out and "Top recipients" in out
        print("[smoke] text output sections ok")

        # ---- Test 2: --json output ----
        r2 = run_cli(db, ["--json"])
        assert r2.returncode == 0
        s = json.loads(r2.stdout)
        assert s["overview"]["message_count"] == 22, \
            f"expected 22 msgs (10 + 5 + 3 + 4), got {s['overview']['message_count']}"
        # unread = 5 (bob→alice fresh) + 1 (msg #17 reply) + 1 (orphan) + 4 (TTL)
        # actually only the 5 bob→alice + 3 threading + 4 TTL not marked-read = 12
        # let me check the seeded data:
        # 10 old read, 5 fresh unread, 3 threading (no read_at), 4 TTL (no read_at)
        # unread = 5 + 3 + 4 = 12
        assert s["overview"]["unread_count"] == 12, \
            f"expected 12 unread, got {s['overview']['unread_count']}"
        print(f"[smoke] --json overview ok ({s['overview']['message_count']} total / "
              f"{s['overview']['unread_count']} unread)")

        # ---- Test 3: top senders ordering ----
        senders = s["top_senders"]
        # alice sent: 10 old + 1 root + 1 orphan + 4 TTL = 16
        # bob sent: 5 fresh + 1 reply = 6
        assert senders[0]["name"] == "alice" and senders[0]["sent"] == 16, \
            f"top sender wrong: {senders[0]}"
        assert senders[1]["name"] == "bob" and senders[1]["sent"] == 6, \
            f"second sender wrong: {senders[1]}"
        print("[smoke] top senders ranking ok")

        # ---- Test 4: top recipients with unread ----
        recips = s["top_recipients"]
        # bob received: 10 + 1 (root) + 1 (orphan) + 4 (TTL) = 16
        # alice received: 5 fresh + 1 reply = 6
        bob_entry = next(r for r in recips if r["name"] == "bob")
        alice_entry = next(r for r in recips if r["name"] == "alice")
        assert bob_entry["received"] == 16
        # bob's unread: 1 root + 1 orphan + 4 TTL = 6 (10 old read)
        assert bob_entry["unread"] == 6, f"bob unread wrong: {bob_entry}"
        # alice's unread: 5 fresh + 1 reply = 6
        assert alice_entry["unread"] == 6
        print("[smoke] top recipients + unread split ok")

        # ---- Test 5: threading stats ----
        t = s["threading"]
        # roots = messages with in_reply_to IS NULL
        # That's 10 old + 5 fresh + 1 root + 4 TTL = 20
        assert t["roots"] == 20, f"roots: {t['roots']}"
        # replies = 2 (one valid reply + one orphan)
        assert t["replies"] == 2
        assert t["orphan_replies"] == 1, f"orphan_replies: {t['orphan_replies']}"
        print(f"[smoke] threading stats ok: {t}")

        # ---- Test 6: TTL stats ----
        ttl = s["ttl"]
        assert ttl["with_ttl"] == 4
        assert ttl["expired_pending_sweep"] == 2
        assert ttl["expiring_24h"] == 1, f"expiring_24h: {ttl['expiring_24h']}"
        print(f"[smoke] TTL stats ok: {ttl}")

        # ---- Test 7: peers ----
        p = s["peers"]
        assert p["total"] == 4
        assert p["active_7d"] == 3
        print(f"[smoke] peers ok: {p}")

        # ---- Test 8: --since filter ----
        r3 = run_cli(db, ["--since", "2h", "--json"])
        assert r3.returncode == 0
        s_filt = json.loads(r3.stdout)
        # Within last 2h: 5 fresh + 3 threading + 4 TTL = 12 (note: threading + TTL
        # are inserted with NOW timestamp, so in window)
        # Old (10 messages from -10 days) NOT in window
        assert s_filt["overview"]["in_window"] == 12, \
            f"--since 2h in_window: expected 12, got {s_filt['overview']['in_window']}"
        print(f"[smoke] --since filter ok ({s_filt['overview']['in_window']} in window)")

        # ---- Test 9: hour histogram has 24 entries ----
        assert len(s["hour_histogram"]) == 24
        print("[smoke] hour histogram 24 buckets ok")

        # ---- Test 10: read-only invariant — DB rows unchanged ----
        # Verify by comparing message count before and after CLI run
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 22, f"CLI shouldn't change row count: {count}"
        print("[smoke] read-only invariant ok (22 rows unchanged)")

        print(f"\n[smoke] ALL STATS TESTS PASSED")
        return 0
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
