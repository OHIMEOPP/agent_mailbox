# Cross-device mailbox setup

End-to-end onboarding for adding a second machine (laptop / tablet / future
mobile / Tailscale-connected VPS) to an existing mailbox hub.

> If you're the first machine setting up mailbox at all, see [README.md](README.md)
> and [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md) first. This document
> assumes you already have a working single-machine mailbox.

## Architecture

```
┌──────── HUB (your desktop) ─────────┐         ┌──── SPOKE (laptop) ────┐
│                                      │         │                          │
│  C:/Users/User/.claude/mailbox/      │         │  no local mailbox.db     │
│  └─ mailbox.db (single writer)       │         │  (stateless)             │
│                                      │         │                          │
│  Local agents:                       │  LAN /  │  Claude Code session     │
│   - server.py (mcp__mailbox)         │  VPN    │   - server.py with       │
│   - mailbox-watch.py (local SQLite)  │  ◀────▶ │     CLAUDE_MAILBOX_REMOTE│
│   - mailbox-bridge :1904 (Discord)   │  HTTP   │     → routes via REST    │
│                                      │  +SSE   │   - mailbox-watch.py     │
│  mailbox-server.py :1905 ◀───────────┼─────────┤     --remote (SSE)       │
│   serves REST/SSE to spokes          │         │                          │
└──────────────────────────────────────┘         └──────────────────────────┘
```

Single source of truth (the SQLite file) lives only on the hub. Spokes are
HTTP clients — no DB to drift, no merge conflicts.

---

## Phase 0 — Hub prep (run on the desktop)

### 0.1 Pull latest

```powershell
cd C:\Users\User\Desktop\VSCcode\claude-mailbox
git pull
```

### 0.2 Generate a shared token

```powershell
py -c "import secrets; print(secrets.token_urlsafe(32))" > C:\Users\User\.claude\mailbox\token.txt
type C:\Users\User\.claude\mailbox\token.txt
```

Keep this safe — every spoke needs it. Rotate by overwriting this file and
restarting `mailbox-server.py` + every spoke.

### 0.3 Find hub LAN IP

```powershell
ipconfig | findstr IPv4
```

Pick the 192.168.x.x line that matches your home wifi/ethernet. Optional:
reserve this IP in your router's DHCP so it doesn't change.

### 0.4 Start the server (foreground, smoke test first)

```powershell
$env:CLAUDE_MAILBOX_TOKEN = Get-Content C:\Users\User\.claude\mailbox\token.txt
py C:\Users\User\Desktop\VSCcode\claude-mailbox\mailbox-server.py
```

Expected output:
```
[mailbox-server] listening on http://0.0.0.0:1905  db=C:\Users\User\.claude\mailbox\mailbox.db
[mailbox-server] bearer token: <prefix>... (length 43)
```

In a second shell verify locally:
```powershell
curl http://127.0.0.1:1905/health
# ok
```

### 0.5 Allow inbound :1905 in Windows Firewall

```powershell
# Run as Administrator
New-NetFirewallRule -DisplayName "mailbox-server :1905" `
    -Direction Inbound -Protocol TCP -LocalPort 1905 `
    -RemoteAddress 192.168.0.0/16 -Action Allow
```

Adjust `RemoteAddress` to your LAN range (e.g. `100.64.0.0/10` for Tailscale-only).

### 0.6 Make it survive reboot (optional)

Quickest: Task Scheduler → "At log on" → Action:
```
py.exe  C:\Users\User\Desktop\VSCcode\claude-mailbox\mailbox-server.py
```
with `CLAUDE_MAILBOX_TOKEN` env set on the action (Settings tab → "Environment").

