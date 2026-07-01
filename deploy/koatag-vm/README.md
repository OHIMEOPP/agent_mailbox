# Hub on koatag VM — migration + spoke repoint (2026-07-01)

**The mailbox hub moved off the desktop onto the always-on koatag GCP VM.**

| | Old hub | New hub |
|---|---|---|
| Machine | desktop (`DESKTOP-JVLHTJ4`) | koatag VM (`ohimeopp-api`, us-central1-a) |
| Reach | Tailscale `100.91.88.79` | Tailscale `100.65.180.114` |
| Status | **offline** (HTTP 000) | 24/7, `restart: always` |
| Endpoint | `http://100.91.88.79:1905` | **`http://100.65.180.114:1905`** |

Why: the desktop hub was a SPOF — when it slept/shut down the whole cross-device
mesh went dark. The VM (e2-micro, uptime measured in weeks) is a proper always-on
rendezvous. Reached over **Tailscale only** — no public exposure, no GCP firewall
rule opened (published docker ports bind to the `tailscale0` IP `100.65.180.114`).

The **token is unchanged** — every spoke only swaps the IP.

## What every spoke must do

Change `CLAUDE_MAILBOX_REMOTE` from `http://100.91.88.79:1905` to
`http://100.65.180.114:1905`. Token stays the same.

- **Global env** (`~/.claude/settings.json`): `CLAUDE_MAILBOX_REMOTE` → new IP.
- **Per-project** `.mcp.json` that pins REMOTE (e.g. `supporters`): update it too.
- **Watcher** launch command: `--remote http://100.65.180.114:1905` (env does NOT
  propagate to the watcher subprocess — pass the flag explicitly).

### Desktop agents: local → spoke

The desktop's agents (`wiki` / `koatag` / `koatag-frontend` / `stranger-conv`)
used to run in **local mode** (direct SQLite, they WERE the hub). With the hub
now remote they must become **spokes**:

1. Add to each project's mailbox env: `CLAUDE_MAILBOX_REMOTE=http://100.65.180.114:1905`
   + `CLAUDE_MAILBOX_TOKEN=<same token>`.
2. Restart the Claude Code session (MCP env is read at boot).
3. Restart each watcher with `--remote http://100.65.180.114:1905 --token <token>`.
4. Stop the old desktop hub services (`mailbox-server` + any hub-local watcher) so
   nothing keeps writing to the now-orphan desktop SQLite.

> `wiki` keeps the **bare name** `wiki` — the Discord bridge's default DM target is
> hardcoded to `wiki` (`bridge/inbound.py`), so keeping the desktop wiki alive as a
> spoke preserves DM routing with zero code change. If the desktop is retired,
> retarget the default (small change in `bridge/inbound.py`).

## VM deployment layout

```
~/mailbox/
├── docker-compose.yml   # == deploy/koatag-vm/docker-compose.yml in this repo
├── .env                 # CLAUDE_MAILBOX_TOKEN=... (mode 600, gitignored, NOT here)
├── repo/                # scp'd source: mailbox-server.py + mailbox/ + bridge/
└── data/                # fresh SQLite mailbox.db + attachments/ + backups/
```

Source is **scp'd**, not a git checkout, to avoid cloning the private repo onto the
VM. Re-sync after a code change:

```bash
scp -i ~/.ssh/my_google_vm_key mailbox-server.py user@100.65.180.114:mailbox/repo/
scp -i ~/.ssh/my_google_vm_key -r mailbox bridge user@100.65.180.114:mailbox/repo/
ssh -i ~/.ssh/my_google_vm_key user@100.65.180.114 'cd ~/mailbox && docker compose up -d --force-recreate mailbox-server'
```

## Phase status

- **Phase 1 — core (`mailbox-server:1905`)**: ✅ live on the VM, agent↔agent verified
  (round-trip wiki@LAPTOP ↔ supporters over the VM hub, 2026-07-01).
- **Phase 2 — Discord bridge (`mailbox-bridge:1904`)**: defined under the `bridge`
  docker-compose profile, **not yet started**. Needs `DISCORD_BOT_TOKEN` in `.env`.
  Shares the bot token with the VM's node-red (VTuber) + digests — REST is stateless
  so that's fine; only avoid two gateway websockets misbehaving (empirically the
  desktop-bridge + VM-node-red pair already coexisted).

## Fresh DB

The VM started a **new** `mailbox.db` — the desktop's history was not migrated.
Mailbox is a transient queue (read 7d / unread 14d auto-swept), so nothing of value
was lost; peers re-register on their next watcher heartbeat.
