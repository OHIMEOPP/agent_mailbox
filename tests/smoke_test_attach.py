"""Smoke test for mailbox attachment transfer.

Spawns mailbox-server.py against a temp DB+attachments dir, then:
  1. POST /send-file with two test files (one binary, one text/CJK filename)
  2. GET /inbox — verify message + attachments metadata
  3. GET /attachment/<id> for each — verify bytes match sha256
  4. SSE /watch — connect, send another /send-file, verify event payload
     includes attachments list
"""
import hashlib
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, timeout: float = 10.0) -> None:
    """Wait for server /health endpoint. Since 2026-05-23 it returns JSON;
    accept either {"ok": true, ...} or legacy text "ok"."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                body = r.read().strip()
                if body == b"ok":
                    return
                try:
                    payload = json.loads(body)
                    if payload.get("ok") is True:
                        return
                except json.JSONDecodeError:
                    pass
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server never came up at {url}")


def post_multipart(url: str, token: str, payload: dict,
                   files: list[tuple[str, str, bytes]]) -> dict:
    boundary = "----smoketest" + secrets.token_hex(8)
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


def get_bytes(url: str, token: str) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), dict(r.headers.items())


def sse_listen(url: str, token: str, deadline: float, events: list) -> None:
    """Connect to SSE, append each mail event JSON to events list, until deadline."""
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
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-smoke-"))
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
    failures: list[str] = []
    try:
        wait_health(base, timeout=15)
        print("[smoke] server up")

        # ---- Test 1: POST /send-file ----
        binary_data = b"\x00\x01\x02\x03" + b"hello binary" * 100
        cjk_text = "測試中文檔名 файл データ".encode("utf-8")
        files_t1 = [
            ("test.bin", "application/octet-stream", binary_data),
            ("中文檔名.txt", "text/plain", cjk_text),
        ]
        resp = post_multipart(
            f"{base}/send-file", token,
            {"from": "hub", "to": "spoke", "body": "snapshot zip + cjk"},
            files_t1,
        )
        assert "id" in resp, f"no id in response: {resp}"
        msg_id = resp["id"]
        assert len(resp["attachments"]) == 2
        print(f"[smoke] send-file ok msg_id={msg_id} attachments={len(resp['attachments'])}")

        # ---- Test 2: GET /inbox ----
        inbox = get_json(f"{base}/inbox?name=spoke&unread=1&limit=10", token)
        assert len(inbox["messages"]) == 1
        msg = inbox["messages"][0]
        assert msg["id"] == msg_id
        assert msg["has_attachments"] == 1
        assert len(msg["attachments"]) == 2
        att_by_name = {a["filename"]: a for a in msg["attachments"]}
        assert "test.bin" in att_by_name
        assert "中文檔名.txt" in att_by_name
        assert att_by_name["test.bin"]["size"] == len(binary_data)
        assert att_by_name["test.bin"]["sha256"] == hashlib.sha256(binary_data).hexdigest()
        print("[smoke] inbox ok")

        # ---- Test 3: GET /attachment/<id> per file ----
        for fname, expected_data in [("test.bin", binary_data), ("中文檔名.txt", cjk_text)]:
            att = att_by_name[fname]
            data, headers = get_bytes(f"{base}/attachment/{att['id']}", token)
            assert data == expected_data, f"data mismatch for {fname}"
            assert hashlib.sha256(data).hexdigest() == att["sha256"]
            assert headers.get("X-Mailbox-Sha256") == att["sha256"]
            cd = headers.get("Content-Disposition", "")
            assert "filename=" in cd, f"missing Content-Disposition filename for {fname}: {cd}"
            print(f"[smoke] download ok: {fname} ({len(data)}B, cd={cd[:60]}...)")

        # ---- Test 4: SSE watch sees attachments ----
        sse_events: list = []
        deadline = time.time() + 8
        t = threading.Thread(target=sse_listen,
                             args=(f"{base}/watch?name=spoke2", token, deadline, sse_events),
                             daemon=True)
        t.start()
        time.sleep(1.0)  # let baseline establish
        # Send a second message addressed to spoke2 (so SSE sees it fresh)
        post_multipart(
            f"{base}/send-file", token,
            {"from": "hub", "to": "spoke2", "body": "live event test"},
            [("live.txt", "text/plain", b"live test payload")],
        )
        t.join(timeout=8)
        assert sse_events, "SSE listener saw no mail events"
        ev = sse_events[-1]
        assert ev["to_name"] == "spoke2"
        assert ev["has_attachments"] == 1
        assert isinstance(ev.get("attachments"), list) and len(ev["attachments"]) == 1
        assert ev["attachments"][0]["filename"] == "live.txt"
        print(f"[smoke] SSE ok — event includes attachments: {ev['attachments']}")

        # ---- Test 5: dedup — re-send same bytes should reuse blob ----
        before_files = sum(1 for _ in attachments.rglob("*") if _.is_file())
        post_multipart(
            f"{base}/send-file", token,
            {"from": "hub", "to": "spoke3", "body": "dedup check"},
            [("dup.bin", "application/octet-stream", binary_data)],
        )
        after_files = sum(1 for _ in attachments.rglob("*") if _.is_file())
        assert after_files == before_files, f"dedup failed: blobs grew {before_files}→{after_files}"
        print(f"[smoke] dedup ok — blob count stayed at {after_files}")

        # ---- Test 6: size limit ----
        oversize = b"X" * (101 * 1024 * 1024)  # 101 MB > 100MB single limit
        try:
            post_multipart(
                f"{base}/send-file", token,
                {"from": "hub", "to": "spoke", "body": "oversize"},
                [("huge.bin", "application/octet-stream", oversize)],
            )
            failures.append("oversize 101MB was accepted (expected 413)")
        except urllib.error.HTTPError as e:
            assert e.code == 413, f"expected 413, got {e.code}"
            print("[smoke] size limit ok — 413 on 101MB file")

        print(f"\n[smoke] ALL TESTS PASSED ({0 if not failures else len(failures)} failures)")
        return 0 if not failures else 1
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # dump server stderr for diagnosis
        try:
            err = proc.stderr.read()
            if err:
                print("\n--- server stderr ---", file=sys.stderr)
                print(err, file=sys.stderr)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
