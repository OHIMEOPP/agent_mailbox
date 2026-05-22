"""Smoke test for mailbox webhooks.

Spins a tiny HTTP receiver in-process to verify the full delivery pipeline:
register → seed message → deliver_pending → assert receiver got POST with
correct HMAC + payload.

Test plan:
  1. init_schema is idempotent + creates both tables
  2. register/list/delete round-trip; secret masking on list
  3. End-to-end: insert message → deliver_pending → receiver sees it; HMAC
     verifies; correct payload shape
  4. Filter glob: to-glob filters out non-matching messages
  5. Retry: receiver 500 → retries up to MAX_ATTEMPTS → marks failed
  6. since_id high-water mark: re-running deliver_pending with new since_id
     doesn't re-enqueue
  7. MAILBOX_WEBHOOKS_DISABLED=1 env skips daemon work
  8. verify_signature() helper
"""
import http.server
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import mailbox_webhooks  # noqa: E402


# Shared global so the request handler can stash received POSTs for the test.
_RECEIVED: list[dict] = []
_FAIL_FIRST_N: dict = {"remaining": 0}


class TestReceiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        # Optionally simulate transient failures
        if _FAIL_FIRST_N["remaining"] > 0:
            _FAIL_FIRST_N["remaining"] -= 1
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"simulated failure")
            return
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        _RECEIVED.append({
            "body_bytes": body,
            "json": parsed,
            "headers": dict(self.headers),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        # Silence the per-request log noise
        return


def _start_receiver() -> tuple[http.server.HTTPServer, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), TestReceiver)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}/hook"


