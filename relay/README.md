# mailbox-relay — phone last-mile streaming proxy

Spoke-side nginx that lets a phone on the spoke's hotspot pull mailbox
attachments from hub, without the file ever landing on spoke's disk.

```
Phone (on spoke hotspot) ─HTTP─> Spoke nginx :1906 ─proxy_pass─> Hub :1905/attachment/<id>
                                  (this relay)         (VPN tunnel)
                                  proxy_buffering off  (Bearer auth injected)
```

## Why this exists

- Phones can't easily run a server (background networking, app-store gate);
  pull-from-spoke avoids that.
- "Streaming relay" = bytes chunk through nginx RAM, **never written to
  disk**. Defeats casual disk forensics on the spoke (vs. store-and-forward
  which leaves a recoverable file).
- Spoke is the only thing on phone's LAN — hub is unreachable directly.
  Spoke acts as the gateway; phone never needs hub's IP or hub's token.

## Setup (on a spoke machine that has the hotspot)

Pre-req: spoke already has VPN connectivity to hub (Tailscale / WireGuard /
LAN), and you know hub's mailbox-server token.

```bash
cd relay
cp .env.example .env
# Edit .env — set RELAY_TOKEN (random), HUB_VPN_IP, HUB_TOKEN
docker compose up -d
docker compose ps    # mailbox-relay should be (healthy) after ~10s
```

## Use (from the phone)

1. Connect phone to spoke's WiFi hotspot.
2. Open browser, hit:

   ```
   http://<spoke-hotspot-ip>:1906/file/<attach-id>?token=<RELAY_TOKEN>
   ```

   Default hotspot IP on Windows is `192.168.137.1`.

3. Browser downloads the file. Spoke holds nothing afterwards.

## Where does the attach-id come from?

When an agent does `mcp__mailbox__send(to=spoke, files=[...])`, the mailbox
DB stores the attachment with an integer id. The receiver sees it as
`attach=N` in the watcher event line, or as `attachments: [{id: N, ...}]`
in `inbox()` results.

To hand a URL to user-on-phone, an agent typically does:

```python
url = f"http://{spoke_hotspot_ip}:1906/file/{attach_id}?token={relay_token}"
# then push that URL via Discord /agent-notify (bridge :1904), or simply
# print it in the spoke's CLI session.
```

## Auth model

Two tokens, two purposes:

| Token | Where it lives | Who sees it | Purpose |
|---|---|---|---|
| `HUB_TOKEN` | in `.env`, sent as `Authorization: Bearer` to hub | spoke ↔ hub only | hub's mailbox-server REST auth |
| `RELAY_TOKEN` | in `.env`, expected as `?token=` from phone | phone ↔ spoke only | keep random hotspot guests out |

Phone never knows `HUB_TOKEN`. nginx terminates the phone connection and
opens a fresh upstream connection to hub with the hub token baked in via
`proxy_set_header`.

## Trust model

- **Hub disk** — file lives here (normal mailbox attachment, content-addressed)
- **Spoke disk** — file NEVER lives here (streaming proxy)
- **Spoke RAM** — chunks pass through nginx worker memory in transit only;
  RAM forensics on the spoke machine while traffic is live would expose
  bytes. After the transfer completes, nginx's buffer is released — RAM
  contents typically overwritten by subsequent allocations within seconds.
- **TLS** — not enabled by default. Hotspot LAN is private; if you want
  defense against an attacker who joined the hotspot, terminate TLS on
  nginx (self-signed cert; phone has to "trust" it once).

## Healthcheck

`GET /health` returns `200 ok`. Docker healthcheck wraps this — if the
container shows `unhealthy`, the relay can't serve traffic. Check
`docker logs mailbox-relay`.

## Why not put this on hub?

Hub already serves on 1905. The relay's value is **spoke-side**: it lives
on the hotspot LAN, accepts phone traffic that hub can't see, and avoids
landing the file on the local disk. Running it on hub would be pointless
(hub already has the file on disk anyway).

## Future improvements (not in v0)

- Per-attachment one-time URLs (instead of shared `RELAY_TOKEN`)
- TLS on hotspot side (`certgen` self-signed + phone trust prompt)
- A `/list?to=<spoke-name>&token=...` endpoint listing recent attachments
  with clickable URLs, so user doesn't need to know IDs
- mDNS advertisement so phones discover the relay automatically
- Integration with the bridge `/agent-notify` so wiki can push the URL
  to phone Discord DM automatically when files arrive