Cleaner: wrap with [NSSM](https://nssm.cc/) into a Windows Service so it
restarts on crash.

---

## Phase 1 — Spoke setup (run on the laptop)

### 1.1 Prerequisites

- Python 3.10+ on PATH (`py --version`)
- Claude Code installed
- Git (to clone the mailbox repo)

### 1.2 Install the mcp Python package

```powershell
pip install mcp
# or with uv:
# uv pip install mcp
```

### 1.3 Clone the mailbox repo

```powershell
git clone https://github.com/OHIMEOPP/agent_mailbox.git C:\path\to\claude-mailbox
```

Pick any path — it doesn't have to match the hub's path.

### 1.4 Get the token & hub IP from the hub side

You need:
- `<TOKEN>` from `~/.claude/mailbox/token.txt` on the hub
- `<HUB_IP>` from `ipconfig` step 0.3 (or Tailscale 100.x.y.z)

### 1.5 Pre-flight: verify hub is reachable

```powershell
curl http://<HUB_IP>:1905/health
# Expected: ok

curl -H "Authorization: Bearer <TOKEN>" http://<HUB_IP>:1905/peers
# Expected: JSON list of peers including the hub's known agents
```

If either fails → fix before touching `.mcp.json`. Common causes:
- Hub firewall (Phase 0.5)
- Wrong IP (re-run `ipconfig` on hub)
- mailbox-server.py not running (Phase 0.4)
- Token mismatch (re-copy from `token.txt`)

### 1.6 Configure Claude Code MCP

In any project on the laptop, create or edit `.mcp.json`:

```json
{
  "mcpServers": {
    "mailbox": {
      "command": "python",
      "args": ["C:/path/to/claude-mailbox/server.py"],
      "env": {
        "CLAUDE_MAILBOX_NAME": "laptop",
        "CLAUDE_MAILBOX_REMOTE": "http://<HUB_IP>:1905",
        "CLAUDE_MAILBOX_TOKEN": "<TOKEN>"
      }
    }
  }
}
```

Pick `CLAUDE_MAILBOX_NAME` to be unique across all machines (e.g. `laptop`,
`thinkpad`, `tablet-zen`).

Because `CLAUDE_MAILBOX_REMOTE` is set, `server.py` will route every MCP tool
(`send`, `inbox`, `mark_read`, `peers`, `whoami`) through REST. It will not
create a local SQLite file. Verify by:

```python
# In Claude Code, call:
mcp__mailbox__whoami()
# Expected: {"name": "laptop", "mode": "remote", "hub": "http://<HUB_IP>:1905"}
```

### 1.7 Start the watcher

Use the `Monitor` tool (preferred — stream-mode, never dies):

```yaml
Monitor:
  command: py "C:/path/to/claude-mailbox/mailbox-watch.py" laptop
  description: mailbox watcher for laptop (remote)
  persistent: true
  timeout_ms: 3600000
```

The watcher reads `CLAUDE_MAILBOX_REMOTE` and `CLAUDE_MAILBOX_TOKEN` from env
(no `--remote` flag needed when MCP server set them in `.mcp.json` env block).
If env vars aren't visible to the watcher process, pass explicitly:

```bash
py "C:/path/to/claude-mailbox/mailbox-watch.py" laptop --remote http://<HUB_IP>:1905 --token <TOKEN>
```

Expected first-line stderr:
```
[watcher] remote-mode connect: http://<HUB_IP>:1905  name=laptop
[watcher] connected, streaming events
```

### 1.8 Smoke test the round trip

From the hub, send a mail:
```python
mcp__mailbox__send(to="laptop", body="hello from hub")
```

The laptop watcher should immediately emit one stdout line:
```
MAIL id=<N> from=<hub-name> sent=<ts> preview=hello from hub
```

…which Claude Code's Monitor tool turns into an in-conversation notification.

From the laptop, send back:
```python
mcp__mailbox__send(to="wiki", body="hello from laptop")  # or whoever
```

Hub-side watcher sees it.

---

## Phase 2 — Tailscale add-on (optional, for off-LAN access)

If you want the laptop to work from coffee shops / outside home network:

1. Install Tailscale on **both** hub and laptop, log in to the same account.
2. On hub: `tailscale ip` → returns `100.x.y.z`. This is the hub's Tailscale address.
3. On laptop's `.mcp.json`, change `CLAUDE_MAILBOX_REMOTE` from `http://192.168.1.10:1905`
   to `http://100.x.y.z:1905`.
4. Optional: tighten firewall (Phase 0.5) to allow only Tailscale range:
   ```
   -RemoteAddress 100.64.0.0/10
   ```
5. No protocol change — Tailscale is just a different IP for the same HTTP server.

For mobile (future): Tailscale has iOS/Android apps. Same approach.

---

## Phase 3 — Verification checklist

After both phases, run this checklist on the laptop:

```powershell
# 1. Hub reachable
curl http://<HUB_IP>:1905/health
# Expected: ok

# 2. Auth works
curl -H "Authorization: Bearer <TOKEN>" http://<HUB_IP>:1905/peers
# Expected: JSON listing peers, no 401

# 3. MCP whoami says remote
# (in Claude Code) mcp__mailbox__whoami()
# Expected: {"name": "laptop", "mode": "remote", "hub": "http://<HUB_IP>:1905"}

# 4. No ghost DB on laptop
dir C:\Users\<your-user>\.claude\mailbox\
# Expected: empty or no mailbox.db

# 5. Watcher heartbeating
# (in Claude Code on hub) mcp__mailbox__peers()
# Expected: "laptop" entry with recent last_seen_at (within last minute)

# 6. Round-trip mail
# (on hub) mcp__mailbox__send(to="laptop", body="ping")
# (on laptop) watcher emits MAIL line within ~2 seconds
```

All 6 → cross-device setup complete.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl /health` connection refused | server not running OR firewall block | Phase 0.4 + 0.5 |
| `curl /peers` returns 401 | wrong token | re-copy token.txt; remove leading/trailing whitespace |
| `whoami` returns `mode: local` not `remote` | env var not propagated to MCP server | check `.mcp.json` env block; restart Claude Code session |
| ghost `mailbox.db` appears on laptop | `.mcp.json` missing `CLAUDE_MAILBOX_REMOTE` env | re-check spelling; ensure both REMOTE + TOKEN are set |
| Watcher exits immediately with 401 | token typo | check `CLAUDE_MAILBOX_TOKEN` env in watcher launch context |
| Watcher reconnects every 2 sec | hub serving but auth fails or path 404 | check `--remote` URL has no trailing slash; verify server log shows the connect |
| Mail sent but spoke watcher silent | wrong `CLAUDE_MAILBOX_NAME` mismatch | watcher's name must match recipient name on hub's send |
| New laptop sessions create local DB | `.mcp.json` env block missing on **that** project | env config is per-project, copy `.mcp.json` to every project that uses mailbox |

---

## Token rotation

If a token leaks:
1. Hub: regenerate (`py -c "import secrets; print(secrets.token_urlsafe(32))" > token.txt`)
2. Hub: restart `mailbox-server.py` with new env
3. Spokes: update `.mcp.json` `CLAUDE_MAILBOX_TOKEN` value, restart Claude Code session
4. No DB migration needed — token is per-connection only.

---

## What is NOT installed on the spoke

You do **not** need on the laptop:
- `mailbox-server.py` (hub-only — only the writer runs this)
- `mailbox-bridge` (Discord container — hub-only)
- `mailbox-followup.py` (admin tool — hub-only)
- `mailbox-whitelist.py` (admin tool — hub-only)

Just `server.py` (MCP, with REMOTE env) + `mailbox-watch.py` (SSE client).
That's it.
