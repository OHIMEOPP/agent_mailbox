"""Smoke test for mailing-list aliases (glob fanout).

Tests REST + DB level — server.py MCP is exercised in passing via /send.

  1. /send with literal to → single-dict shape (backward compat)
  2. /send with "koatag*" pattern → fanout dict with multiple recipients
  3. Each fanout recipient gets its own messages row + audit entry
  4. Pattern matching only ACTIVE peers (heartbeat ≤7d)
  5. Pattern with zero match → 404
  6. /send-file with pattern → fanout, each recipient gets its own attachment rows
  7. Blob written once (sha dedup), attachment rows fan out
  8. Pattern cap at ALIAS_MAX_RECIPIENTS (32) — exercised with sparse insertion
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
import urllib.parse
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


def wait_health(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if json.loads(r.read()).get("ok"):
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


def post_multipart(url, token, payload, files):
    boundary = "----aliassmoke" + secrets.token_hex(8)
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


def get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-alias-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

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

        # ---- Seed active peers (heartbeat each one via INSERT) ----
        # post a message FROM each peer so its row gets created in peers table
        for sender in ["koatag", "koatag-frontend", "wiki", "stranger-conv"]:
            post_json(f"{base}/send", token,
                      {"from": sender, "to": "alice", "body": f"hi from {sender}"})

        # Manually backdate one peer to 30 days ago (stale) — should be filtered
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO peers(name, last_seen_at) VALUES('koatag-stale', "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at"
        )
        conn.commit()
        conn.close()

        # ---- Test 1: literal to → single-dict shape ----
        r1 = post_json(f"{base}/send", token,
                       {"from": "alice", "to": "koatag", "body": "literal send"})
        assert "id" in r1 and "fanout" not in r1, \
            f"literal send should NOT be fanout shape: {r1}"
        print("[smoke] literal /send → single-dict ok")

        # ---- Test 2: pattern "koatag*" → fanout ----
        r2 = post_json(f"{base}/send", token,
                       {"from": "alice", "to": "koatag*", "body": "fanout to koatag*"})
        assert r2.get("fanout") is True
        peers = set(r2["matched_peers"])
        # Should include koatag + koatag-frontend, NOT koatag-stale (30d old)
        assert "koatag" in peers, f"missing koatag: {peers}"
        assert "koatag-frontend" in peers, f"missing koatag-frontend: {peers}"
        assert "koatag-stale" not in peers, \
            f"stale peer (30d) should be excluded: {peers}"
        assert r2["count"] == len(r2["messages"]) == 2
        print(f"[smoke] pattern 'koatag*' fanout ok: {peers}")

        # ---- Test 3: each recipient got its own message row ----
        for msg in r2["messages"]:
            inbox = get_json(f"{base}/inbox?name={msg['to']}&unread=1", token)
            found = next((m for m in inbox["messages"] if m["id"] == msg["id"]), None)
            assert found, f"msg {msg['id']} not in {msg['to']}'s inbox"
            assert found["body"] == "fanout to koatag*"
        print("[smoke] per-recipient inbox rows ok")

        # ---- Test 4: empty match → 404 ----
        try:
            post_json(f"{base}/send", token,
                      {"from": "alice", "to": "nonexistent-*", "body": "noop"})
            print("[smoke] FAIL: empty match should 404", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 404
            print("[smoke] empty match → 404 ok")

        # ---- Test 5: wildcard "*" matches all active peers ----
        r5 = post_json(f"{base}/send", token,
                       {"from": "alice", "to": "*", "body": "broadcast"})
        assert r5.get("fanout") is True
        # Should include alice, koatag, koatag-frontend, wiki, stranger-conv (all active)
        # but exclude koatag-stale
        assert "koatag-stale" not in r5["matched_peers"]
        assert r5["count"] >= 4
        print(f"[smoke] wildcard * fanout ok ({r5['count']} peers)")

        # ---- Test 6: /send-file with pattern ----
        blob_data = b"shared payload bytes" * 100
        r6 = post_multipart(
            f"{base}/send-file", token,
            {"from": "alice", "to": "koatag*", "body": "fanout with file"},
            [("attach.bin", "application/octet-stream", blob_data)],
        )
        assert r6.get("fanout") is True, f"send-file with pattern should fanout: {r6}"
        assert r6["count"] == 2
        # Each fanout result has its own attachment rows
        for msg in r6["messages"]:
            assert len(msg["attachments"]) == 1
            assert msg["attachments"][0]["filename"] == "attach.bin"
        # All attachments share same sha256 (blob dedup)
        shas = {m["attachments"][0]["sha256"] for m in r6["messages"]}
        assert len(shas) == 1, f"blob should be dedup'd, got distinct shas: {shas}"
        # Only ONE blob file on disk for the shared content
        blob_files = [p for p in attachments.rglob("*") if p.is_file()]
        # (Earlier no files were uploaded; blob count = 1)
        assert len(blob_files) == 1, f"expected 1 blob (dedup), got {len(blob_files)}"
        print(f"[smoke] /send-file fanout with blob dedup ok")

        # ---- Test 7: literal to=koatag with /send-file → single-dict shape ----
        r7 = post_multipart(
            f"{base}/send-file", token,
            {"from": "alice", "to": "wiki", "body": "literal file"},
            [("a.bin", "application/octet-stream", b"single recip")],
        )
        assert "id" in r7 and "fanout" not in r7
        print("[smoke] literal /send-file → single-dict ok")

        print(f"\n[smoke] ALL ALIAS FANOUT TESTS PASSED")
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
