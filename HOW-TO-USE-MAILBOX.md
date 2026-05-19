# How to Use the Mailbox (Receive + Reply Flow)

> **Companion docs**:
> - [README.md](README.md) — overview + cold-start checklist (start here if unsure)
> - [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md) — starting the watcher; this doc assumes one is already running
>
> Covers: receiving mail, marking as read, replying through the right channel
> for each peer kind.

---

## Receiving mail

Once your Monitor watcher is up, every new unread mail addressed to your
`<NAME>` becomes a task-notification of this shape:

```
<task-notification>
  <task-id>b3bwqu3kb</task-id>
  <summary>Monitor event: "mailbox watcher ..."</summary>
  <event>MAIL id=759 from=user-discord (ohimeopp) sent=2026-05-19T02:44:51Z preview=...</event>
</task-notification>
```

`from=` reveals the peer kind:

| `from_name` shape | Source | Reply channel |
|---|---|---|
| `wiki` / `koatag` / `koatag-frontend` | another Claude agent | `mcp__mailbox__send` |
| `user-discord (ohimeopp)` | trusted user via Discord DM | `POST :1901/agent-notify` (no channel) |
| `user-discord (<other>) ch=<id>` | stranger via Discord DM | `POST :1901/agent-notify` **with** `channel: <id>` |
| `test-self` | manual SQL insert (testing) | up to you |

The `preview` field is truncated to 200 chars. If you need the full body, run:

```python
import sqlite3
db = sqlite3.connect(r'C:\Users\User\.claude\mailbox\mailbox.db')
db.row_factory = sqlite3.Row
row = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
print(row['body'])
```

Or use the MCP tool if available: `mcp__mailbox__inbox(unread_only=true)`.

---

## Marking as read

**Always mark read immediately** after you've ingested a mail — otherwise the
watcher will re-fire on session restart (it baselines on max(id) per startup,
not on read state, so already-handled mail can resurface).

```python
db.execute("UPDATE messages SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (msg_id,))
db.commit()
```

Or via MCP: `mcp__mailbox__mark_read(ids=[msg_id])`.

---

## Replying — pick the right channel

### A. Reply to another agent (internal)

The agent's watcher will surface your message just like yours surfaces theirs.

```python
mcp__mailbox__send(to="koatag", body="commit 0xdeadbeef is broken, see ...")
```

Or SQL insert (same effect, no MCP needed):

```python
db.execute(
    "INSERT INTO messages(from_name, to_name, body, sent_at) VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
    (your_name, "koatag", body)
)
db.commit()
```

### B. Reply to the trusted user via Discord DM

The bridge is **one-way** (Discord → mailbox only). To reach Discord you must
POST to the node-red endpoint, which the discordBot container forwards to the
user's DM channel.

```python
import urllib.request, json
body = {
    "agent": "wiki",           # your instance name
    "task": "Re: <thread>",    # short title, shows on line 1
    "status": "info",          # info / done / fail / warn -> 📋 / ✅ / ❌ / ⚠️
    "detail": "<actual reply text, multi-line OK>",
}
req = urllib.request.Request(
    "http://localhost:1901/agent-notify",
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"},
)
urllib.request.urlopen(req, timeout=8)
```

Discord renders as:
```
<icon> **[wiki]** <task>
<detail>
```

**Status icon mapping**: `done`→✅, `fail`→❌, `warn`→⚠️, `info`→📋 (default).

#### Critical schema gotcha

The endpoint **silently drops unknown fields**. Sending `{"message": "..."}`
instead of `{"detail": "..."}` produces a DM with **only the icon + agent name**
visible, no body. Always use `agent / task / status / detail`. The endpoint
returns the rendered DM as response body — if you see only `<icon> **[wiki]**`
in the response, your schema is wrong and the user got nothing readable.

### C. Reply to a stranger via Discord DM

When the mail came from `user-discord (<some_username>) ch=<channel_id>`, parse
that channel and include it in the notify payload so the DM lands in the
correct private channel (not the trusted-user DM):

```python
import re, urllib.request, json
m = re.match(r'^user-discord \((.+?)\) ch=(\d+)$', from_name)
username, channel = m.group(1), m.group(2)

body = {
    "agent": "discord-chat",
    "task": "",
    "status": "info",
    "detail": reply_text,
    "channel": channel,           # <-- this routes to the stranger's DM
}
# ... same POST as above
```

This is the `discord-stranger-chat` session flow. The trusted user's DM
**omits** `channel` (defaults to the configured trusted channel).

### ❌ Anti-pattern: replying via `mcp__mailbox__send` to `user-discord`

```python
mcp__mailbox__send(to="user-discord", body="reply text")  # ← WRONG
```

This inserts a row into SQLite but the bridge container does not poll for
outbound rows. The mail sits forever in `read_at IS NULL` state, the user
never sees it. **Use `agent-notify` for anything reaching Discord.**

---

## End-to-end example: handling one user DM

```python
# 1. task-notification fires:
#    MAIL id=759 from=user-discord (ohimeopp) sent=... preview=...
import sqlite3, urllib.request, json, datetime

DB = r'C:\Users\User\.claude\mailbox\mailbox.db'
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

# 2. Fetch full body
row = db.execute("SELECT * FROM messages WHERE id = ?", (759,)).fetchone()
print(row['from_name'], row['body'])

# 3. Compose reply
reply = f"收到，{row['body'][:30]}... 處理中"

# 4. Send via agent-notify (because peer is user-discord)
notify_body = {
    "agent": "wiki",
    "task": "Re: msg #759",
    "status": "info",
    "detail": reply,
}
req = urllib.request.Request(
    "http://localhost:1901/agent-notify",
    data=json.dumps(notify_body, ensure_ascii=False).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"},
)
print(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))

# 5. Mark inbound read
now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
db.execute("UPDATE messages SET read_at = ? WHERE id = ?", (now, 759))
db.commit()
```

---

## When not to notify

Per `feedback_notify_schema` memory + general etiquette:

- **Each mailbox round** → spam, don't
- **Mid-step ack** → noise unless something critical
- **Pure technical detail ack** → low signal
- **Repeating same task across phases** → user fatigue

Do notify on:
- **Round close** (a deploy completed, a phase ended)
- **Production-touching action failed** (user must know)
- **Classifier blocked an action needing user GO**
- **Critical security gap discovered**
- **Long-running autonomous task started / ended**

---

## Encoding pitfalls (CJK)

The notify endpoint accepts UTF-8 JSON only. Three common ways CJK can break:

1. **Bash inline `curl -d '{"detail":"中文"}'`** on Git Bash → Windows shell
   re-encodes the inline string in cp950, server sees mojibake. Use a JSON
   file: `curl --data-binary @reply.json` or use Python.
2. **`py -c "..."` with backticks in the body** → bash tries to command-
   substitute the backticks. Write the body to a file then load it.
3. **Source `.py` file with CJK string literals on Windows + run via Bash** →
   default encoding can be cp1252. Always declare `# -*- coding: utf-8 -*-`
   or use `py -X utf8` flag.

See [snapshot/memory-reference_agent_discord_notify.md](snapshot/memory-reference_agent_discord_notify.md)
for the full pattern set.
