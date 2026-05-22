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
# Since 2026-05-23: returns JSON {ok:true, unread_count, blob_count, blob_total_bytes,
#   oldest_message_age_days, peer_count, last_sweep_at, ...}
# Old text "ok" deprecated — substring grep for "ok" still matches.
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
# Expected: JSON containing "ok":true plus observability fields

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
# Expected: JSON with "ok":true

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

## Phase 4 — File / zip transfer (since 2026-05-23)

Once cross-device mailbox is up, you can also push files between hub and spoke through the same `:1905` server. No extra container or port.

### Endpoints

```
POST /send-file (multipart/form-data)
  payload_json:  JSON {from, to, body}
  files[0..N]:   one form-data part per file (any field name starting with "files")
  → 200 {id, sent_at, attachments: [{id, filename, mime, size, sha256}]}

GET /attachment/<id>
  → 200 binary blob
  Content-Disposition: attachment; filename="<ascii>"; filename*=UTF-8''<percent-encoded>
  X-Mailbox-Sha256: <hex>
```

### Limits

| | Default |
|---|---|
| Per-file | 100 MB |
| Total payload | 500 MB |
| Files per message | 32 |

Set higher? Edit `MAX_SINGLE_FILE` / `MAX_TOTAL_PAYLOAD` / `MAX_FILES_PER_MSG` constants at the top of `mailbox-server.py` and restart the container.

### Blob storage

Content-addressed under `<db-parent>/attachments/<sha256[:2]>/<sha256>` (so in default deploy: `C:/Users/User/.claude/mailbox/attachments/...`). Same SHA across messages = same blob on disk (automatic dedup). Hub-only — spoke never stores blobs locally.

### MCP tool surface

`send(to, body, files=[...])` — same tool, optional `files` list of host filesystem paths.
`download(attachment_id, save_to)` — explicit fetch; spoke watcher never auto-downloads.

```python
# Hub agent:
mcp__mailbox__send(to="wiki@LAPTOP-XYZ", body="snapshot zip",
                   files=["C:/tmp/snapshot.zip"])

# Spoke agent later:
inbox = mcp__mailbox__inbox()
# → [{id, from, body, attachments: [{id, filename, size, sha256}]}]
mcp__mailbox__download(attachment_id=N, save_to="C:/tmp/snapshot.zip")
# → verifies sha256 against server-reported hash before saving
```

### CLI tool (no MCP)

`mailbox-attach.py` — shell equivalent of `send(files=[...])`. Posts multipart to hub `/send-file`.

```powershell
py mailbox-attach.py --from wiki@DESKTOP-ABC --to wiki@LAPTOP-XYZ `
    --body "config snapshot" --files C:/cfg/foo.json C:/cfg/bar.toml `
    --hub http://192.168.1.10:1905 --token <bearer>
```

`--hub` / `--token` fall back to `CLAUDE_MAILBOX_REMOTE` / `CLAUDE_MAILBOX_TOKEN` env vars.

> Don't confuse with `mailbox-discord-file.py` — that one pushes to Discord DM via the `:1904` bridge. `mailbox-attach.py` is peer ↔ peer mailbox over the `:1905` cross-device server.

### Watcher behavior

Stream-mode watcher emits `attach=N` on the MAIL stdout line when the message has attachments:

```
MAIL id=42 from=wiki@DESKTOP-ABC sent=2026-05-23T... attach=1 preview=snapshot zip
```

The watcher does NOT auto-download. Agent calls `download()` explicitly. Reason: an idle inbox shouldn't be able to fill the spoke's disk.

### What about folders?

The protocol takes individual files, not directories. For folder transfer, zip first:

```powershell
Compress-Archive -Path C:/wiki -DestinationPath C:/tmp/wiki.zip
py mailbox-attach.py --from wiki@hub --to wiki@laptop --body "wiki snapshot" --files C:/tmp/wiki.zip
```

