"""Worker-pattern e2e — claim + priority + mark_read integration.

End-to-end demo that proves the SQS-style worker pattern works across
all the new primitives shipped this overnight (priority + claim +
auto-release on mark_read + claimable_only filter).

Scenario:
  1. Boss seeds 5 task messages addressed to worker, varied priority
  2. Worker calls inbox(claimable_only=True, min_priority=3) — should
     return only the high-priority ones, ordered DESC priority
  3. Worker claims the top-priority task → 200, exclusive lock for TTL
  4. Concurrent "ghost worker" (SQL-injected to simulate another worker
     instance with same name) attempts claim → 409 with existing info
  5. Worker mark_read on the claimed task → auto-releases claim + marks
     read; row no longer surfaces in inbox(unread_only=True)
  6. Worker inbox(claimable_only=True, min_priority=3) again — top task
     is gone, next-highest surfaces, FIFO within priority preserved

If all 6 steps pass, the worker pattern is operationally sound — a real
agent can implement the loop:
    while task := pick_top_claimable():
        claim(task.id, ttl=300)
        do_work(task)
        mark_read([task.id])
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
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-worker-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

    env = os.environ.copy()
    env["CLAUDE_MAILBOX_TOKEN"] = token
    # Disable side-effect daemons — we only want send/inbox/claim
    env["MAILBOX_WEBHOOKS_DISABLED"] = "1"
    env["MAILBOX_RETENTION_DISABLED"] = "1"
    env["MAILBOX_BACKUP_DISABLED"] = "1"
    env["MAILBOX_SCHEDULED_DISABLED"] = "1"
    here = Path(__file__).parent.parent
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

        # ---- Step 1: Boss seeds 5 mixed-priority tasks to worker ----
        # priorities: [0, 9, 3, 5, 1]
        tasks = [
            ("low task", 0),
            ("URGENT alpha", 9),
            ("medium task", 3),
            ("important task", 5),
            ("trivial bg", 1),
        ]
        for body, prio in tasks:
            post_json(f"{base}/send", token,
                      {"from": "boss", "to": "worker",
                       "body": body, "priority": prio})
        print(f"[smoke] seeded 5 tasks with priorities {[p for _,p in tasks]}")

        # ---- Step 2: Worker inbox(claimable_only=True, min_priority=3) ----
        # Should return only msgs with priority>=3, ordered DESC:
        #   urgent(9), important(5), medium(3)
        # Sort and verify
        r = get_json(f"{base}/inbox?name=worker&unread=1&claimable=1&min_priority=3", token)
        msgs = r["messages"]
        assert len(msgs) == 3, f"expected 3 high-priority msgs, got {len(msgs)}: {[m['body'] for m in msgs]}"
        priorities = [m["priority"] for m in msgs]
        # mailbox-server.py /inbox returns ORDER BY id DESC by default,
        # but priority-aware ordering is "priority DESC, id ASC"
        assert priorities == sorted(priorities, reverse=True), \
            f"expected DESC priority order, got {priorities}"
        assert priorities[0] == 9
        assert msgs[0]["body"] == "URGENT alpha"
        print(f"[smoke] worker sees top 3 priorities {priorities}, "
              f"top body='{msgs[0]['body']}'")

        # ---- Step 3: Worker claims top task ----
        top_id = msgs[0]["id"]
        status, claim_r = post_json(f"{base}/claim", token,
                                     {"actor": "worker", "message_id": top_id,
                                      "ttl_seconds": 300})
        assert status == 200 and claim_r["claimed_by"] == "worker"
        print(f"[smoke] worker claimed msg {top_id} (URGENT alpha) "
              f"until {claim_r['claimed_until']}")

        # ---- Step 4: Concurrent ghost worker (SQL inject) → 409 ----
        # Simulate another worker instance holding the claim. To trigger 409,
        # SQL-inject a foreign claim, then have "worker" try claim — will hit
        # 409 because claimed_by != actor and claimed_until > now.
        conn = sqlite3.connect(str(db))
        # Take note of existing claim, replace with ghost
        conn.execute(
            "UPDATE messages SET claimed_by='worker-ghost', "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', '+10 minutes') "
            "WHERE id=?", (top_id,)
        )
        conn.commit()
        conn.close()
        try:
            post_json(f"{base}/claim", token,
                      {"actor": "worker", "message_id": top_id, "ttl_seconds": 60})
            print("[smoke] FAIL: should 409", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 409
            body = json.loads(e.read())
            assert body["claimed_by"] == "worker-ghost"
            print(f"[smoke] concurrent ghost-worker claim → 409 ok "
                  f"(reported {body['claimed_by']})")
        # Restore claim to actual worker so mark_read in next step releases sensibly
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE messages SET claimed_by='worker', "
            "claimed_until=strftime('%Y-%m-%dT%H:%M:%fZ','now', '+5 minutes') "
            "WHERE id=?", (top_id,)
        )
        conn.commit()
        conn.close()

        # ---- Step 5: mark_read → auto-release ----
        status, mr = post_json(f"{base}/mark_read", token, {"ids": [top_id]})
        assert mr["count"] == 1
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT claimed_by, claimed_until, read_at FROM messages WHERE id=?",
            (top_id,),
        ).fetchone()
        conn.close()
        assert row[0] is None and row[1] is None, \
            f"mark_read did NOT auto-release claim: claimed_by={row[0]}"
        assert row[2] is not None, "mark_read didn't set read_at"
        print("[smoke] mark_read auto-released + read_at set ok")

        # ---- Step 6: Re-inbox — top task gone, next surfaces ----
        r2 = get_json(f"{base}/inbox?name=worker&unread=1&claimable=1&min_priority=3", token)
        msgs2 = r2["messages"]
        # Now expected: priorities [5, 3] (urgent 9 has been mark_read)
        assert len(msgs2) == 2, \
            f"expected 2 remaining high-priority after mark_read, got {len(msgs2)}"
        assert msgs2[0]["priority"] == 5 and msgs2[0]["body"] == "important task", \
            f"next-priority should be 5 'important task', got {msgs2[0]}"
        assert msgs2[1]["priority"] == 3 and msgs2[1]["body"] == "medium task"
        # URGENT alpha (id=top_id) should NOT appear in unread inbox
        ids2 = {m["id"] for m in msgs2}
        assert top_id not in ids2, "marked-read msg should not appear in unread inbox"
        print(f"[smoke] re-inbox: top task gone, next priorities {[m['priority'] for m in msgs2]}")

        # ---- Bonus: prove the worker can complete the full queue ----
        # Loop until no more claimable >=3 tasks remain
        completed = ["URGENT alpha"]
        while True:
            r3 = get_json(f"{base}/inbox?name=worker&unread=1&claimable=1&min_priority=3", token)
            if not r3["messages"]:
                break
            task = r3["messages"][0]
            post_json(f"{base}/claim", token,
                      {"actor": "worker", "message_id": task["id"], "ttl_seconds": 60})
            post_json(f"{base}/mark_read", token, {"ids": [task["id"]]})
            completed.append(task["body"])
        assert completed == ["URGENT alpha", "important task", "medium task"], \
            f"worker queue didn't drain in priority order: {completed}"
        print(f"[smoke] worker queue drained in priority order: {completed}")

        # Low-priority (0 + 1) untouched — verify in inbox without min_priority filter
        r4 = get_json(f"{base}/inbox?name=worker&unread=1", token)
        unread = {m["body"] for m in r4["messages"]}
        assert unread == {"low task", "trivial bg"}, \
            f"low-priority leftover: expected {{low,trivial}}, got {unread}"
        print(f"[smoke] low-priority leftover untouched: {unread}")

        print(f"\n[smoke] ALL WORKER PATTERN TESTS PASSED")
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
            if err and ("FAIL" in err or "Error" in err):
                print("\n--- server stderr ---", file=sys.stderr)
                print(err, file=sys.stderr)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
