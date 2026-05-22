"""Smoke test for scheduled-send queue.

Mostly module-level + CLI tests; daemon-tick behavior tested by calling
deliver_pending() directly (avoids spinning a 30s wait).

  1. init_schema is idempotent (run twice; no error)
  2. parse_deliver_at: ISO passthrough + relative (5m / 2h / 7d)
  3. enqueue() inserts row, list_pending returns it
  4. deliver_pending materializes rows whose deliver_at <= now
  5. cancel() marks pending as cancelled, prevents future delivery
  6. cancel of delivered row returns ok=False with helpful error
  7. CLI --stats / --list / --cancel / --deliver-now
  8. End-to-end via /send REST: POST with deliver_at → enqueued; manual flush
     delivers; recipient inbox shows message
"""
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from mailbox import scheduled as mailbox_scheduled  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if json.loads(r.read()).get("ok"):
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server never came up at {url}")


def post_json(url, token, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def run_cli(args, db):
    here = Path(__file__).parent.parent
    return subprocess.run(
        [sys.executable, str(here / "tools" / "mailbox-scheduled.py"), "--db", str(db)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10,
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-scheduled-smoke-"))
    db = workdir / "mailbox.db"
    print(f"[smoke] workdir={workdir}")

    try:
        # Need messages table for the deliver_pending side effect.
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                read_at TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                in_reply_to INTEGER,
                expires_at TEXT
            );
        """)
        conn.commit()
        conn.close()

        # ---- Test 1: idempotent init ----
        mailbox_scheduled.init_schema(db)
        mailbox_scheduled.init_schema(db)
        print("[smoke] init_schema idempotent ok")

        # ---- Test 2: parse_deliver_at ----
        assert mailbox_scheduled.parse_deliver_at(None) is None
        assert mailbox_scheduled.parse_deliver_at("") is None
        iso_in = "2030-01-01T00:00:00Z"
        assert mailbox_scheduled.parse_deliver_at(iso_in) == iso_in
        rel = mailbox_scheduled.parse_deliver_at("5m")
        assert rel and "T" in rel and rel.endswith("Z"), \
            f"relative 5m didn't resolve: {rel!r}"
        # Bad input raises
        try:
            mailbox_scheduled.parse_deliver_at("garbage")
            return 1
        except ValueError:
            pass
        print(f"[smoke] parse_deliver_at ok (5m → {rel})")

        # ---- Test 3: enqueue + list ----
        # Enqueue one row 1 hour in the future (won't deliver in this test)
        q1 = mailbox_scheduled.enqueue(
            db, from_name="alice", to_name="bob", body="future msg",
            deliver_at=mailbox_scheduled.parse_deliver_at("1h"),
        )
        assert q1["id"]
        rows = mailbox_scheduled.list_pending(db)
        assert len(rows) == 1 and rows[0]["id"] == q1["id"]
        print(f"[smoke] enqueue + list ok (id={q1['id']})")

        # ---- Test 4: deliver_pending materializes past rows ----
        # Use SQL to insert one row with deliver_at = -1 minute (already due)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO scheduled_messages(from_name, to_name, body, deliver_at) "
            "VALUES('carol', 'dave', 'past due',  "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 minute'))"
        )
        conn.commit()
        conn.close()

        before = sqlite3.connect(str(db))
        before_count = before.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        before.close()

        c = mailbox_scheduled.deliver_pending(db)
        assert c["delivered"] == 1, f"expected 1 delivered, got {c}"
        assert c["scanned"] == 1

        after = sqlite3.connect(str(db))
        after_count = after.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        # And the delivered scheduled_messages row should have delivered_msg_id set
        sched_row = after.execute(
            "SELECT delivered_msg_id FROM scheduled_messages "
            "WHERE from_name='carol' AND to_name='dave'"
        ).fetchone()
        after.close()
        assert after_count == before_count + 1
        assert sched_row[0] is not None
        print(f"[smoke] deliver_pending materialized 1 row (msg #{sched_row[0]})")

        # Re-run delivery — should be no-op (already delivered)
        c2 = mailbox_scheduled.deliver_pending(db)
        assert c2["delivered"] == 0
        print("[smoke] idempotent re-deliver no-op ok")

        # ---- Test 5: cancel ----
        cancel_result = mailbox_scheduled.cancel(db, q1["id"])
        assert cancel_result["ok"], f"cancel failed: {cancel_result}"
        # Verify subsequent delivery doesn't fire (even if deliver_at passes,
        # row is cancelled). Since deliver_at is 1h future anyway, can't fully
        # test the "past + cancelled" path here, but we can confirm list_pending
        # excludes it.
        pending = mailbox_scheduled.list_pending(db)
        assert all(r["id"] != q1["id"] for r in pending), \
            "cancelled row still in pending list"
        print("[smoke] cancel ok (excluded from pending)")

        # ---- Test 6: cancel of already-delivered row ----
        # The carol→dave row has delivered_msg_id set
        delivered_sched_id = sqlite3.connect(str(db)).execute(
            "SELECT id FROM scheduled_messages WHERE delivered_msg_id IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        result = mailbox_scheduled.cancel(db, delivered_sched_id)
        assert result["ok"] is False, f"cancel of delivered should fail: {result}"
        assert "already delivered" in result["error"]
        print("[smoke] cancel of delivered row → ok=False ok")

        # ---- Test 7: CLI commands ----
        r_stats = run_cli(["--stats", "--json"], db)
        assert r_stats.returncode == 0
        s = json.loads(r_stats.stdout)
        # 2 in total: q1 (cancelled), delivered (carol→dave). q1 status varies.
        assert s["scheduled_pending"] == 0  # both terminal
        assert s["scheduled_delivered"] == 1
        assert s["scheduled_cancelled"] == 1
        print(f"[smoke] CLI --stats ok ({s})")

        r_list = run_cli(["--list", "--include-delivered", "--json"], db)
        assert r_list.returncode == 0
        rows = json.loads(r_list.stdout)
        assert len(rows) == 2
        print(f"[smoke] CLI --list --include-delivered ok ({len(rows)} rows)")

        # ---- Test 8: E2E via REST /send with deliver_at ----
        token = secrets.token_urlsafe(32)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["CLAUDE_MAILBOX_TOKEN"] = token
        env["MAILBOX_SCHEDULED_DISABLED"] = "1"  # disable daemon — manual flush
        env["MAILBOX_WEBHOOKS_DISABLED"] = "1"
        env["MAILBOX_RETENTION_DISABLED"] = "1"
        env["MAILBOX_BACKUP_DISABLED"] = "1"

        # Fresh DB for the REST e2e
        rest_db = workdir / "rest.db"
        attachments = workdir / "rest-attachments"
        here = Path(__file__).parent.parent
        proc = subprocess.Popen(
            [sys.executable, str(here / "mailbox-server.py"),
             "--host", "127.0.0.1", "--port", str(port),
             "--db", str(rest_db), "--attachments-dir", str(attachments)],
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            wait_health(base, timeout=15)
            # Schedule a message 1s in the past so it's immediately due
            past = mailbox_scheduled.parse_deliver_at("1s")  # 1s future
            # Wait 2s and then flush — simulates daemon tick
            r = post_json(f"{base}/send", token,
                          {"from": "alice", "to": "bob",
                           "body": "scheduled hello", "deliver_at": past})
            assert r.get("scheduled") is True
            assert r["count"] == 1
            sched_id = r["items"][0]["scheduled_id"]
            # bob's inbox should be empty (not yet delivered)
            inbox_before = get_json(f"{base}/inbox?name=bob", token)
            assert len(inbox_before["messages"]) == 0
            # Wait deliver_at to pass + manual flush via CLI
            time.sleep(1.5)
            r_flush = run_cli(["--deliver-now", "--json"], rest_db)
            assert r_flush.returncode == 0
            flush_c = json.loads(r_flush.stdout)
            assert flush_c["delivered"] == 1, f"flush counters: {flush_c}"
            # Now bob's inbox should have the message
            inbox_after = get_json(f"{base}/inbox?name=bob", token)
            assert len(inbox_after["messages"]) == 1
            assert inbox_after["messages"][0]["body"] == "scheduled hello"
            print(f"[smoke] REST e2e scheduled-send ok (sched_id={sched_id} → msg {inbox_after['messages'][0]['id']})")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        print(f"\n[smoke] ALL SCHEDULED TESTS PASSED")
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
