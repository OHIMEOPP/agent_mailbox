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

### 0.4 Start the server via docker compose (preferred)

`mailbox-server` is wired into the same `bridge/docker-compose.yml` as the
existing `mailbox-bridge`. Both share `~/.claude/mailbox/` volume so they hit
the same SQLite.

**Add token to `.env`** (gitignored, same file the bridge already reads):

```powershell
cd C:\Users\User\Desktop\VSCcode\claude-mailbox\bridge
# If .env doesn't exist yet:  cp .env.example .env
# Append the token:
$tok = Get-Content C:\Users\User\.claude\mailbox\token.txt
Add-Content .env "CLAUDE_MAILBOX_TOKEN=$tok"
```

**Bring up the service**:

```powershell
docker compose up -d mailbox-server
```

(Or `docker compose up -d` to ensure both `mailbox-bridge` and `mailbox-server`
are running.)

**Verify**:

```powershell
docker compose logs --tail 5 mailbox-server
# Expected:
#   [mailbox-server] listening on http://0.0.0.0:1905  db=/data/mailbox.db
#   [mailbox-server] bearer token: <prefix>... (length 43)

curl http://127.0.0.1:1905/health
# ok
```

Container has `restart: always` so it survives Docker Desktop restart / reboot
automatically — no Task Scheduler / NSSM needed.

**Manual run (debug-only fallback)**:

```powershell
$env:CLAUDE_MAILBOX_TOKEN = Get-Content C:\Users\User\.claude\mailbox\token.txt
py C:\Users\User\Desktop\VSCcode\claude-mailbox\mailbox-server.py
```

Use this only when diagnosing — e.g. to see live stderr without `docker logs`.

### 0.5 Allow inbound :1905 in Windows Firewall

Run as **Administrator**. Use one-line form — backtick line-continuation often
gets eaten when pasted into the PowerShell window (the 2nd/3rd lines run as
separate commands and the rule ends up wide-open with default values).

```powershell
New-NetFirewallRule -DisplayName "mailbox-server :1905" -Direction Inbound -Protocol TCP -LocalPort 1905 -RemoteAddress 192.168.0.0/16 -Action Allow
```

Adjust `-RemoteAddress` to match your network:
- `192.168.0.0/16` — home Wi-Fi (most common)
- `10.0.0.0/8` / `172.16.0.0/12` — other private ranges
- `100.64.0.0/10` — Tailscale
- `26.0.0.0/8` — Radmin VPN
- Multiple VPNs / mixed: create one rule per range (separate `New-NetFirewallRule` calls), or pass an array: `-RemoteAddress @("192.168.0.0/16","100.64.0.0/10")`

**Symptom**: spoke `curl /health` shows `timed out` (not `refused`) — typically firewall blocking. `refused` = server not running. Ping going through but TCP timeout = firewall.

**Verify** (the rule should report `LocalPort=1905` and `Protocol=TCP`):
```powershell
Get-NetFirewallRule -DisplayName "mailbox-server :1905" | Get-NetFirewallPortFilter
```

If you accidentally created a wide-open rule (default values, no port filter),
fix with:
```powershell
Set-NetFirewallRule -DisplayName "mailbox-server :1905" -Direction Inbound -Protocol TCP -LocalPort 1905 -RemoteAddress 192.168.0.0/16 -Action Allow
```

### 0.6 Make it survive reboot

If you used Phase 0.4's docker compose path, **already done** — container has
`restart: always`. Docker Desktop auto-starts on Windows login, and the
container then auto-starts inside.

(If you're using the manual run fallback, then use Task Scheduler "At log on"
or NSSM to wrap, but docker is the recommended path.)

---

## Phase 1 — Spoke setup (run on the laptop)

### 1.1 Prerequisites

- Python 3.10+ on PATH (`py --version`)
- Claude Code installed
- Git (to clone the mailbox repo)

**Heads-up about pre-existing DBs on the laptop**: if the laptop has been used
with an older version of mailbox before, you may find leftover SQLite files at:
- `~/.claude-mailbox.db` (pre-2026-05 legacy single-file location)
- `~/.claude/mailbox/mailbox.db` (current default location)

Either way, **cross-device spoke setup does NOT read, write, merge, or migrate
these files**. With `CLAUDE_MAILBOX_REMOTE` set (Phase 1.7), `server.py` skips
local SQLite entirely. Legacy DBs become orphan history — leave them, delete
them, or archive them; no impact on cross-device operation.

Do NOT try to merge a legacy laptop DB into the hub DB — id sequences will
collide and read_at semantics get scrambled. Treat legacy laptop DBs as
read-only history at best.

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
- `<HUB_HOSTNAME>` from hub's `$env:COMPUTERNAME` (for sending mail back to hub)

