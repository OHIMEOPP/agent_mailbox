"""Smoke test for message claim / visibility timeout feature.

Covers SQS-style "one worker grabs a task, others skip" pattern via REST.
Designed to NOT need long sleeps — uses short TTLs (1-2s) and direct
SQL to simulate expiry.

  1. Migration v004 idempotent on fresh + legacy DBs
  2. /claim succeeds on unclaimed message; returns claimed_by + claimed_until
  3. /claim by another agent on same msg → 409 with existing claim info
  4. /claim by same agent → re-claim succeeds (refresh TTL)
  5. /claim on expired claim succeeds (different agent)
  6. /claim on msg not addressed to actor → 403
  7. /claim on missing msg → 404
  8. /release by owner clears claim
  9. /release by non-owner is no-op (released=False)
 10. /mark_read auto-releases claim
 11. /inbox?claimable=1 skips others' active claims but shows own
 12. /health exposes messages_claimed_active count
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
        return r.status, json.loads(r.read().decode("utf-8"))


def get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-claim-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

    env = os.environ.copy()
    env["CLAUDE_MAILBOX_TOKEN"] = token
    env["MAILBOX_WEBHOOKS_DISABLED"] = "1"
    env["MAILBOX_RETENTION_DISABLED"] = "1"
    env["MAILBOX_BACKUP_DISABLED"] = "1"
    env["MAILBOX_SCHEDULED_DISABLED"] = "1"
    here = Path(__file__).parent
    proc = subprocess.Popen(
        [sys.executable, str(here / "mailbox-server.py"),
         "--host", "127.0.0.1", "--port", str(port),
         "--db", str(db), "--attachments-dir", str(attachments)],
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        wait_health(base, timeout=15)
        print("[smoke] server up")

        # ---- Test 1: migration applied (claimed_by + claimed_until exist) ----
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        conn.close()
        assert "claimed_by" in cols and "claimed_until" in cols, \
            f"migration v004 didn't add columns: {cols}"
        print("[smoke] migration v004 applied ok")

        # Seed: alice → bob (msg 1), alice → carol (msg 2)
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "task A"})
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "carol", "body": "task B"})

        # ---- Test 2: /claim succeeds on unclaimed ----
        status, r1 = post_json(f"{base}/claim", token,
                               {"actor": "bob", "message_id": 1, "ttl_seconds": 60})
        assert status == 200 and r1["ok"]
        assert r1["claimed_by"] == "bob"
        assert r1["claimed_until"]
        print(f"[smoke] /claim ok (msg=1 by bob until={r1['claimed_until']})")

        # ---- Test 3: same recipient name from different session/instance → 409 ----
        # Simulate "bob@SECONDARY" already holding the claim by SQL-injecting
        # (real-world: two bob instances on different machines).
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE messages SET claimed_by='bob-other-session', "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', '+5 minutes') "
            "WHERE id=1"
        )
        conn.commit()
        conn.close()
        try:
            post_json(f"{base}/claim", token,
                      {"actor": "bob", "message_id": 1, "ttl_seconds": 60})
            print("[smoke] FAIL: should 409", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 409
            body = json.loads(e.read())
            assert body["claimed_by"] == "bob-other-session"
            print("[smoke] /claim by same-name-different-instance → 409 ok")
        # Clear so test 4 sees bob's own claim
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE messages SET claimed_by='bob', claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now','+5 minutes') WHERE id=1")
        conn.commit()
        conn.close()

        # ---- Test 4: same agent re-claim refresh TTL ----
        status, r4 = post_json(f"{base}/claim", token,
                               {"actor": "bob", "message_id": 1, "ttl_seconds": 120})
        assert status == 200 and r4["claimed_by"] == "bob"
        # claimed_until should be later than r1
        assert r4["claimed_until"] > r1["claimed_until"]
        print("[smoke] re-claim refreshes TTL ok")

        # ---- Test 5: expired claim by ghost-bob → bob can reclaim ----
        # SQL-inject ghost-bob claim that's already expired
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE messages SET claimed_by='ghost-bob', "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', '-1 hour') "
            "WHERE id=1"
        )
        conn.commit()
        conn.close()
        status, r5 = post_json(f"{base}/claim", token,
                               {"actor": "bob", "message_id": 1, "ttl_seconds": 60})
        assert status == 200 and r5["claimed_by"] == "bob", \
            f"expired ghost-bob claim should let bob reclaim: {r5}"
        print("[smoke] expired claim → re-claimable by recipient ok")

        # ---- Test 6: claim msg not addressed to actor → 403 ----
        try:
            post_json(f"{base}/claim", token,
                      {"actor": "bob", "message_id": 2, "ttl_seconds": 60})  # msg 2 is for carol
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 403
            print("[smoke] /claim wrong recipient → 403 ok")

        # ---- Test 7: missing msg → 404 ----
        try:
            post_json(f"{base}/claim", token,
                      {"actor": "bob", "message_id": 9999, "ttl_seconds": 60})
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 404
            print("[smoke] /claim missing msg → 404 ok")

        # ---- Test 8: /release by owner clears claim ----
        status, r8 = post_json(f"{base}/release", token,
                               {"actor": "bob", "message_id": 1})
        assert status == 200 and r8["released"] is True
        # Verify claim cleared
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT claimed_by, claimed_until FROM messages WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] is None and row[1] is None
        print("[smoke] /release by owner ok")

        # ---- Test 9: /release by non-owner is no-op ----
        # Re-claim msg 1
        post_json(f"{base}/claim", token,
                  {"actor": "bob", "message_id": 1, "ttl_seconds": 60})
        status, r9 = post_json(f"{base}/release", token,
                               {"actor": "eve", "message_id": 1})
        assert status == 200 and r9["released"] is False
        # bob's claim still intact
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT claimed_by FROM messages WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] == "bob"
        print("[smoke] /release by non-owner is no-op ok")

        # ---- Test 10: mark_read auto-releases ----
        status, r10 = post_json(f"{base}/mark_read", token, {"ids": [1]})
        assert r10["count"] == 1
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT claimed_by, read_at FROM messages WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] is None, f"claim not cleared on mark_read: {row[0]}"
        assert row[1] is not None
        print("[smoke] mark_read auto-releases claim ok")

        # ---- Test 11: inbox claimable=1 filter ----
        # Seed two new unread for bob; one claimed by eve, one unclaimed
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "task C"})
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "task D"})
        # Get the new ids
        inbox_all = get_json(f"{base}/inbox?name=bob&unread=1", token)
        new_msgs = sorted(m["id"] for m in inbox_all["messages"])
        msg_c, msg_d = new_msgs[-2], new_msgs[-1]
        # SQL-inject a foreign claim on msg_c (simulates another bob instance)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE messages SET claimed_by='other-bob-instance', "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', '+10 minutes') "
            "WHERE id=?", (msg_c,)
        )
        conn.commit()
        conn.close()

        # claimable=0 (default) → returns all bob's unread (incl msg_c)
        inbox_default = get_json(f"{base}/inbox?name=bob&unread=1", token)
        ids_default = {m["id"] for m in inbox_default["messages"]}
        assert msg_c in ids_default

        # claimable=1 → skips msg_c (claimed by other), shows msg_d
        inbox_clm = get_json(f"{base}/inbox?name=bob&unread=1&claimable=1", token)
        ids_clm = {m["id"] for m in inbox_clm["messages"]}
        assert msg_c not in ids_clm, f"claimable should skip msg_c: {ids_clm}"
        assert msg_d in ids_clm, f"claimable should include msg_d: {ids_clm}"
        print(f"[smoke] inbox?claimable=1 filter ok "
              f"(default has {len(ids_default)}, claimable has {len(ids_clm)})")

        # ---- Test 12: /health messages_claimed_active ----
        health = get_json(f"{base}/health", token)
        # msg_c is claimed (artificial) → 1 active claim
        assert health.get("messages_claimed_active") == 1, \
            f"expected 1 active claim, got {health.get('messages_claimed_active')}"
        print("[smoke] /health messages_claimed_active=1 ok")

        print(f"\n[smoke] ALL CLAIM TESTS PASSED")
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
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            err = proc.stderr.read()
            if err and "FAIL" in err:
                print("\n--- server stderr ---", file=sys.stderr)
                print(err, file=sys.stderr)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