This is intentional — mailbox is a message queue with attachments, not a sync engine. For ongoing folder sync use Syncthing / Tailscale Drive instead.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 413 on `/send-file` | exceeded MAX_SINGLE_FILE (100 MB) or MAX_TOTAL_PAYLOAD (500 MB) | split into multiple messages, or bump constants + restart server |
| 400 "multipart parse failed" | client sent malformed multipart (boundary mismatch, missing `\r\n`) | use `mailbox-attach.py` or MCP `send(files=...)` — both use proven encoders |
| Spoke watcher fires MAIL but no `attach=N` | spoke is on old `mailbox-watch.py` (pre-2026-05-23) | `git pull` mailbox repo on spoke, restart Monitor; SSE payload is backwards-compatible so old watcher still works, just doesn't print the tag |
| `download()` returns sha256 mismatch error | network corruption mid-transfer (rare on LAN, possible over Tailscale on lossy link) | retry — server verifies file on disk has the original hash before serving |
| Blob file missing on `/attachment/<id>` (500) | someone deleted from `<dir>/attachments/` manually | rare; investigate before re-sending. Server logs the path |

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

## Phase 5 — Retention (since 2026-05-23)

Mailbox is a transient message queue — nothing should accumulate indefinitely. Hub runs a background daily sweep that deletes old messages, frees orphan blobs, and drops stale peer rows.

### Defaults

| Item | TTL | Rationale |
|---|---|---|
| Read messages | 7 days | already processed, no value |
| Unread messages | 14 days | likely stale dead-letter |
| Peer rows | 30 days | no heartbeat = long-gone machine |
| Attachment rows | tied to message | cascade when message goes |
| Blobs on disk | tied to last referencing attachment | content-addressed; reused blobs survive |

### Sweep daemon (hub-side, automatic)

The container's `mailbox-server.py` spawns a daemon thread at boot that:
1. Waits **1 hour grace** (avoid noise on boot)
2. Sweeps, logs `[sweep] deleted N read / N unread / N attach rows / N blobs (XMB freed) / N peers`
3. Sleeps **24 hours**
4. Repeats

State is in-memory only — `LAST_SWEEP_AT` and counters surface via `/health`.

### Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_RETENTION_READ_DAYS` | 7 | read messages older than this → delete |
| `MAILBOX_RETENTION_UNREAD_DAYS` | 14 | unread messages older than this → delete |
| `MAILBOX_RETENTION_PEER_DAYS` | 30 | peer heartbeat older than this → drop row |
| `MAILBOX_RETENTION_DISABLED` | (unset) | set to `1` to skip auto-sweep (CLI still works) |

Wired into `bridge/docker-compose.yml` — override in `bridge/.env`:

```env
MAILBOX_RETENTION_READ_DAYS=3
MAILBOX_RETENTION_UNREAD_DAYS=7
```

Then `docker compose up -d --force-recreate mailbox-server`.

### Manual CLI (`mailbox-retention.py`)

