"""Smoke test for tools/mailbox-digest.py — actionable inbox summary.

Seed an inbox with mixed senders / priorities / TTLs / claims / threading /
reactions, run digest CLI, assert each section surfaces the right items.
"""
import io
import json
import os
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


def seed(db: Path) -> None:
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
            expires_at TEXT,
            claimed_by TEXT,
            claimed_until TEXT,
            priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE peers (name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL);
        CREATE TABLE reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            actor TEXT NOT NULL,
            emoji TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
    """)
    # Alice sends 3 unread to bob: priorities 0/5/9
    # bob earlier sent msg 0 (read); alice's #4 replies to bob's #0
    # Carol sends 2 unread (low priority)
    # Eve sends 1 with expires_at = +6h (within 24h window)

    # msg 1: bob → alice, read (so we can reference it as parent)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority, read_at) "
        "VALUES('bob', 'alice', 'original from bob', 0, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    msg1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # msg 2: alice → bob, P9 unread
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority) "
        "VALUES('alice', 'bob', 'URGENT: server down', 9)")
    msg2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # msg 3: alice → bob, P5 unread
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority) "
        "VALUES('alice', 'bob', 'important request', 5)")
    msg3 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # msg 4: alice → bob, P0 unread, in_reply_to=msg1 (replies to bob's read msg)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority, in_reply_to) "
        "VALUES('alice', 'bob', 'reply to bobs original', 0, ?)", (msg1,))
    msg4 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # msg 5,6: carol → bob, P1, P2 (low)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority) "
        "VALUES('carol', 'bob', 'fyi 1', 1)")
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority) "
        "VALUES('carol', 'bob', 'fyi 2', 2)")
    # msg 7: eve → bob with expires_at in 6h
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority, expires_at) "
        "VALUES('eve', 'bob', 'time-bomb msg', 0, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','+6 hours'))")
    msg7 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # msg 8: eve → bob with expires_at in 48h (outside 24h window — should NOT show)
    conn.execute(
        "INSERT INTO messages(from_name, to_name, body, priority, expires_at) "
        "VALUES('eve', 'bob', 'future bomb', 0, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','+48 hours'))")

    # Claims: msg3 held by "other-bob" (simulates conflict)
    conn.execute(
        "UPDATE messages SET claimed_by='bob-other', "
        "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now','+5 minutes') "
        "WHERE id=?", (msg3,))

    # Reactions on msg2 (the URGENT one) — 3 reactions
    for actor, emoji in [('carol', '🚨'), ('dave', '👀'), ('eve', '🔥')]:
        conn.execute(
            "INSERT INTO reactions(message_id, actor, emoji) VALUES(?, ?, ?)",
            (msg2, actor, emoji))

    conn.commit()
    conn.close()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-digest-smoke-"))
    db = workdir / "mailbox.db"
    here = Path(__file__).resolve().parent.parent / "tools" / "mailbox-digest.py"
    print(f"[smoke] workdir={workdir}")

    try:
        seed(db)

        # Run text mode
        r1 = subprocess.run(
            [sys.executable, str(here), "--peer", "bob", "--db", str(db)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        assert r1.returncode == 0, f"text mode exit {r1.returncode}: {r1.stderr}"
        text = r1.stdout
        assert "📨 mailbox digest for bob" in text
        assert "alice" in text  # top sender
        assert "URGENT: server down" in text  # high priority preview
        assert "P9" in text  # priority bucket label
        assert "time-bomb msg" in text  # TTL-expiring within 24h
        assert "future bomb" not in text  # outside 24h window
        assert "bob-other" in text or "held by other" in text  # claim conflict surfaced
        assert "🚨" in text or "reactions=3" in text  # most-reacted msg
        print("[smoke] text-mode sections + content ok")

        # JSON mode
        r2 = subprocess.run(
            [sys.executable, str(here), "--peer", "bob", "--db", str(db), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        assert r2.returncode == 0
        d = json.loads(r2.stdout)
        assert d["peer"] == "bob"
        # 3 alice + 2 carol + 2 eve (one in 24h, one in 48h) = 7 unread
        assert d["totals"]["unread"] == 7, f"unread totals: {d['totals']}"
        assert d["totals"]["distinct_senders"] == 3  # alice, carol, eve

        # unread_by_sender — alice has 3, carol 2, eve 2
        senders = {r["sender"]: r["count"] for r in d["unread_by_sender"]}
        assert senders["alice"] == 3, f"alice unread: {senders}"
        assert senders["carol"] == 2
        assert senders["eve"] == 2

        # priority buckets
        by_prio = {r["priority"]: r["count"] for r in d["unread_by_priority"]}
        assert by_prio.get(9) == 1 and by_prio.get(5) == 1 and by_prio.get(0, 0) >= 3

        # high_priority_unread (threshold default 3) — 2 items (P9, P5)
        hp = d["high_priority_unread"]
        assert len(hp) == 2
        assert hp[0]["priority"] == 9 and hp[1]["priority"] == 5
        print(f"[smoke] JSON mode ok ({d['totals']['unread']} unread, {len(d['unread_by_sender'])} senders, "
              f"{len(hp)} high-priority)")

        # TTL section: only msg 7 (within 24h)
        ttl = d["ttl_expiring_24h"]
        assert len(ttl) == 1, f"expected 1 TTL msg, got {len(ttl)}"
        assert "time-bomb" in ttl[0]["preview"]
        print("[smoke] TTL-expiring filter ok (24h window excludes 48h msg)")

        # Reply-thread: msg 4 replies to msg 1 (bob's msg)
        replies = d["unread_replies_to_you"]
        assert len(replies) == 1
        assert replies[0]["from_name"] == "alice"
        assert "reply to bobs original" in replies[0]["preview"]
        print("[smoke] unread_replies_to_you ok (alice's reply to bob detected)")

        # Most-reacted: msg 2 has 3 reactions
        reacted = d["most_reacted"]
        assert len(reacted) == 1 and reacted[0]["reaction_count"] == 3
        print("[smoke] most_reacted ok")

        # Claim status
        cs = d["claim_status"]
        assert cs["held_by_others"] == 1, f"claim status: {cs}"
        print("[smoke] claim_status ok (msg held by other)")

        # --threshold-priority override
        r3 = subprocess.run(
            [sys.executable, str(here), "--peer", "bob", "--db", str(db),
             "--threshold-priority", "7", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        d3 = json.loads(r3.stdout)
        # Only msg 2 (P9) >= 7
        assert len(d3["high_priority_unread"]) == 1
        print("[smoke] --threshold-priority override ok")

        # --peer required when env unset
        env = os.environ.copy()
        env.pop("CLAUDE_MAILBOX_NAME", None)
        r4 = subprocess.run(
            [sys.executable, str(here), "--db", str(db)],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
        assert r4.returncode == 2 and "peer required" in r4.stderr
        print("[smoke] --peer required guard ok")

        print(f"\n[smoke] ALL DIGEST TESTS PASSED")
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
