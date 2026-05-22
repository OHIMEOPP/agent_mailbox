"""End-to-end integration smoke across all mailbox features.

Spins a real mailbox-server.py subprocess against a temp DB and exercises:
  - reply threading (in_reply_to)
  - mailing list aliases (to="koatag*" fanout)
  - TTL (expires_at)
  - reactions (react/unreact + inbox surface)
  - webhooks (register + delivery + HMAC verify)
  - audit log (all actions appear with correct actors)
  - FTS5 search (find by body keyword)

Catches integration drift that individual per-feature smokes miss — e.g.
inbox SELECT forgot to JOIN reactions, audit ACTIONS missing a new verb,
webhook daemon doesn't fire because init_schema ran in wrong order.

Run: py smoke_test_integration.py
"""
import hashlib
import hmac
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


# ---------- Generic HTTP helpers ----------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                payload = json.loads(r.read().decode("utf-8"))
                if payload.get("ok") is True:
                    return payload
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server never came up at {url}")


def post_json(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- Local HTTP receiver for webhook delivery test ----------

_RECEIVER_HITS: list[dict] = []


class WebhookReceiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _RECEIVER_HITS.append({
            "body": body,
            "headers": dict(self.headers),
            "parsed": json.loads(body),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        return  # quiet


# ---------- Scenarios ----------

def scenario_1_mail_lifecycle(base: str, token: str) -> None:
    """text send → reply with in_reply_to → react → search → mark_read.

    Verifies that:
    - send threads through audit + creates row + heartbeats sender peer
    - in_reply_to round-trips through inbox SELECT
    - react surfaces in inbox reactions list
    - FTS5 finds by body keyword
    - mark_read clears unread state
    """
    print("\n[scn 1] mail lifecycle: send → reply → react → search → mark_read")

    # alice sends to bob
    r = post_json(f"{base}/send", token,
                   {"from": "alice", "to": "bob",
                    "body": "kickoff message with unique-keyword-xyzzy"})
    parent_id = r["id"]
    assert parent_id >= 1

    # bob inbox sees it (and only it)
    inbox = get_json(f"{base}/inbox?name=bob&unread=1&limit=10", token)
    assert len(inbox["messages"]) == 1
    m = inbox["messages"][0]
    assert m["id"] == parent_id
    assert m["body"].startswith("kickoff")
    assert m["in_reply_to"] is None
    assert m["reactions"] == []
    assert m["attachments"] == []

    # bob replies threaded
    r2 = post_json(f"{base}/send", token,
                    {"from": "bob", "to": "alice",
                     "body": "reply with same xyzzy keyword",
                     "in_reply_to": parent_id})
    reply_id = r2["id"]

    # alice inbox: reply present, in_reply_to threads to parent
    alice_inbox = get_json(f"{base}/inbox?name=alice&unread=1&limit=10", token)
    assert len(alice_inbox["messages"]) == 1
    reply_msg = alice_inbox["messages"][0]
    assert reply_msg["id"] == reply_id
    assert reply_msg["in_reply_to"] == parent_id

    # alice reacts to bob's reply
    react_resp = post_json(f"{base}/react", token,
                            {"actor": "alice", "message_id": reply_id,
                             "emoji": "✅"})
    assert react_resp["added"] is True

    # Re-react = no-op (idempotency)
    react_again = post_json(f"{base}/react", token,
                             {"actor": "alice", "message_id": reply_id,
                              "emoji": "✅"})
    assert react_again["added"] is False
    assert react_again["id"] == react_resp["id"]

    # bob inbox again — should see reactions on parent (none yet, since alice
    # only reacted to reply) — but ALSO no new mail because we already polled.
    # We need to re-poll bob's inbox unread=0 to see existing mails with
    # reactions attached.
    # Actually alice's reaction was on the reply, which is alice's inbox view.
    # Re-poll alice's inbox via unread=0 to see persistent state.
    alice_all = get_json(f"{base}/inbox?name=alice&unread=0&limit=10", token)
    found = [m for m in alice_all["messages"] if m["id"] == reply_id]
    assert len(found) == 1
    assert any(rx["actor"] == "alice" and rx["emoji"] == "✅"
                for rx in found[0]["reactions"])

    # FTS5: search by keyword finds both messages
    search = get_json(
        f"{base}/search?q=xyzzy&scope=all&limit=10", token)
    matched_ids = {row["id"] for row in search["results"]}
    assert {parent_id, reply_id}.issubset(matched_ids), \
        f"FTS5 should find both kickoff+reply: got {matched_ids}"

    # Scope filter: alice's inbox (mail sent TO alice) is just the reply
    search_alice = get_json(
        f"{base}/search?q=xyzzy&scope=inbox&name=alice&limit=10", token)
    alice_ids = {row["id"] for row in search_alice["results"]}
    assert alice_ids == {reply_id}, f"scope=inbox/alice should only match reply"

    # mark_read clears alice's unread
    post_json(f"{base}/mark_read", token, {"ids": [reply_id]})
    alice_unread = get_json(f"{base}/inbox?name=alice&unread=1&limit=10", token)
    assert len(alice_unread["messages"]) == 0
    print(f"  [OK] mail lifecycle: parent={parent_id} reply={reply_id} "
          f"react+FTS5+mark_read all roundtripped")


def scenario_2_mailing_list_fanout(base: str, token: str) -> None:
    """to=glob fanout creates N message rows, each visible in their owner's inbox."""
    print("\n[scn 2] mailing list fanout")

    # Seed peers — send each a heartbeat-creating message to bring them
    # into the peers table.
    for peer in ("koatag", "koatag-frontend"):
        post_json(f"{base}/send", token,
                   {"from": peer, "to": "_seed", "body": "heartbeat"})

    # Now fanout: send to "koatag*"
    r = post_json(f"{base}/send", token,
                   {"from": "alice", "to": "koatag*",
                    "body": "broadcast to all koatag*"})
    # Fanout response shape (c69d385): {fanout: true, pattern, matched_peers,
    # count, messages: [{id, sent_at, to}, ...], expires_at}
    assert r.get("fanout") is True, f"expected fanout response shape: {r}"
    assert r["count"] >= 2, f"expected ≥2 recipients in fanout: {r}"
    recipients = set(r["matched_peers"])
    assert "koatag" in recipients and "koatag-frontend" in recipients, \
        f"expected fanout to 2 peers, got {recipients}"

    # Each recipient gets a copy
    for peer in ("koatag", "koatag-frontend"):
        inb = get_json(f"{base}/inbox?name={peer}&unread=1&limit=10", token)
        bodies = [m["body"] for m in inb["messages"]]
        assert "broadcast to all koatag*" in bodies, \
            f"{peer} missing fanout message: {bodies}"

    print(f"  [OK] fanout reached {len(recipients)} peer(s): {sorted(recipients)}")


def scenario_3_ttl_pruning(base: str, token: str, db: Path) -> None:
    """expires_at past + manual retention sweep → expired mail deleted, fresh stays."""
    print("\n[scn 3] TTL pruning via retention sweep")

    # Send one normal mail and one already-expired (1hr in the past)
    past = "2026-05-22T00:00:00.000Z"  # before test workdir creation
    fresh = post_json(f"{base}/send", token,
                       {"from": "ttl-sender", "to": "ttl-recv",
                        "body": "fresh message", "expires_at": None})
    expired = post_json(f"{base}/send", token,
                         {"from": "ttl-sender", "to": "ttl-recv",
                          "body": "stale message", "expires_at": past})

    # Pre-sweep: both visible in inbox
    inb = get_json(f"{base}/inbox?name=ttl-recv&unread=1&limit=10", token)
    ids = {m["id"] for m in inb["messages"]}
    assert {fresh["id"], expired["id"]}.issubset(ids)

    # /health shows pending sweep
    h_pre = get_json(f"{base}/health", "")  # /health is unauthenticated
    assert h_pre.get("ttl_expired_pending_sweep", 0) >= 1, \
        f"expected ≥1 expired pending: {h_pre}"

    # Run sweep via the CLI subprocess (matches operator workflow)
    here = Path(__file__).parent
    result = subprocess.run(
        [sys.executable, str(here / "mailbox-retention.py"),
         "--db", str(db), "--once", "--json"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"retention CLI failed: {result.stderr}"
    counters = json.loads(result.stdout)["counters"]
    assert counters["expired_messages_deleted"] >= 1, \
        f"sweep didn't delete expired mail: {counters}"

    # Post-sweep: only fresh survives
    inb_post = get_json(f"{base}/inbox?name=ttl-recv&unread=1&limit=10", token)
    ids_post = {m["id"] for m in inb_post["messages"]}
    assert fresh["id"] in ids_post
    assert expired["id"] not in ids_post

    h_post = get_json(f"{base}/health", "")
    assert h_post.get("ttl_expired_pending_sweep", 99) == 0, \
        f"expected 0 expired pending after sweep: {h_post}"
    print(f"  [OK] TTL: 1 expired deleted, fresh survived; "
          f"ttl_expired_pending_sweep {h_pre.get('ttl_expired_pending_sweep')} → "
          f"{h_post.get('ttl_expired_pending_sweep')}")


def scenario_4_webhook_delivery(base: str, token: str, db: Path) -> None:
    """Register webhook → send mail → daemon delivers → receiver verifies HMAC."""
    print("\n[scn 4] webhook delivery + HMAC verify")

    # Start a local receiver thread
    receiver = http.server.HTTPServer(("127.0.0.1", 0), WebhookReceiver)
    receiver_port = receiver.server_address[1]
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    receiver_url = f"http://127.0.0.1:{receiver_port}/hook"

    try:
        _RECEIVER_HITS.clear()

        # Register webhook via the admin CLI (matches operator workflow)
        here = Path(__file__).parent
        add_result = subprocess.run(
            [sys.executable, str(here / "mailbox-webhooks.py"),
             "--db", str(db), "--json",
             "--add", "integration-test-hook", "--url", receiver_url],
            capture_output=True, text=True, timeout=10,
        )
        assert add_result.returncode == 0, \
            f"webhook --add failed: {add_result.stderr}"
        webhook_row = json.loads(add_result.stdout)
        secret = webhook_row["secret_hmac"]

        # Trigger a new mail
        post_json(f"{base}/send", token,
                   {"from": "wh-sender", "to": "wh-recv",
                    "body": "webhook trigger message"})

        # Wait for daemon tick (5s default) + a buffer
        deadline = time.time() + 15
        while time.time() < deadline and not _RECEIVER_HITS:
            time.sleep(0.5)

        # Daemon since_id starts at MAX(id) on server boot, BUT in our test the
        # server boots with empty DB so since_id=0 — it'll deliver all earlier
        # scenario messages too. Find the one we triggered specifically.
        deadline2 = time.time() + 15
        target_hit = None
        while time.time() < deadline2 and not target_hit:
            for h in _RECEIVER_HITS:
                if h["parsed"]["message"]["body"] == "webhook trigger message":
                    target_hit = h
                    break
            if not target_hit:
                time.sleep(0.5)
        assert target_hit, (
            f"webhook never got the trigger message (got {len(_RECEIVER_HITS)} "
            f"other deliveries: "
            f"{[h['parsed']['message']['body'][:40] for h in _RECEIVER_HITS]})"
        )
        # HMAC verify
        sig = target_hit["headers"]["X-Mailbox-Sig"]
        expected = "sha256=" + hmac.new(
            secret.encode(), target_hit["body"], hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(expected, sig), \
            f"HMAC mismatch: server sent {sig}, expected {expected}"
        # Payload shape
        assert target_hit["parsed"]["event"] == "mail"
        print(f"  [OK] webhook delivered ({len(_RECEIVER_HITS)} total fires, "
              f"trigger hit verified) + HMAC valid")
    finally:
        receiver.shutdown()


def scenario_5_audit_forensics(base: str, token: str, db: Path) -> None:
    """Query /audit endpoint, verify all earlier actions were logged."""
    print("\n[scn 5] audit forensics — verify all earlier scenarios appear")

    # Pull full audit log
    audit = get_json(f"{base}/audit?limit=500", token)
    rows = audit["rows"]
    assert audit["count"] > 0, "audit log empty after scenarios"

    actions = {r["action"] for r in rows}
    # Must include at least these from prior scenarios:
    expected = {"send", "inbox", "mark_read", "react", "search"}
    missing = expected - actions
    assert not missing, f"audit missing actions: {missing} (got {actions})"

    # Actors: should include scenario participants
    actors = {r["actor"] for r in rows}
    expected_actors = {"alice", "bob", "wh-sender", "ttl-sender"}
    actor_overlap = expected_actors & actors
    assert len(actor_overlap) >= 3, \
        f"audit missing scenario actors: expected ≥3 of {expected_actors}, got {actor_overlap}"

    # Filter — only react entries
    react_rows = get_json(
        f"{base}/audit?action=react&limit=50", token,
    )["rows"]
    assert all(r["action"] == "react" for r in react_rows)
    assert len(react_rows) >= 1, "no react audit rows"

    # Filter — alice's events
    alice_rows = get_json(
        f"{base}/audit?actor=alice&limit=50", token,
    )["rows"]
    assert all(r["actor"] == "alice" for r in alice_rows)
    assert len(alice_rows) >= 3, f"alice should have ≥3 audit rows: {len(alice_rows)}"

    print(f"  [OK] audit: {audit['count']} total rows, "
          f"{len(actions)} distinct actions: {sorted(actions)}")


# ---------- Main harness ----------

def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-integration-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

    # Force webhook daemon to use 1s ticks so the test doesn't have to wait
    # 5s for delivery. There's no env override for this currently — accept
    # the 5s default (kept room in scenario 4's deadline).

    env = os.environ.copy()
    env["CLAUDE_MAILBOX_TOKEN"] = token
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

        scenario_1_mail_lifecycle(base, token)
        scenario_2_mailing_list_fanout(base, token)
        scenario_3_ttl_pruning(base, token, db)
        scenario_4_webhook_delivery(base, token, db)
        scenario_5_audit_forensics(base, token, db)

        print("\n[smoke] ALL INTEGRATION SCENARIOS PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


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