def _init_messages_table(db: Path) -> None:
    """Mirror server.py schema enough for webhook tests."""
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
    """)
    conn.commit()
    conn.close()


def _insert_msg(db: Path, from_name: str, to_name: str, body: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "INSERT INTO messages(from_name, to_name, body) VALUES(?, ?, ?) "
            "RETURNING id",
            (from_name, to_name, body),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def test_1_ddl_idempotent(db: Path) -> None:
    print("\n[test 1] init_schema idempotent + both tables present")
    mailbox_webhooks.init_schema(db)
    mailbox_webhooks.init_schema(db)  # no-op

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "webhooks" in tables
        assert "webhook_deliveries" in tables
    finally:
        conn.close()
    print("  webhooks + webhook_deliveries tables ok")


def test_2_register_list_delete(db: Path) -> None:
    print("\n[test 2] register / list / delete round-trip + secret masking")
    _init_messages_table(db)
    mailbox_webhooks.init_schema(db)

    row = mailbox_webhooks.register(
        db, name="slack", url="http://example.com/hook",
        filter_to_glob="koatag*",
    )
    assert row["id"] >= 1
    assert row["name"] == "slack"
    assert row["secret_hmac"]  # secret returned on register
    assert row["filter_to_glob"] == "koatag*"
    assert bool(row["active"]) is True

    # list (default) masks secret
    rows_masked = mailbox_webhooks.list_webhooks(db)
    assert len(rows_masked) == 1
    assert rows_masked[0]["secret_hmac"] == "***"

    # list with include_secret=True exposes it
    rows_unmasked = mailbox_webhooks.list_webhooks(db, include_secret=True)
    assert rows_unmasked[0]["secret_hmac"] == row["secret_hmac"]

    # set_active toggle
    assert mailbox_webhooks.set_active(db, row["id"], False) == 1
    rows2 = mailbox_webhooks.list_webhooks(db)
    assert bool(rows2[0]["active"]) is False

    # delete cascades (no deliveries yet — just verify rowcount)
    assert mailbox_webhooks.delete(db, row["id"]) == 1
    assert mailbox_webhooks.list_webhooks(db) == []
    print("  register/list/delete/set_active ok")


def test_3_end_to_end_delivery(db: Path) -> None:
    print("\n[test 3] end-to-end deliver_pending → receiver → HMAC verify")
    server, url = _start_receiver()
    try:
        _RECEIVED.clear()
        _init_messages_table(db)
        mailbox_webhooks.init_schema(db)

        wh = mailbox_webhooks.register(db, name="e2e", url=url)
        secret = wh["secret_hmac"]

        m1 = _insert_msg(db, "wiki", "koatag", "hello e2e")
        m2 = _insert_msg(db, "koatag", "wiki", "reply")

        counters = mailbox_webhooks.deliver_pending(db, since_id=0)
        assert counters["messages_scanned"] == 2
        assert counters["deliveries_enqueued"] == 2
        assert counters["deliveries_succeeded"] == 2
        assert counters["deliveries_failed"] == 0
        assert counters["new_since_id"] == m2

        # Receiver got 2 POSTs
        assert len(_RECEIVED) == 2
        for got in _RECEIVED:
            payload = got["json"]
            assert payload["event"] == "mail"
            assert "id" in payload["message"]
            assert payload["message"]["body"] in ("hello e2e", "reply")
            # HMAC verification
            sig = got["headers"]["X-Mailbox-Sig"]
            assert mailbox_webhooks.verify_signature(
                got["body_bytes"], sig, secret,
            )

        # Deliveries marked success
        deliveries = mailbox_webhooks.list_deliveries(db)
        assert len(deliveries) == 2
        assert all(d["status"] == "success" for d in deliveries)
        assert all(d["attempts"] == 1 for d in deliveries)
        print("  2 deliveries succeeded; HMAC verified; payload shape ok")
    finally:
        server.shutdown()


def test_4_filter_glob(db: Path) -> None:
    print("\n[test 4] filter_to_glob / filter_from_glob")
    server, url = _start_receiver()
    try:
        _RECEIVED.clear()
        _init_messages_table(db)
        mailbox_webhooks.init_schema(db)

        # Only fire for koatag* recipients
        mailbox_webhooks.register(db, name="koatag-only", url=url,
                                   filter_to_glob="koatag*")

        m_match = _insert_msg(db, "wiki", "koatag", "should fire")
        m_match2 = _insert_msg(db, "wiki", "koatag-frontend", "also match")
        m_skip = _insert_msg(db, "wiki", "stranger-conv", "skip")

        counters = mailbox_webhooks.deliver_pending(db, since_id=0)
        # All 3 messages scanned, but only 2 enqueued (koatag glob match)
        assert counters["messages_scanned"] == 3
        assert counters["deliveries_enqueued"] == 2
        assert len(_RECEIVED) == 2
        bodies = {p["json"]["message"]["body"] for p in _RECEIVED}
        assert bodies == {"should fire", "also match"}
        print("  glob filter excluded 1/3 messages")
    finally:
        server.shutdown()


def test_5_retry_then_fail(db: Path) -> None:
    print("\n[test 5] retry up to MAX_ATTEMPTS, mark failed")
    server, url = _start_receiver()
    try:
        _RECEIVED.clear()
        _FAIL_FIRST_N["remaining"] = mailbox_webhooks.MAX_ATTEMPTS  # always fail
        _init_messages_table(db)
        mailbox_webhooks.init_schema(db)
        wh = mailbox_webhooks.register(db, name="flaky", url=url)
        _insert_msg(db, "wiki", "koatag", "retry me")

        # Each daemon tick attempts ALL pending rows once. So we need
        # to call deliver_pending MAX_ATTEMPTS times to exhaust attempts.
        last_counters = None
        for _ in range(mailbox_webhooks.MAX_ATTEMPTS):
            last_counters = mailbox_webhooks.deliver_pending(db, since_id=0)

        deliveries = mailbox_webhooks.list_deliveries(db)
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d["status"] == "failed", f"expected failed, got {d['status']} ({d})"
        assert d["attempts"] == mailbox_webhooks.MAX_ATTEMPTS
        assert d["response_code"] == 500

        # Subsequent tick must not attempt again (attempts cap)
        _FAIL_FIRST_N["remaining"] = 0  # would now succeed, but cap blocks retry
        counters_after = mailbox_webhooks.deliver_pending(db, since_id=0)
        assert counters_after["deliveries_succeeded"] == 0, \
            "failed delivery shouldn't retry past cap"
        assert counters_after["deliveries_enqueued"] == 0, \
            "no new messages, no new enqueue"

        # Verify last_error recorded on webhook
        wh_after = mailbox_webhooks.list_webhooks(db)[0]
        assert wh_after["last_error"], f"expected last_error set: {wh_after}"
        print(f"  failed after {mailbox_webhooks.MAX_ATTEMPTS} attempts; "
              f"last_error: {wh_after['last_error'][:60]}")
    finally:
        _FAIL_FIRST_N["remaining"] = 0
        server.shutdown()


def test_6_since_id_high_water_mark(db: Path) -> None:
    print("\n[test 6] since_id advances; re-runs don't re-enqueue")
    server, url = _start_receiver()
    try:
        _RECEIVED.clear()
        _init_messages_table(db)
        mailbox_webhooks.init_schema(db)
        mailbox_webhooks.register(db, name="hw", url=url)

        m1 = _insert_msg(db, "wiki", "koatag", "first")
        c1 = mailbox_webhooks.deliver_pending(db, since_id=0)
        assert c1["deliveries_enqueued"] == 1
        assert c1["new_since_id"] == m1

        # Re-run with same since_id — dedup should prevent re-enqueue
        c2 = mailbox_webhooks.deliver_pending(db, since_id=0)
        assert c2["deliveries_enqueued"] == 0, \
            "dedup should skip already-enqueued delivery"

        # New message, since_id from previous tick
        m2 = _insert_msg(db, "wiki", "koatag", "second")
        c3 = mailbox_webhooks.deliver_pending(db, since_id=c1["new_since_id"])
        assert c3["messages_scanned"] == 1  # only the new one
        assert c3["deliveries_enqueued"] == 1
        assert c3["new_since_id"] == m2

        print(f"  since_id watermark: 0→{m1}→{m2}; no double-enqueue")
    finally:
        server.shutdown()


def test_7_disabled_env_skips(db: Path) -> None:
    print("\n[test 7] MAILBOX_WEBHOOKS_DISABLED=1 skips daemon work")
    server, url = _start_receiver()
    try:
        _RECEIVED.clear()
        _init_messages_table(db)
        mailbox_webhooks.init_schema(db)
        mailbox_webhooks.register(db, name="disabled-test", url=url)
        _insert_msg(db, "wiki", "koatag", "should not fire")

        os.environ["MAILBOX_WEBHOOKS_DISABLED"] = "1"
        try:
            counters = mailbox_webhooks.deliver_pending(db, since_id=0)
            assert counters["messages_scanned"] == 0, \
                f"disabled should skip scan: {counters}"
            assert counters["deliveries_enqueued"] == 0
        finally:
            del os.environ["MAILBOX_WEBHOOKS_DISABLED"]

        # Confirm un-disable resumes work
        counters_after = mailbox_webhooks.deliver_pending(db, since_id=0)
        assert counters_after["deliveries_succeeded"] == 1
        print("  disabled env skipped; post-unset delivery worked")
    finally:
        server.shutdown()


def test_8_verify_signature(db: Path) -> None:
    print("\n[test 8] verify_signature() helper")
    body = b'{"event":"mail","message":{"id":1}}'
    secret = "test-secret-xyzzy"
    # Compute the matching signature
    import hmac
    import hashlib
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    sig = f"sha256={mac.hexdigest()}"

    assert mailbox_webhooks.verify_signature(body, sig, secret) is True
    assert mailbox_webhooks.verify_signature(body, sig, "wrong-secret") is False
    assert mailbox_webhooks.verify_signature(b"tampered" + body, sig, secret) is False
    assert mailbox_webhooks.verify_signature(body, "sha256=bogus", secret) is False
    print("  verify_signature: positive + 3 negatives ok")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-webhooks-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_ddl_idempotent,
                                 test_2_register_list_delete,
                                 test_3_end_to_end_delivery,
                                 test_4_filter_glob,
                                 test_5_retry_then_fail,
                                 test_6_since_id_high_water_mark,
                                 test_7_disabled_env_skips,
                                 test_8_verify_signature), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL WEBHOOK TESTS PASSED")
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
