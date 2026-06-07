#!/usr/bin/env python3
"""Post a digest (or an error notice) DIRECTLY to Discord via REST (bot token).

VM 版投遞：不經本機 :1904 bridge，直接打 Discord REST API
（POST /channels/{id}/messages，Authorization: Bot <token>）。
REST 送訊息不需要 gateway，與 VM 上 Node-RED bot 的 gateway 連線互不衝突。

從同目錄的 .secrets（KEY=VALUE）讀取：
  DISCORD_BOT_TOKEN=...
  DISCORD_CHANNEL_ID=...

用法：
  python3 post-to-discord.py digest-out.md
  python3 post-to-discord.py --error "原因"
"""
import sys
import os
import json
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, ".secrets")
MAX = 1900  # Discord 單則上限 2000，留點餘裕給分段標記


def load_secrets():
    d = {}
    with open(SECRETS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def post(token, channel, content):
    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    data = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "koatag-digest/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def chunk(text, size):
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
    s = load_secrets()
    token = s.get("DISCORD_BOT_TOKEN")
    channel = s.get("DISCORD_CHANNEL_ID")
    if not token or not channel:
        print("ERROR: .secrets 缺 DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID")
        sys.exit(2)

    if len(sys.argv) >= 3 and sys.argv[1] == "--error":
        post(token, channel, f"❌ **AI/LLM digest 失敗**\n{sys.argv[2]}")
        print("posted error notice")
        return

    if len(sys.argv) < 2:
        print("usage: post-to-discord.py <file> | --error <reason>")
        sys.exit(2)

    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        post(token, channel, "❌ **AI/LLM digest 失敗**\ndigest 內容為空")
        print("empty digest -> posted failure notice")
        return

    parts = chunk(text, MAX)
    n = len(parts)
    for i, p in enumerate(parts, 1):
        content = p if n == 1 else f"**({i}/{n})**\n{p}"
        if i > 1:
            time.sleep(1)  # 稍微避開 Discord rate limit
        code = post(token, channel, content)
        print(f"part {i}/{n}: HTTP {code}")


if __name__ == "__main__":
    main()
