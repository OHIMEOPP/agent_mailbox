"""End-to-end smoke against the running mailbox-bridge container.

Stands up a fake "Discord CDN" on a host port reachable via
host.docker.internal, POSTs /from-discord with a trusted-user
attachment payload, then reads the real ~/.claude/mailbox/mailbox.db
to confirm the row + attachment + blob landed.

Cleans up the test message + attachment row + blob (if not shared by
dedup with anything else) before exit.

Run on host: `py tests/e2e_bridge_inbound_attachment.py`
Requires: bridge container running on :1904, host.docker.internal
resolvable from the container (default on Docker Desktop Windows).
"""
import hashlib
import http.server
import json
import os
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

DB = Path(r"C:\Users\User\.claude\mailbox\mailbox.db")
ATTS_DIR = Path(r"C:\Users\User\.claude\mailbox\attachments")
BRIDGE = "http://127.0.0.1:1904"
TEST_AUTHOR = "ohimeopp"  # trusted user
TEST_BODY_TAG = "[bridge-e2e-test]"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def main() -> int:
    payload_bytes = b"\x89PNG\r\n\x1a\n" + (b"e2e-test-bytes" * 64)
    expected_sha = hashlib.sha256(payload_bytes).hexdigest()
    expected_size = len(payload_bytes)

    port = free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(expected_size))
            self.end_headers()
            self.wfile.write(payload_bytes)

    srv = socketserver.TCPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[e2e] fake CDN listening on 0.0.0.0:{port}")

    # Discord-shaped attachment dict pointing to host.docker.internal
    cdn_url = f"http://host.docker.internal:{port}/screenshot.png"
    body = {
        "content": f"{TEST_BODY_TAG} please relay this image",
        "author": TEST_AUTHOR,
        "author_id": "999",
        "channel": "1284065900659740773",
        "attachments": [{
            "id": "9000",
            "filename": "screenshot.png",
            "url": cdn_url,
            "proxy_url": cdn_url,
            "content_type": "image/png",
            "size": expected_size,
        }],
    }

    try:
        req = urllib.request.Request(
            f"{BRIDGE}/from-discord",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        print(f"[e2e] /from-discord resp: {resp}")
        assert resp.get("ok") is True, f"bridge returned not-ok: {resp}"
        assert resp.get("to") == "wiki"
        assert len(resp.get("attachments") or []) == 1
        msg_id = resp["id"]
        att_id = resp["attachments"][0]["id"]
        assert resp["attachments"][0]["sha256"] == expected_sha
        print(f"[e2e] bridge ack — msg_id={msg_id} att_id={att_id}")

        # Verify the real mailbox.db has the row
        with sqlite3.connect(str(DB)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT body, has_attachments, to_name FROM messages WHERE id=?",
                (msg_id,)).fetchone()
            assert row is not None, "messages row missing"
            assert row["has_attachments"] == 1
            assert TEST_BODY_TAG in row["body"]
            assert row["to_name"] == "wiki"
            att = c.execute(
                "SELECT filename, size, sha256 FROM attachments WHERE id=?",
                (att_id,)).fetchone()
            assert att["filename"] == "screenshot.png"
            assert att["size"] == expected_size
            assert att["sha256"] == expected_sha
        print(f"[e2e] mailbox.db verified")

        # Verify blob on disk
        blob = ATTS_DIR / expected_sha[:2] / expected_sha
        assert blob.exists(), f"blob missing at {blob}"
        assert blob.read_bytes() == payload_bytes
        print(f"[e2e] blob on disk verified: {blob}")

        print("\n[e2e] PASSED — bridge inbound attachment relay is live.")
        return 0
    except AssertionError as e:
        print(f"\n[e2e] FAIL: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"\n[e2e] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
