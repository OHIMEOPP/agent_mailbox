"""Smoke test for bridge inbound Discord attachment relay.

Stands up a temp SQLite + tiny HTTP server serving fake "Discord CDN" bytes,
calls bridge.inbound.process_discord_inbound, asserts:
  - messages row inserted with has_attachments=1
  - attachments rows match (filename / size / sha256)
  - blob written to <db_dir>/attachments/<sha[:2]>/<sha>
  - response payload echoes the stored attachments
  - empty content + no attachments => 400
  - empty content + attachments => succeeds (caption-less image DM)
"""
import hashlib
import http.server
import os
import shutil
import socket
import socketserver
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


def _init_db(db_path: str) -> None:
    """Subset of server.py:_init_db sufficient for inbound integration."""
    with sqlite3.connect(db_path) as c:
        c.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                read_at TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE peers (
                name TEXT PRIMARY KEY,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                filename TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        """)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_fake_cdn(port: int, payloads: dict[str, bytes],
                    error_keys: dict[str, int] | None = None) -> socketserver.TCPServer:
    """Tiny HTTP server: GET /<key> returns payloads[key], or error_keys[key]
    HTTP status. Used to simulate Discord media proxy 415 on non-image."""
    error_keys = error_keys or {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            pass  # quiet

        def do_GET(self):
            key = self.path.lstrip("/")
            if key in error_keys:
                self.send_response(error_keys[key])
                self.end_headers()
                return
            if key not in payloads:
                self.send_response(404)
                self.end_headers()
                return
            data = payloads[key]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = socketserver.TCPServer(("127.0.0.1", port), Handler)
    srv.allow_reuse_address = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv  # caller can srv.shutdown()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="bridge-attach-smoke-"))
    db = workdir / "mailbox.db"
    atts_dir = workdir / "attachments"
    atts_dir.mkdir()

    # bridge.whitelist hardcodes WHITELIST_DB to /data/whitelist.db — override
    # for this test via env, then import bridge AFTER setting it.
    os.environ["WHITELIST_DB"] = str(workdir / "whitelist.db")
    # Use ohimeopp as the trusted user (matches config default) so we skip the
    # stranger gate and land in the trusted-user fast path.
    os.environ["TRUSTED_DISCORD_USER"] = "tester"

    _init_db(str(db))
    # Seed a heartbeat so notify_offline isn't triggered (avoids outbound POST
    # to a Discord NOTIFY_URL that doesn't exist in this test).
    with sqlite3.connect(str(db)) as c:
        c.execute("INSERT INTO peers(name, last_seen_at) "
                  "VALUES('wiki', strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        c.commit()

    # Lazy import so env vars above take effect.
    from bridge.inbound import process_discord_inbound

    port = _free_port()
    img_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 200
    txt_bytes = "image caption attached.txt 中文".encode("utf-8")
    srv = _spawn_fake_cdn(
        port,
        {
            "img.png": img_bytes,
            "note.txt": txt_bytes,
            "data.xlsx": b"PK\x03\x04" + b"fake-xlsx" * 100,
        },
        # Simulates Discord media proxy returning 415 for non-image — we
        # exercise the url fallback path (proxy_url 415 -> url 200).
        error_keys={"proxy/data.xlsx": 415},
    )

    failures: list[str] = []
    try:
        # --- Test 1: text + 2 attachments succeeds ---
        atts_in = [
            {"id": "100", "filename": "img.png",
             "proxy_url": f"http://127.0.0.1:{port}/img.png",
             "url": f"http://127.0.0.1:{port}/img.png",
             "content_type": "image/png", "size": len(img_bytes)},
            {"id": "101", "filename": "note.txt",
             "proxy_url": f"http://127.0.0.1:{port}/note.txt",
             "url": f"http://127.0.0.1:{port}/note.txt",
             "content_type": "text/plain", "size": len(txt_bytes)},
        ]
        status, resp = process_discord_inbound(
            content="here's the screenshot",
            author="tester", author_id="42", channel="999",
            to_name_hint=None, db_path=str(db),
            attachments=atts_in,
        )
        assert status == 200, f"expected 200, got {status}: {resp}"
        assert resp["ok"] is True
        assert resp["to"] == "wiki"
        assert len(resp["attachments"]) == 2
        msg_id = resp["id"]
        print(f"[smoke] inbound ok msg_id={msg_id} resp_atts={len(resp['attachments'])}")

        # Verify messages row
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT body, has_attachments FROM messages WHERE id=?",
                            (msg_id,)).fetchone()
            assert row["body"] == "here's the screenshot"
            assert row["has_attachments"] == 1
            atts_db = c.execute(
                "SELECT filename, size, sha256 FROM attachments "
                "WHERE message_id=? ORDER BY id", (msg_id,)).fetchall()
        assert len(atts_db) == 2
        by_name = {a["filename"]: a for a in atts_db}
        assert by_name["img.png"]["size"] == len(img_bytes)
        assert by_name["img.png"]["sha256"] == hashlib.sha256(img_bytes).hexdigest()
        assert by_name["note.txt"]["size"] == len(txt_bytes)
        assert by_name["note.txt"]["sha256"] == hashlib.sha256(txt_bytes).hexdigest()
        print("[smoke] db rows ok")

        # Verify blobs on disk
        for fname, payload in [("img.png", img_bytes), ("note.txt", txt_bytes)]:
            sha = hashlib.sha256(payload).hexdigest()
            blob = atts_dir / sha[:2] / sha
            assert blob.exists(), f"blob missing for {fname}: {blob}"
            assert blob.read_bytes() == payload, f"blob mismatch for {fname}"
        print("[smoke] blobs on disk ok")

        # --- Test 2: empty content + attachment succeeds (caption-less image) ---
        atts_in2 = [
            {"id": "200", "filename": "img2.png",
             "proxy_url": f"http://127.0.0.1:{port}/img.png",
             "content_type": "image/png", "size": len(img_bytes)},
        ]
        status, resp = process_discord_inbound(
            content="", author="tester", author_id="42",
            channel="999", to_name_hint=None, db_path=str(db),
            attachments=atts_in2,
        )
        assert status == 200, f"caption-less image rejected: {status} {resp}"
        assert len(resp["attachments"]) == 1
        print("[smoke] caption-less attachment ok")

        # --- Test 3: empty content + no attachments => 400 ---
        status, resp = process_discord_inbound(
            content="", author="tester", author_id="42",
            channel="999", to_name_hint=None, db_path=str(db),
            attachments=[],
        )
        assert status == 400, f"expected 400 on empty/empty, got {status}: {resp}"
        print("[smoke] empty rejection ok")

        # --- Test 4: download failure leaves has_attachments=0 + caption intact ---
        atts_in4 = [
            {"id": "300", "filename": "missing.bin",
             "proxy_url": f"http://127.0.0.1:{port}/does-not-exist",
             "content_type": "application/octet-stream", "size": 0},
        ]
        status, resp = process_discord_inbound(
            content="caption survives",
            author="tester", author_id="42", channel="999",
            to_name_hint=None, db_path=str(db),
            attachments=atts_in4,
        )
        assert status == 200
        assert resp["attachments"] == []
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT body, has_attachments FROM messages WHERE id=?",
                            (resp["id"],)).fetchone()
            assert row["body"] == "caption survives"
            assert row["has_attachments"] == 0, \
                "has_attachments should be rolled back to 0 when all downloads fail"
        print("[smoke] partial-failure rollback ok")

        # --- Test 5: dedup — same sha256 reuses blob, but new attachments row ---
        # Re-send img.png — blob already exists, INSERT into attachments creates new row
        before = sum(1 for _ in atts_dir.rglob("*") if _.is_file())
        status, resp = process_discord_inbound(
            content="dedup check",
            author="tester", author_id="42", channel="999",
            to_name_hint=None, db_path=str(db),
            attachments=[atts_in[0]],
        )
        after = sum(1 for _ in atts_dir.rglob("*") if _.is_file())
        assert after == before, f"dedup failed: blobs {before} -> {after}"
        assert status == 200 and len(resp["attachments"]) == 1
        print(f"[smoke] dedup ok — blob count steady at {after}")

        # --- Test 6: proxy_url 415 -> url fallback (the #1539 xlsx bug) ---
        # Simulates Discord media proxy refusing non-image content: proxy_url
        # returns 415 but url succeeds. We use url-first ordering so this
        # should download cleanly from url on the first try.
        xlsx_payload = b"PK\x03\x04" + b"fake-xlsx" * 100
        xlsx_sha = hashlib.sha256(xlsx_payload).hexdigest()
        atts_in6 = [
            {"id": "600", "filename": "data.xlsx",
             # proxy_url points to 415 endpoint; url points to working endpoint
             "proxy_url": f"http://127.0.0.1:{port}/proxy/data.xlsx",
             "url": f"http://127.0.0.1:{port}/data.xlsx",
             "content_type": "application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet",
             "size": len(xlsx_payload)},
        ]
        status, resp = process_discord_inbound(
            content="excel sheet",
            author="tester", author_id="42", channel="999",
            to_name_hint=None, db_path=str(db),
            attachments=atts_in6,
        )
        assert status == 200, f"xlsx via url failed: {status} {resp}"
        assert len(resp["attachments"]) == 1
        assert resp["attachments"][0]["sha256"] == xlsx_sha
        assert resp["attachments"][0]["filename"] == "data.xlsx"
        print("[smoke] non-image via url ok (proxy_url 415 fallback path)")

        # --- Test 7: only proxy_url available + it 415s => skip cleanly ---
        atts_in7 = [
            {"id": "700", "filename": "weird.xlsx",
             "proxy_url": f"http://127.0.0.1:{port}/proxy/data.xlsx",
             # no url field
             "content_type": "application/octet-stream", "size": 1},
        ]
        status, resp = process_discord_inbound(
            content="all-proxy attempt",
            author="tester", author_id="42", channel="999",
            to_name_hint=None, db_path=str(db),
            attachments=atts_in7,
        )
        assert status == 200
        assert resp["attachments"] == []
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT has_attachments FROM messages WHERE id=?",
                            (resp["id"],)).fetchone()
            assert row["has_attachments"] == 0
        print("[smoke] proxy-only 415 cleanly skips + has_attachments=0")

        print(f"\n[smoke] ALL TESTS PASSED ({len(failures)} failures)")
        return 0 if not failures else 1
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3
    finally:
        srv.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