**Typical handoff path**: spoke agent recognizes it needs these three → asks the
user → user fetches from hub-side agent (or directly from `token.txt` /
`ipconfig`) → pastes back into spoke chat. Until token auto-pipeline lands
(see backlog), human-in-the-loop is the channel.

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

### 1.6 Naming convention — find your machine's hostname

To prevent name collision across machines (two `wiki` agents on different
devices would duplicate-process mail and be ambiguous on send), every
`CLAUDE_MAILBOX_NAME` should embed the machine's hostname.

**Format**: `<role>@<hostname>`

**Why hostname**: OS-provided unique-per-machine ID, stable across reboots,
human-readable. Beats MAC (ugly, NIC-swap-fragile), MachineGuid (unreadable
UUID), or made-up names like "laptop" (will collide when you get a second
laptop).

**Find hostname**:

| Platform | Command | Example output |
|---|---|---|
| Windows PowerShell | `$env:COMPUTERNAME` | `DESKTOP-ABC123` |
| Windows cmd        | `hostname`          | `DESKTOP-ABC123` |
| Linux / macOS      | `hostname`          | `thinkpad-x1`    |
| Python (cross)     | `python -c "import socket; print(socket.gethostname())"` | `LAPTOP-XYZ789` |

So names on different machines look like:

| Machine | Agents |
|---|---|
| Desktop (`DESKTOP-ABC123`) | `wiki@DESKTOP-ABC123` / `koatag@DESKTOP-ABC123` / `koatag-frontend@DESKTOP-ABC123` |
| Laptop (`LAPTOP-XYZ789`)   | `wiki@LAPTOP-XYZ789` / `koatag@LAPTOP-XYZ789` |
| Future tablet              | `wiki@TAB-ZEN` |
| Tailscale VPS              | `wiki@vps-tokyo` |

Sending mail: `mcp__mailbox__send(to="wiki@LAPTOP-XYZ789", body="...")` — unambiguous which machine's wiki.

If you're sure a role only ever runs on one machine (e.g., `koatag-bridge`
container only on hub), you *may* drop `@hostname` for that single role. But
mixed convention gets confusing — recommended to be consistent.

### 1.7 Configure Claude Code MCP

> **Before writing the token**: confirm `.mcp.json` is in `.gitignore` for this
> project (or the project itself is private). Token is a long-lived secret.
> Default `life_wiki` / `KOATAG` projects already gitignore `.mcp.json`.
> If unsure, run `git check-ignore -v .mcp.json` in the project; output means
> ignored, no output means tracked — fix `.gitignore` first.

In any project on the laptop, create or edit `.mcp.json`:

```json
{
  "mcpServers": {
    "mailbox": {
      "command": "python",
      "args": ["C:/path/to/claude-mailbox/server.py"],
      "env": {
        "CLAUDE_MAILBOX_NAME": "wiki@LAPTOP-XYZ789",
        "CLAUDE_MAILBOX_REMOTE": "http://<HUB_IP>:1905",
        "CLAUDE_MAILBOX_TOKEN": "<TOKEN>"
      }
    }
  }
}
```

Replace `LAPTOP-XYZ789` with your actual hostname from Phase 1.6.

> ⚠️ **Restart Claude Code session after saving `.mcp.json`** — MCP env is
> read on session boot, not hot-reloaded. Skip this step → `whoami()` returns
> `mode: local` instead of `remote` and you'll start a local ghost DB.

Because `CLAUDE_MAILBOX_REMOTE` is set, `server.py` will route every MCP tool
(`send`, `inbox`, `mark_read`, `peers`, `whoami`) through REST. It will not
create a local SQLite file. Verify by:

```python
# In Claude Code, call:
mcp__mailbox__whoami()
# Expected: {"name": "wiki@LAPTOP-XYZ789", "mode": "remote", "hub": "http://<HUB_IP>:1905"}
```

### 1.8 Start the watcher

> **Critical**: `.mcp.json` env block injects ONLY into the MCP server subprocess
> (`server.py`). The watcher is a **separate** subprocess spawned by Monitor /
> Bash and does **not** inherit `.mcp.json` env. Pass `--remote` + `--token`
> on the watcher command line, do not rely on env propagation.
>
> Symptom of getting this wrong: watcher stderr spams `unable to open
> database file` (it tried local-mode), `stdout` is empty, Monitor never fires
> notifications. Round-trip ping mail gets queued in hub DB but spoke never
> wakes up.

Use the `Monitor` tool (preferred — stream-mode, never dies):

```yaml
Monitor:
  command: py "C:/path/to/claude-mailbox/mailbox-watch.py" wiki@LAPTOP-XYZ789 --remote http://<HUB_IP>:1905 --token <TOKEN>
  description: mailbox watcher for wiki@LAPTOP-XYZ789 (remote)
  persistent: true
  timeout_ms: 3600000
```