Lives in the repo root. Operates directly on the SQLite DB (server doesn't need to be running):

```powershell
py mailbox-retention.py --stats
# db / attachments dir / counts / oldest message age

py mailbox-retention.py --dry-run
# [sweep] DRY-RUN: would have deleted N read / N unread / ... — no writes

py mailbox-retention.py --once
# [sweep] deleted N read / N unread / ... — does the sweep

py mailbox-retention.py --once --read-days 3 --unread-days 7
# override retention windows for this run only

py mailbox-retention.py --stats --json
# machine-readable output for piping into other tools
```

The CLI uses `mailbox_sweep` (importable module) — same code path as the daemon, so output matches.

### /health observability

```bash
curl http://<HUB_IP>:1905/health
# {
#   "ok": true,
#   "unread_count": 3,
#   "message_count": 47,
#   "attachment_count": 5,
#   "blob_count": 4,
#   "blob_total_bytes": 13_421_770,
#   "oldest_message_age_days": 5.2,
#   "peer_count": 4,
#   "last_sweep_at": "2026-05-22T22:00:14Z",
#   "last_sweep_counters": { ... }
# }
```

`last_sweep_at` is `null` until first sweep completes (1hr after server boot). Use this for monitoring — if it falls > 25hr stale, sweep daemon died.

### What's NOT swept

- **Pinned messages**: no such feature. If you need to keep something forever, dump to wiki via `mailbox-dump.py` before TTL.
- **WAL files**: server uses `journal_mode=DELETE` — nothing accumulates there.
- **Pip cache** (bridge container at `/data/.pip-cache`): tiny, ignored. Manually `rm -rf` if you really want.

---

## Phase 6 — Backup (since 2026-05-23)

Retention sweep deletes; backup snapshots. The pair gives you: stable disk footprint **and** recoverability if a sweep or external corruption removes something you wanted back.

### Defaults

| Item | Method | Rationale |
|---|---|---|
| `mailbox.db` | SQLite online `.backup()` API | atomic, doesn't block live writers, captures consistent view |
| `attachments/` | tar.gz of the entire directory | content-addressed blobs already dedup; tar preserves layout |
| Rolling retention | 7 daily / 4 weekly / 3 monthly | keeps ~1 month deep history without unbounded growth |
| Default location | `<db parent>/backups/` (= `~/.claude/mailbox/backups/` on hub) | siblings the DB it's backing up |
| Naming | `mailbox-backup-YYYYMMDD-HHMMSS.db` + `-attachments.tar.gz` | UTC timestamp; pair shares the timestamp |

### Backup daemon (hub-side, automatic)

Same thread as the sweep daemon (Phase 5) — order is **backup first, then sweep**, so the most recent backup always reflects pre-sweep state. If a sweep ever turns out to be wrong, restoring the latest backup gets you back the data sweep just deleted.

1. Wait **1 hour grace** (shared with sweep)
2. Backup, logs `[backup] backed up db=XMB + attachments=YMB, pruned N old (ZMB freed)`
3. Sweep, logs `[sweep] ...`
4. Sleep **24 hours**
5. Repeat

State is in-memory only — `LAST_BACKUP_AT` and counters surface via `/health`.

### Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_BACKUP_DIR` | `<db parent>/backups` | override output directory (e.g. external drive) |
| `MAILBOX_BACKUP_DISABLED` | (unset) | set to `1` to skip auto-backup (sweep still runs; CLI still works) |
| `MAILBOX_BACKUP_KEEP_DAILY` | 7 | rolling — newest-per-day buckets to keep |
| `MAILBOX_BACKUP_KEEP_WEEKLY` | 4 | rolling — newest-per-ISO-week buckets to keep (additive) |
| `MAILBOX_BACKUP_KEEP_MONTHLY` | 3 | rolling — newest-per-month buckets to keep (additive) |

Wired into `bridge/docker-compose.yml` — override in `bridge/.env`:

```env
MAILBOX_BACKUP_DIR=/data/backups          # mount this as a docker volume
MAILBOX_BACKUP_KEEP_DAILY=14
```

Then `docker compose up -d --force-recreate mailbox-server`.

### Manual CLI (`mailbox-backup.py`)

Lives in the repo root. Operates directly on the SQLite DB + attachments dir (server doesn't need to be running — the online backup API is concurrent-safe):

```powershell
py mailbox-backup.py --stats
# last_backup_at / backup_count / total bytes

py mailbox-backup.py --list
# all snapshots, newest first, with db / tar / total sizes

py mailbox-backup.py --once
# take one backup + rolling prune now

py mailbox-backup.py --restore 20260523-020000
# DRY-RUN — prints what it would do, exits 2

py mailbox-backup.py --restore 20260523-020000 --yes
# actually overwrites live data (after moving current state to .before-restore-<now>)

py mailbox-backup.py --list --json
# machine-readable; same shape for --stats / --once / --restore with --json
```

The CLI uses `mailbox_backup` (importable module) — same code path as the daemon, so output matches.

### Restore semantics

`mailbox-backup.py --restore <timestamp> --yes`:

1. Moves current `mailbox.db` → `mailbox.db.before-restore-<now>`
2. Moves current `attachments/` → `attachments.before-restore-<now>`
3. Copies `mailbox-backup-<timestamp>.db` into place
4. Extracts `mailbox-backup-<timestamp>-attachments.tar.gz` into place (if it exists)

If the restore turns out wrong, rollback is: rename `.before-restore-*` back to original. No state is destroyed by `--restore`.

`<timestamp>` can be:
- the bare `YYYYMMDD-HHMMSS` (from `--list`)
- the full filename (`mailbox-backup-20260523-020000.db`) — CLI extracts the ts portion

### /health observability

```bash
curl http://<HUB_IP>:1905/health
# {
#   ...
#   "last_backup_at": "2026-05-22T22:00:11Z",
#   "backup_count": 11,
#   "backup_total_bytes": 14_280_192,
#   "last_backup_counters": {
#     "db_backup_path": "...",
#     "db_backup_bytes": 524288,
#     "attachments_tar_path": "...",
#     "attachments_tar_bytes": 12345678,
#     "backups_pruned": 0,
#     "bytes_freed_pruning": 0
#   }
# }
```

`last_backup_at` is `null` until the first backup runs (1hr after server boot). Monitor for staleness > 25hr — that's a dead backup daemon.

### Verify docker env-var disables auto-backup

Manual check (smoke test #5 leaves this as a comment — would require spinning the container):

```bash
# In bridge/.env:
MAILBOX_BACKUP_DISABLED=1

docker compose up -d --force-recreate mailbox-server
docker logs mailbox-server | grep backup
# Expected: "[mailbox-server] backup: DISABLED via MAILBOX_BACKUP_DISABLED"
# AND no "[backup] backed up ..." lines appear after the 1hr grace period
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Backup dir not created on disk | hub didn't reach the 1hr grace yet, or `MAILBOX_BACKUP_DISABLED=1` | wait 1hr after server boot, or check env; run `py mailbox-backup.py --once` to create manually |
| `--restore` succeeds but `attach=N` rows still show 0 in `/health` | the snapshot itself had no attachments at backup time | verify with `py mailbox-backup.py --list` — `attachments` column == 0 means tarball wasn't generated |
| `[backup] FAILED: OperationalError: database is locked` | another writer holds an exclusive lock (rare; SQLite online backup is supposed to handle this) | retry; if persistent, check for stuck `mailbox-discord-bridge` process or `.db-journal` leftover |
| Restore failed with `db backup not found` | timestamp typo or backup already pruned | `py mailbox-backup.py --list` to see available timestamps |
| `/health` shows `last_backup_at` but pruning never happens | only 1-2 backups exist; nothing to prune yet | retention triggers only when more than `KEEP_DAILY+KEEP_WEEKLY+KEEP_MONTHLY` snapshots exist with the right time spread |
| Backup dir growing past expected size | `KEEP_*` env vars set too high, or db growing on its own | check `py mailbox-backup.py --stats`; lower `MAILBOX_BACKUP_KEEP_DAILY` if needed |

### What's NOT backed up

- **Memory state**: `LAST_SWEEP_AT`, `LAST_BACKUP_AT`, etc — these are stats only; rebuilt on next daemon tick.
- **Token file** (`~/.claude/mailbox/token.txt`): backup it yourself if you care; not auto-managed.
- **Config files** (`bridge/.env`, `docker-compose.yml`): tracked in git, not in the runtime backup.

---

## Phase 7 — Audit log (since 2026-05-23)

Passive trail of every mailbox operation, written into `audit_log` alongside `messages`/`peers`/`attachments`. Powers forensics, "who downloaded this", "why is the send rate spiking", and post-mortem of any DB anomaly.

### Schema

```sql
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor        TEXT NOT NULL,        -- who did this
    action       TEXT NOT NULL,        -- one of send/inbox/mark_read/download/whoami/peers
    target       TEXT,                 -- recipient name / msg id / attachment id
    payload_json TEXT,                 -- JSON blob, shape per action
    ok           INTEGER NOT NULL DEFAULT 1
);
-- Plus three indexes: ts, (actor, ts), (action, ts)
```

DDL is idempotent (`CREATE TABLE IF NOT EXISTS`) and **not** in the same executescript as the messages-table migrations — `mailbox_audit.init_schema()` owns its table shape, called once at startup by both `server.py` and `mailbox-server.py`.

### Write points

| Where | When | Actor convention |
|---|---|---|
| `server.py` (MCP local mode) | each tool call after success | `NAME` env (e.g. `wiki`) |
| `server.py` (MCP remote mode) | (not logged on spoke; hub records via REST endpoint) | n/a |
| `mailbox-server.py` `/send` `/send-file` | after successful insert | payload `from` |
| `mailbox-server.py` `/inbox` | after select | query-string `name` |
| `mailbox-server.py` `/mark_read` | after update | `rest:<client-ip>` |
| `mailbox-server.py` `/peers` | after select | `rest:<client-ip>` |
| `mailbox-server.py` `/attachment/<id>` | success **and** failure (ok=0) | `rest:<client-ip>` |

`log_event()` swallows all exceptions internally — audit must never break the audited operation. Failures go to stderr only.

### Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_AUDIT_DISABLED` | (unset) | set to `1` to skip all `log_event()` writes (reads still work) |

Use with care — without audit you lose forensic capability. Only flip when you have a documented perf problem.

### REST endpoint

```
GET /audit?since=<ts>&until=<ts>&actor=<name>&action=<name>&limit=N&asc=0
Authorization: Bearer <token>
→ {rows: [{id, ts, actor, action, target, payload, ok}, ...], count: N}
```

`limit` hard-capped at 500 to prevent giant dumps. Paginate older history with `since=<last-ts-from-previous-page>`. `asc=1` to reverse order.

### Manual CLI (`mailbox-audit.py`)

Hub-only. Operates directly on the SQLite DB:

```powershell
py mailbox-audit.py --tail
# default — last 50 rows, newest first

py mailbox-audit.py --tail --limit 200 --since 24h

py mailbox-audit.py --tail --actor wiki --action send --json

py mailbox-audit.py --stats
# audit_count + first_at + last_at + by_action breakdown
```

`--since` accepts ISO timestamps **or** relative (`15m` / `1h` / `24h` / `7d`).

### /health observability

```bash
curl http://<HUB_IP>:1905/health
# {
#   ...
#   "audit_count": 1247,
#   "audit_last_at": "2026-05-22T22:14:33.881Z"
# }
```

Use `audit_last_at` to spot stuck servers — if it falls far behind wall-clock during traffic, a connection / write path is broken.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `--tail` returns `(no audit rows matching filters)` after server traffic | `MAILBOX_AUDIT_DISABLED=1` set in server env, or you're querying a DB the server doesn't write to | check `docker compose config` for the env var; verify `--db` path matches the server's runtime db |
| `[audit] log_event failed: ...` in server stderr | the audit module caught a DB error (lock / disk full / corrupt) and swallowed it | watch for repeat lines — single occurrence is fine, persistent failure means the underlying DB is unhealthy |
| `audit_count` grows but disk barely moves | each row is ~150 bytes — even 1M rows is ~150 MB | not a problem; if it is, set `MAILBOX_AUDIT_DISABLED=1` and add to retention sweep |
| `--action foo` rejected with rc=2 | `foo` not in the canonical action set | valid actions: `send / inbox / mark_read / download / whoami / peers` |
| `/audit` returns 401 | missing `Authorization: Bearer <token>` header | same auth as all other REST endpoints (Phase 0) |
| Spoke `mcp__mailbox__whoami()` doesn't show up in audit | spoke is in remote-mode and doesn't write local audit — the call hits the hub but `whoami` is not a REST endpoint | by design; whoami is identity introspection, not a state mutation, and the hub never sees it |

### What's NOT audited

- `/health` — public endpoint, polled by monitors; would drown the log
- SSE `/watch` connections — these are long-poll fan-out; would log every poll tick
- `mailbox_sweep` / `mailbox_backup` daemon actions — separate `/health` metrics already track these (`last_sweep_at`, `last_backup_at`)

### Retention of the audit log itself

Currently the audit table grows unbounded. When this becomes a problem, extend `mailbox_sweep.sweep_all()` to prune `audit_log` rows older than N days (env: `MAILBOX_AUDIT_RETENTION_DAYS` once implemented). For now: manually `DELETE FROM audit_log WHERE ts < ?` if needed.

---

## What is NOT installed on the spoke

You do **not** need on the laptop:
- `mailbox-server.py` (hub-only — only the writer runs this)
- `mailbox-bridge` (Discord container — hub-only)
- `mailbox-followup.py` (admin tool — hub-only)
- `mailbox-whitelist.py` (admin tool — hub-only)
- `mailbox-retention.py` (hub-only — operates on hub's DB; spoke has no local DB)
- `mailbox-backup.py` (hub-only — same reason as retention)
- `mailbox-audit.py` (hub-only — spoke has no local audit table)

Just `server.py` (MCP, with REMOTE env) + `mailbox-watch.py` (SSE client).
That's it.
