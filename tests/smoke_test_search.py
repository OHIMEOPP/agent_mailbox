"""Smoke test for FTS5 search.

Tests both REST /search endpoint and mailbox_server's _init_fts setup:
  1. Server boots, /search rejects empty / bad params
  2. Insert some messages, /search returns matching rows ranked by relevance
  3. FTS5 keeps in sync via triggers (INSERT/DELETE auto-mirrors)
  4. CJK content searchable when space-tokenized
  5. Boolean / phrase / prefix syntax works
  6. scope=inbox / scope=sent / scope=all filtering correct
  7. /search 501 if FTS5 absent — skip (hard to simulate without rebuilding sqlite)

The mailbox-server.py subprocess is the same pattern used by smoke_test_attach.
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
                payload = json.loads(r.read().decode("utf-8"))
                if payload.get("ok"):
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


def get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-search-smoke-"))
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

        # ---- Test 1: bad params ----
        try:
            get_json(f"{base}/search", token)
            print("[smoke] FAIL: missing q accepted", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print("[smoke] /search without q → 400 ok")

        try:
            get_json(f"{base}/search?q=foo&scope=garbage", token)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print("[smoke] bad scope → 400 ok")

        # ---- Test 2: seed messages + search ----
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "the quick brown fox jumps over the lazy dog"})
        post_json(f"{base}/send", token,
                  {"from": "carol", "to": "bob", "body": "snapshot zip uploaded to backup retention"})
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "lazy afternoon coding session"})
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "dave", "body": "completely unrelated"})
        post_json(f"{base}/send", token,
                  {"from": "bob", "to": "alice", "body": "reply about lazy fox to alice"})

        # /search for "lazy" → bob's inbox should match msg 1 + msg 3 (NOT msg 5
        # which is bob→alice, not in bob's inbox)
        r = get_json(f"{base}/search?q=lazy&name=bob&scope=inbox", token)
        ids = sorted(x["id"] for x in r["results"])
        assert ids == [1, 3], f"lazy in bob's inbox: expected ids [1,3], got {ids}"
        print(f"[smoke] FTS5 keyword 'lazy' ranks {len(r['results'])} results in bob's inbox: {ids}")

        # ---- Test 3: phrase search ----
        r2 = get_json(f"{base}/search?q=" + urllib.parse.quote('"quick brown fox"') +
                      "&name=bob&scope=inbox", token)
        assert len(r2["results"]) == 1 and r2["results"][0]["id"] == 1, \
            f"phrase search expected msg 1, got {r2['results']}"
        print("[smoke] phrase search ok")

        # ---- Test 4: boolean OR ----
        r3 = get_json(f"{base}/search?q=" + urllib.parse.quote("snapshot OR lazy") +
                      "&name=bob&scope=inbox", token)
        ids3 = sorted(x["id"] for x in r3["results"])
        assert ids3 == [1, 2, 3], f"OR search: expected [1,2,3], got {ids3}"
        print(f"[smoke] boolean OR ok ({len(r3['results'])} results)")

        # ---- Test 5: prefix search ----
        r4 = get_json(f"{base}/search?q=" + urllib.parse.quote("snap*") +
                      "&name=bob&scope=inbox", token)
        ids4 = sorted(x["id"] for x in r4["results"])
        assert ids4 == [2], f"prefix search expected [2], got {ids4}"
        print("[smoke] prefix search ok")

        # ---- Test 6: scope=sent ----
        r5 = get_json(f"{base}/search?q=" + urllib.parse.quote("lazy") +
                      "&name=alice&scope=sent", token)
        ids5 = sorted(x["id"] for x in r5["results"])
        # alice sent msgs 1, 3, 4 (and 5 is bob→alice). Lazy in alice-sent: 1, 3
        assert ids5 == [1, 3], f"scope=sent for alice: expected [1,3], got {ids5}"
        print("[smoke] scope=sent ok")

        # ---- Test 7: scope=all ----
        r6 = get_json(f"{base}/search?q=" + urllib.parse.quote("lazy") +
                      "&scope=all", token)
        ids6 = sorted(x["id"] for x in r6["results"])
        # lazy appears in msgs 1, 3, 5 (across all senders/recipients)
        assert ids6 == [1, 3, 5], f"scope=all: expected [1,3,5], got {ids6}"
        print("[smoke] scope=all ok")

        # ---- Test 8: snippet contains <b>...</b> ----
        snippet = r["results"][0]["snippet"]
        assert "<b>" in snippet and "</b>" in snippet, \
            f"snippet missing highlight: {snippet!r}"
        print(f"[smoke] snippet highlight ok: {snippet[:80]!r}")

        # ---- Test 9: CJK content searchable (space-tokenized) ----
        # FTS5 unicode61 tokenizer treats CJK as single tokens at character
        # boundaries by default. Test with space-separated CJK.
        post_json(f"{base}/send", token,
                  {"from": "alice", "to": "bob", "body": "中文 訊息 內容 測試"})
        r7 = get_json(f"{base}/search?q=" + urllib.parse.quote("訊息") +
                      "&name=bob&scope=inbox", token)
        assert any(x["id"] == 6 for x in r7["results"]), \
            f"CJK search didn't find msg 6: {r7}"
        print(f"[smoke] CJK search ok ({len(r7['results'])} results)")

        # ---- Test 10: invalid FTS5 query → 400 ----
        try:
            # unmatched quote → FTS5 syntax error
            get_json(f"{base}/search?q=" + urllib.parse.quote('"oops') +
                     "&name=bob&scope=inbox", token)
            print("[smoke] FAIL: bad query accepted", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print("[smoke] invalid FTS5 query → 400 ok")

        # ---- Test 11: delete trigger keeps FTS in sync ----
        # Delete msg 1 → search shouldn't find "fox" any more
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM messages WHERE id=1")
        conn.commit()
        conn.close()
        r8 = get_json(f"{base}/search?q=fox&name=bob&scope=inbox", token)
        ids8 = [x["id"] for x in r8["results"]]
        assert 1 not in ids8, f"delete trigger broken: msg 1 still in FTS: {ids8}"
        # msg 5 still has "fox" so should match (if it's in bob's inbox — no,
        # msg 5 is bob→alice, so not in bob's inbox). Search should be empty
        # or only show what remains.
        print(f"[smoke] delete trigger ok (msg 1 removed from FTS, remaining={ids8})")

        print(f"\n[smoke] ALL SEARCH TESTS PASSED")
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
