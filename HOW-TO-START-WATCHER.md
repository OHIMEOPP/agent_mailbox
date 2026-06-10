# How to Start the Mailbox Watcher

> Single-page quick reference for any Claude agent / human on any device.
> Place this at session start, after `whoami` resolves the instance name.
>
> **Companion docs**:
> - [README.md](README.md) — overview + cold-start checklist (start here if unsure)
> - [HOW-TO-USE-MAILBOX.md](HOW-TO-USE-MAILBOX.md) — once the watcher fires, how to receive / mark read / reply through the right channel

---

## 1. Resolve your instance name

```
mcp__plugin_agent-mailbox_mailbox__whoami()
```

Returns something like `{ "name": "wiki" }`. Use this as `<NAME>` below.
Common names: `wiki`, `koatag`, `koatag-frontend`, `stranger-conv`.

---

## 2. Start the watcher

### Preferred: Claude Code **Monitor** tool (stream-mode, watcher never dies)

```yaml
tool: Monitor
command:     py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py" <NAME> --monitor
description: mailbox watcher for <NAME>
persistent:  true
timeout_ms:  3600000
```

What you'll see in the task output the first tick:
```
[stderr] [watcher] monitor-mode start name=<NAME> tick=5s baseline_id=<int>
```

Every new mail addressed to `<NAME>` thereafter appears as a stdout line:
```
MAIL id=<int> from=<peer> sent=<iso8601> preview=<first 200 chars>
```
…which the Monitor tool turns into a wake notification. Watcher stays alive.

### Fallback: Bash `run_in_background` (exit-mode, watcher dies on first mail)

If `Monitor` is unavailable (older Claude Code, restricted env):
```yaml
tool: Bash
command:           py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py" <NAME>
run_in_background: true
```

Wake mechanism: watcher exits → harness sends task-completion notification →
agent must **restart** the watcher after handling the mail (no auto-revive).
The `feedback_watcher_always_on` memory documents this discipline if you have
project-level memory.

---

## 3. Verify watcher is alive

Per-tick heartbeat writes to `peers.last_seen_at` in the mailbox DB:

```bash
py -c "import sqlite3; db=sqlite3.connect(r'C:/Users/User/.claude/mailbox/mailbox.db'); print(db.execute(\"SELECT name, last_seen_at FROM peers WHERE name=?\",('<NAME>',)).fetchone())"
```

Should show a timestamp within the last 5 seconds. If not, watcher is dead.

---

## 4. Tell the user (one line)

> mailbox watcher 已啟動（Monitor stream-mode，每封 mail 一條 notification，watcher 不死）

---

## Cross-device notes

### Linux / macOS

Replace Windows path with the actual repo location:
```
python3 "/home/<user>/code/claude-mailbox/mailbox-watch.py" <NAME> --monitor
```

DB lives at `~/.claude/mailbox/mailbox.db` (relative to `$HOME`). The watcher
auto-discovers via the hardcoded `DB` constant — adjust `mailbox-watch.py:49`
if your DB lives elsewhere, or pass `--db <path>`.

### Multiple agents same machine

Each agent runs its own watcher with its own `<NAME>`. The `peers` table
filters by `to_name`, so they don't cross-trigger. Two `wiki` watchers can
run simultaneously without breaking — only wasteful, not wrong.

### When to skip starting the watcher

- User explicitly says「不要 watcher」/「這次不用 mailbox」
- Session is one-shot scripting (no expectation of incoming mail)
- Otherwise: **always start it** in the first turn after `whoami`

---

## When the watcher dies

Possible causes:
- OS subprocess killed (reboot, manual kill, container restart on bridge side)
- Python error (rare; SQLite db error is caught + sleep + retry)
- Claude Code session ended (Monitor terminates persistent tasks on session end)

Recovery: just run step 2 again. No state needs to persist — the watcher
baselines on max(id) at startup, so historical mail isn't re-announced.

---

## Sending mail (FYI, not watcher-related)

- **Agent ↔ agent (internal)**: `mcp__plugin_agent-mailbox_mailbox__send(to="<peer>", body="...")` — sits in SQLite, peer's watcher emits it
- **Agent → user Discord DM**: `POST http://localhost:1904/agent-notify` with `agent / task / status / detail` JSON. The `mailbox-bridge` container is **one-way** (Discord→mailbox only); INSERTing `to_name='user-discord'` does NOT reach Discord

---

## Cross-device watch (LAN / VPN, since 2026-05-22)

> **Full end-to-end onboarding** for adding a new machine: [SETUP-CROSS-DEVICE.md](SETUP-CROSS-DEVICE.md).
> This section is the watcher-side quick reference only.

If the agent runs on a different machine than the SQLite mailbox.db, the watcher
can connect to a hub running `mailbox-server.py` over HTTP/SSE instead of
opening the SQLite file directly.

### Hub side (the machine that owns `mailbox.db`)

```bash
# Generate token once
py -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.claude/mailbox/token.txt

# Start REST server (binds 0.0.0.0:1905 by default)
CLAUDE_MAILBOX_TOKEN=$(cat ~/.claude/mailbox/token.txt) \
  py C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-server.py
```

Keep this running on the hub. For Tailscale-only access, pass `--host 100.x.y.z`
(the hub's tailscale IP) instead of the default 0.0.0.0.

### Spoke side (the remote machine running the agent)

```yaml
tool: Monitor
command:     py C:/path/to/mailbox-watch.py <NAME> --remote http://<hub-ip>:1905 --token <TOKEN>
description: mailbox watcher for <NAME> via remote hub
persistent:  true
timeout_ms:  3600000
```

The watcher outputs identical `MAIL id=...` lines to the local --monitor mode,
so Monitor tool consumes events the same way. Auto-reconnects with exponential
backoff on network drop.

### REST endpoints exposed by mailbox-server

| Method | Path | Auth | Use |
|---|---|---|---|
| GET  | `/health`     | none | liveness check |
| POST | `/send`       | bearer | `{from, to, body}` → `{id, sent_at}` |
| GET  | `/inbox?name=X&unread=1&limit=50` | bearer | list of messages |
| POST | `/mark_read`  | bearer | `{ids:[...]}` → `{count}` |
| GET  | `/peers`      | bearer | list of known peers + last_seen_at |
| GET  | `/watch?name=X` | bearer | SSE stream `event: mail\ndata: {...}` |

### Trust model

- Single shared bearer token; LAN/VPN-trusted environment assumed
- No per-peer auth, no rate limiting, no replay protection
- Token leak → fix: rotate, restart server with new env var; clients re-deploy
- Don't expose `0.0.0.0` to public internet without putting a TLS reverse-proxy in front
