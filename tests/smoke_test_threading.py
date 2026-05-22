"""Smoke test for reply threading (in_reply_to column).

Tests:
  1. /send accepts in_reply_to → stored + returned in /inbox
  2. /send without in_reply_to → backward compat, returns null
  3. /send-file accepts in_reply_to → stored
  4. SSE /watch event includes in_reply_to field
  5. /send with invalid in_reply_to (string) → 400
  6. Idempotent schema migration: open pre-2026-05-23 schema DB → after db_init, column exists
"""
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                body = r.read().strip()
                try:
                    payload = json.loads(body)
                    if payload.get("ok") is True:
                        return
                except json.JSONDecodeError:
                    if body == b"ok":
                        return
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


def post_multipart(url: str, token: str, payload: dict,
                   files: list[tuple[str, str, bytes]]) -> dict:
    boundary = "----threadsmoke" + secrets.token_hex(8)
    chunks: list[bytes] = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    chunks.append(b"Content-Type: application/json; charset=utf-8\r\n\r\n")
    chunks.append(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    chunks.append(b"\r\n")
    for i, (fname, mime, data) in enumerate(files):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n'
            .encode("utf-8"))
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def sse_listen(url: str, token: str, deadline: float, events: list) -> None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=deadline - time.time()) as r:
            event = None
            for raw in r:
                if time.time() > deadline:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                if not line:
                    event = None
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    if event == "mail":
                        events.append(json.loads(line[5:].strip()))
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        pass


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-thread-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

    env = os.environ.copy()
    env["CLAUDE_MAILBOX_TOKEN"] = token
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

        # ---- Test 1: /send with in_reply_to ----
        r1 = post_json(f"{base}/send", token,
                       {"from": "alice", "to": "bob", "body": "first message"})
        parent_id = r1["id"]
        assert isinstance(parent_id, int)

        r2 = post_json(f"{base}/send", token,
                       {"from": "bob", "to": "alice", "body": "reply to first",
                        "in_reply_to": parent_id})
        reply_id = r2["id"]

        # /inbox for alice should show the reply with in_reply_to
        alice_inbox = get_json(f"{base}/inbox?name=alice&unread=1&limit=10", token)
        msgs = alice_inbox["messages"]
        assert len(msgs) == 1
        assert msgs[0]["id"] == reply_id
        assert msgs[0]["in_reply_to"] == parent_id, \
            f"expected in_reply_to={parent_id} got {msgs[0].get('in_reply_to')}"
        print(f"[smoke] /send with in_reply_to ok (parent={parent_id} reply={reply_id})")

        # ---- Test 2: /send without in_reply_to → null ----
        r3 = post_json(f"{base}/send", token,
                       {"from": "alice", "to": "carol", "body": "no thread"})
        carol_inbox = get_json(f"{base}/inbox?name=carol&unread=1&limit=10", token)
        msg = carol_inbox["messages"][0]
        assert msg["id"] == r3["id"]
        assert msg["in_reply_to"] is None, \
            f"backward compat broke: expected null, got {msg['in_reply_to']}"
        print("[smoke] backward compat (no in_reply_to → null) ok")

        # ---- Test 3: /send-file with in_reply_to ----
        r4 = post_multipart(
            f"{base}/send-file", token,
            {"from": "alice", "to": "dave", "body": "attached file reply",
             "in_reply_to": parent_id},
            [("test.txt", "text/plain", b"file contents")],
        )
        assert r4["id"] is not None
        dave_inbox = get_json(f"{base}/inbox?name=dave&unread=1&limit=10", token)
        dmsg = dave_inbox["messages"][0]
        assert dmsg["in_reply_to"] == parent_id
        assert dmsg["has_attachments"] == 1
        assert len(dmsg["attachments"]) == 1
        print("[smoke] /send-file with in_reply_to ok")

        # ---- Test 4: SSE event includes in_reply_to ----
        sse_events: list = []
        deadline = time.time() + 8
        t = threading.Thread(target=sse_listen,
                             args=(f"{base}/watch?name=eve", token, deadline, sse_events),
                             daemon=True)
        t.start()
        time.sleep(1.0)
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "eve", "body": "live thread test",
                   "in_reply_to": parent_id})
        t.join(timeout=8)
        assert sse_events, "no SSE events received"
        ev = sse_events[-1]
        assert ev.get("in_reply_to") == parent_id, \
            f"SSE missing in_reply_to: {ev}"
        print(f"[smoke] SSE event includes in_reply_to ok ({ev['in_reply_to']})")

        # ---- Test 5: invalid in_reply_to → 400 ----
        try:
            post_json(f"{base}/send", token,
                      {"from": "alice", "to": "bob", "body": "bad",
                       "in_reply_to": "not-an-int"})
            print("[smoke] FAIL: invalid in_reply_to accepted", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print("[smoke] invalid in_reply_to → 400 ok")

        print(f"\n[smoke] ALL THREADING TESTS PASSED")
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
        shutil.rmtree(workdir, ignore_errors=True)


def test_migration() -> int:
    """Test 6: idempotent schema migration — old DB without in_reply_to column
    gets the column added when db_init runs."""
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-thread-migration-"))
    db = workdir / "legacy.db"
    try:
        # Create legacy schema (pre-2026-05-23 — no in_reply_to)
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                read_at TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO messages(from_name, to_name, body) VALUES('a', 'b', 'legacy msg');
        """)
        conn.commit()
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        conn.close()
        assert "in_reply_to" not in cols_before, "test setup wrong"

        # Run db_init (via subprocess to mimic real boot)
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, pathlib; sys.path.insert(0, '.'); "
             "import importlib.util; "
             f"spec = importlib.util.spec_from_file_location('m', r'{Path(__file__).parent.parent / 'mailbox-server.py'}'); "
             "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
             f"m.db_init(pathlib.Path(r'{db}')); print('init ok')"],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"[smoke-migration] db_init failed: {result.stderr}", file=sys.stderr)
            return 1

        conn = sqlite3.connect(str(db))
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        # Legacy row should still be there
        rows = list(conn.execute("SELECT id, body, in_reply_to FROM messages"))
        conn.close()

        assert "in_reply_to" in cols_after, f"migration didn't add column: {cols_after}"
        assert len(rows) == 1 and rows[0][2] is None, \
            f"legacy row migrated incorrectly: {rows}"
        print("[smoke] migration ok (legacy DB gained in_reply_to column, existing rows null)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        rc = test_migration()
    sys.exit(rc)