Expected first stderr lines:
```
[watcher] remote-mode connect: http://<HUB_IP>:1905  name=wiki@LAPTOP-XYZ789
[watcher] connected, streaming events
```

If your shell wrapper sets `CLAUDE_MAILBOX_REMOTE` / `CLAUDE_MAILBOX_TOKEN` in
the *parent* env before launching Monitor (e.g. a systemd unit, a wrapper
`.ps1`), the watcher will pick them up — but inside Claude Code's Monitor tool
the cleanest path is **always pass flags explicitly**.

Expected first-line stderr:
```
[watcher] remote-mode connect: http://<HUB_IP>:1905  name=wiki@LAPTOP-XYZ789
[watcher] connected, streaming events
```

### 1.9 Smoke test the round trip

From the hub, send a mail (use the laptop's full `<role>@<hostname>`):
```python
mcp__mailbox__send(to="wiki@LAPTOP-XYZ789", body="hello from hub")
```

The laptop watcher should immediately emit one stdout line:
```
MAIL id=<N> from=<hub-name> sent=<ts> preview=hello from hub
```

…which Claude Code's Monitor tool turns into an in-conversation notification.

From the laptop, send back (use the hub's full name):
```python
mcp__mailbox__send(to="wiki@DESKTOP-ABC123", body="hello from laptop")
```

Hub-side wiki watcher sees it. Names always include `@hostname` to avoid
ambiguity.

> **Hub mixed naming** (bare `wiki` + `wiki@DESKTOP-...` coexist transient):
> if the hub hasn't migrated yet (see "Transitioning the hub" above), spoke's
> safer choice is the **bare name** (`wiki`) — it matches what hub's MCP tools
> stamp as `from_name` and what its watcher subscribes to. Once hub fully
> migrates, switch to `@hostname` for both sides.

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

### Transitioning the hub to `@hostname` convention (optional)

If your hub agents currently use bare names (`wiki`, `koatag`) — fine, they
still work. But to make cross-device unambiguous, you may want to migrate:

1. On each hub project's `.mcp.json`, change `CLAUDE_MAILBOX_NAME` from `wiki`
   to `wiki@DESKTOP-ABC123` (your hub's hostname).
2. Restart Claude Code sessions.
3. Restart the hub's local watcher with the new name.
4. The `peers` table will accumulate both old (`wiki`) and new (`wiki@DESKTOP-ABC123`)
   rows — cosmetic, can be cleaned: `DELETE FROM peers WHERE name NOT LIKE '%@%'`.
5. Historical message rows still reference old names — harmless, history doesn't auto-update.

Or skip the migration: hub keeps bare names, spokes use `@hostname`, and you
just remember "no `@` = hub". Either works.

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
| `curl /health` connection refused | server not running | Phase 0.4 |
| `curl /health` timeout (ping VPN OK but TCP timeout) | firewall blocking the VPN range | add VPN range to firewall rule (Phase 0.5 — Radmin `26/8`, Tailscale `100.64/10`, etc) |
| `curl /peers` returns 401 | wrong token | re-copy token.txt; remove leading/trailing whitespace |
| `whoami` returns `mode: local` not `remote` | env var not propagated to MCP server | check `.mcp.json` env block; restart Claude Code session |
| ghost `mailbox.db` appears on laptop | `.mcp.json` missing `CLAUDE_MAILBOX_REMOTE` env | re-check spelling; ensure both REMOTE + TOKEN are set |
| Watcher exits immediately with 401 | token typo | check `--token` on watcher command line (don't rely on env from `.mcp.json` — see Phase 1.8) |
| Watcher stderr spams `unable to open database file` | watcher launched without `--remote`, fell back to local SQLite mode | pass `--remote http://<HUB_IP>:1905 --token <TOKEN>` on the Monitor command line; `.mcp.json` env does NOT propagate to watcher subprocess |
| Watcher reconnects every 2 sec | hub serving but auth fails or path 404 | check `--remote` URL has no trailing slash; verify server log shows the connect |
| Mail sent but spoke watcher silent | wrong `CLAUDE_MAILBOX_NAME` mismatch | watcher's name must exactly match recipient name on hub's send (case-sensitive, including `@hostname`) |
| Two watchers wake on every mail | same `CLAUDE_MAILBOX_NAME` on two machines | adopt `<role>@<hostname>` convention (Phase 1.6) so each machine has unique name |
| Laptop has pre-existing `~/.claude-mailbox.db` or `~/.claude/mailbox/mailbox.db` | legacy from old single-machine setup | leave alone; cross-device skips local DB entirely when REMOTE env set (see Phase 1.1). Do NOT merge into hub DB |
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
