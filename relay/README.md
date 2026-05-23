# mailbox-relay — phone last-mile streaming proxy + list/delete UI

Spoke-side aiohttp app that lets a phone on the spoke's WiFi hotspot
**browse, download, and delete** mailbox attachments — without any file
ever landing on spoke's disk.

```
Phone (on spoke hotspot) ─HTTP─> Spoke aiohttp :1906 ─HTTP─> Hub :1905
                                  /list (HTML)               /attachments
                                  /file/<id> (stream)        /attachment/<id> GET
                                  /delete (form POST)        /attachment/<id> DELETE
```

## What lives where

| | Hub | Spoke (this relay) | Phone |
|---|---|---|---|
| Canonical file blobs (`/data/attachments/<sha[:2]>/<sha>`) | ✅ | ❌ | ❌ |
| Mailbox DB (SQLite) | ✅ | ❌ | ❌ |
| `mailbox-server.py` REST API | ✅ | ❌ | ❌ |
| nginx / aiohttp relay | ❌ | ✅ | ❌ |
| Bearer token to hub | (validates) | ✅ (proxies) | ❌ |
| Phone-side `?token=` gate | ❌ | ✅ (validates) | ✅ (provides) |
| Browser | ❌ | (optional, can also use) | ✅ |

Phone never knows the hub's Bearer token. Spoke never stores file bytes
on disk — streaming proxy passes bytes through aiohttp memory only.

## Setup (on a spoke machine)

Pre-req:
- Spoke can reach hub on its VPN IP (Tailscale / Radmin / LAN).
- You know hub's `CLAUDE_MAILBOX_TOKEN`.

```bash
cd relay
cp .env.example .env
# Edit .env — set RELAY_TOKEN (random), HUB_VPN_IP, HUB_TOKEN,
# MAILBOX_RECIPIENT (whose attachments to list; default "wiki")
docker compose up -d
docker compose ps      # mailbox-relay (healthy) after ~10-30s
```

## Use (from the phone)

1. Phone connects to spoke's hotspot WiFi (or phone *is* the hotspot
   and spoke joined it — either way, both on same subnet).
2. On spoke run `ipconfig` (PowerShell/cmd) to find spoke's IPv4 on
   that WiFi interface.
3. On phone, open browser:

   ```
   http://<spoke-ip>:1906/list?token=<RELAY_TOKEN>
   ```

4. You'll see a list of mailbox attachments for `MAILBOX_RECIPIENT`.
   - Tap a filename → download
   - Tick checkboxes + tap **🗑️ 刪除選取** → confirm → entries removed
     (DB row + blob if no other reference)

## Endpoints (phone-facing)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | — | 404 (no fingerprint leak) |
| GET | `/health` | — | 200 `ok` — docker healthcheck |
| GET | `/list?token=...` | `?token` | HTML page of attachments for `MAILBOX_RECIPIENT` |
| GET | `/file/<id>?token=...` | `?token` | streaming proxy of hub `/attachment/<id>` |
| POST | `/delete?token=...` | `?token` | form: `ids=N&ids=M`; calls hub DELETE for each, redirects to `/list` |

## Auth model

Two tokens, two purposes:

| Token | Where it lives | Seen by | Purpose |
|---|---|---|---|
| `HUB_TOKEN` | `.env`; injected as `Authorization: Bearer` on outbound calls | spoke ↔ hub only | hub mailbox-server REST auth |
| `RELAY_TOKEN` | `.env`; expected as `?token=` from phone | phone ↔ spoke only | keep random hotspot guests out |

Spoke terminates the phone connection and opens a fresh upstream
connection to hub with the hub token. Phone never knows `HUB_TOKEN`.

## Trust model

- **Hub disk** — canonical blobs live here (content-addressed, dedup).
- **Spoke disk** — file NEVER lives here. aiohttp streams chunked (64 KB)
  upstream → response, no disk buffering.
- **Spoke RAM** — chunks pass through Python's socket buffers in transit
  only. Forensic recovery would require root + live access during the
  transfer; afterward RAM pages are typically reused within seconds.
- **TLS** — not enabled by default. Hotspot LAN is private; add a
  self-signed cert + phone trust prompt if you want defense against an
  attacker who joined the hotspot.

## Delete semantics

POST `/delete` with form field `ids` (one or more) →
each id calls hub `DELETE /attachment/<id>` → hub:

1. Looks up the attachment row by id.
2. Counts other rows referencing the same `sha256`.
3. Removes the row.
4. Unlinks the blob **only when no other row referenced it** (dedup-aware
   refcount).
5. Audit-logs `delete_attachment` with `{filename, size, sha256,
   other_refs, blob_deleted}` for both successful and failed unlink paths.

Sender's original file (the path that was passed to `send(files=[...])`)
is **not touched** — mailbox stores a copy of bytes at ingest, no path
reference back.

## Healthcheck

`GET /health` → 200 `ok`. Docker healthcheck uses
`urllib http://127.0.0.1/health` from inside the container (literal IPv4
loopback — `localhost` would resolve to `::1` and miss the IPv4 listener).

## Why aiohttp instead of nginx (v0 → v1)

v0 was pure nginx (streaming proxy only). v1 needs:
- HTML rendering (`/list`)
- Form parsing (`/delete`)
- Querying hub for attachment metadata
- Calling hub DELETE per selected id

Pure nginx can't render dynamic HTML or call upstream DELETE. Options
were nginx + Lua, or nginx + sidecar Python, or replace with aiohttp.
Single aiohttp app turned out simplest — same Python container handles
streaming proxy too.

## Future improvements (still not in v1)

- Per-attachment one-time signed URLs (HMAC + exp) — avoid `?token=` in
  phone history / referer leaks
- TLS on hotspot side (self-signed)
- mDNS advertisement so phones discover the relay without IP entry
- Bridge integration: wiki sends file → bridge automatically DMs user
  Discord with full URL
- Per-attach authorization model (today `RELAY_TOKEN` is a master key
  granting access to all of `MAILBOX_RECIPIENT`'s attachments)
