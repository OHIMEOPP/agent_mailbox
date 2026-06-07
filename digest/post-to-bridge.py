#!/usr/bin/env python3
"""Post a digest (or an error notice) to the mailbox Discord bridge (:1904).

UTF-8 safe, auto-chunked to respect Discord's 2000-char message cap.

Usage:
    py post-to-bridge.py digest-out.md          # deliver a digest file
    py post-to-bridge.py --error "reason text"  # deliver a failure notice
"""
import sys
import io
import json
import urllib.request

# Windows Git Bash stdout can be cp950 — wrap so printing the response never crashes.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BRIDGE = "http://localhost:1904/agent-notify"
AGENT = "wiki"
TASK = "AI/LLM 每日 digest"
MAX = 1800  # Discord caps a message at 2000 chars; leave room for the bridge header.


def post(task, detail, status="info"):
    body = {"agent": AGENT, "task": task, "status": status, "detail": detail}
    req = urllib.request.Request(
        BRIDGE,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def chunk(text, size):
    """Split on line boundaries, hard-splitting any single over-long line."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--error":
        post(TASK + " — 失敗", sys.argv[2], status="fail")
        print("posted error notice")
        return

    if len(sys.argv) < 2:
        print("usage: post-to-bridge.py <digest-file> | --error <reason>")
        sys.exit(2)

    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        post(TASK + " — 失敗", "digest 內容為空", status="fail")
        print("empty digest -> posted failure notice")
        return

    parts = chunk(text, MAX)
    n = len(parts)
    for i, part in enumerate(parts, 1):
        task = TASK if n == 1 else f"{TASK} ({i}/{n})"
        code = post(task, part)
        print(f"part {i}/{n}: HTTP {code}")


if __name__ == "__main__":
    main()
