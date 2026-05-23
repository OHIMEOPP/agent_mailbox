"""mailbox-relay v1 — spoke-side aiohttp app.

Provides a phone-friendly HTML interface plus streaming proxy to hub's
mailbox-server `/attachment/<id>`. Bytes never land on this spoke's disk.

Endpoints (all phone-side requests gated by `?token=<RELAY_TOKEN>`):
  GET  /              → 404
  GET  /health        → 200 "ok" (no token required)
  GET  /list          → HTML page listing attachments for MAILBOX_RECIPIENT
  GET  /file/<id>     → streaming proxy of hub /attachment/<id>
  POST /delete        → form: ids=N&ids=M → DELETE each on hub → 303 → /list

Env (set in docker-compose .env):
  RELAY_TOKEN         phone-side shared secret (required)
  HUB_VPN_IP          hub address reachable from this spoke (required)
  HUB_PORT            hub mailbox-server port (default 1905)
  HUB_TOKEN           hub Bearer token (= spoke's CLAUDE_MAILBOX_TOKEN; required)
  MAILBOX_RECIPIENT   whose attachments to list (default "wiki")
  RELAY_BIND          bind address (default 0.0.0.0)
  RELAY_PORT_INTERNAL container-internal port (default 80; compose maps host:1906)
"""
import asyncio
import datetime
import html
import os
import sys
from urllib.parse import urlencode

import aiohttp
from aiohttp import web


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.stderr.write(f"[relay] FATAL: env {name} required\n")
        sys.exit(2)
    return v


RELAY_TOKEN = _required("RELAY_TOKEN")
HUB_VPN_IP = _required("HUB_VPN_IP")
HUB_PORT = int(os.environ.get("HUB_PORT", "1905"))
HUB_TOKEN = _required("HUB_TOKEN")
MAILBOX_RECIPIENT = os.environ.get("MAILBOX_RECIPIENT", "wiki").strip()
BIND = os.environ.get("RELAY_BIND", "0.0.0.0")
INTERNAL_PORT = int(os.environ.get("RELAY_PORT_INTERNAL", "80"))

HUB_BASE = f"http://{HUB_VPN_IP}:{HUB_PORT}"
HUB_HEADERS = {"Authorization": f"Bearer {HUB_TOKEN}"}


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _token_ok(request: web.Request) -> bool:
    return request.query.get("token") == RELAY_TOKEN


def _forbidden() -> web.Response:
    return web.Response(status=403, text="forbidden: missing or invalid ?token\n")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(status=404)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def handle_list(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return _forbidden()

    session = request.app["session"]
    # Use dedicated /attachments endpoint (DESC by attachment id, newest first).
    # /inbox would order by message id ASC and paginate by message, so newly
    # arrived files don't surface until you bump limit very high.
    url = f"{HUB_BASE}/attachments?to={MAILBOX_RECIPIENT}&limit=200"
    try:
        async with session.get(url, headers=HUB_HEADERS, timeout=10) as r:
            if r.status != 200:
                body = await r.text()
                return web.Response(
                    status=502,
                    text=f"upstream /attachments returned {r.status}\n{body}\n",
                )
            data = await r.json()
    except aiohttp.ClientError as e:
        return web.Response(status=502, text=f"upstream unreachable: {e}\n")

    rows = []
    for a in data.get("attachments", []):
        rows.append({
            "id": a["id"],
            "filename": a.get("filename") or "(no name)",
            "mime": a.get("mime") or "?",
            "size": a.get("size") or 0,
            "msg_id": a.get("message_id"),
            "from": a.get("from_name") or "?",
            "sent_at": a.get("sent_at") or "",
        })

    return web.Response(
        text=_render_list_html(rows),
        content_type="text/html",
    )


async def handle_file(request: web.Request) -> web.StreamResponse:
    if not _token_ok(request):
        return _forbidden()

    attach_id = request.match_info["id"]
    if not attach_id.isdigit():
        return web.Response(status=400, text="attachment id must be integer\n")

    session = request.app["session"]
    url = f"{HUB_BASE}/attachment/{attach_id}"

    # Streaming proxy: open upstream, get headers, forward status + selected
    # headers to client, then pipe bytes chunk by chunk. Nothing lands on disk.
    try:
        upstream_ctx = session.get(url, headers=HUB_HEADERS, timeout=60)
        upstream = await upstream_ctx.__aenter__()
    except aiohttp.ClientError as e:
        return web.Response(status=502, text=f"upstream unreachable: {e}\n")

    try:
        if upstream.status != 200:
            body = await upstream.read()
            return web.Response(status=upstream.status, body=body)

        resp = web.StreamResponse(status=200, headers={
            "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
            "Cache-Control": "no-store",
        })
        cd = upstream.headers.get("Content-Disposition")
        if cd:
            resp.headers["Content-Disposition"] = cd
        cl = upstream.headers.get("Content-Length")
        if cl:
            resp.headers["Content-Length"] = cl
        sha = upstream.headers.get("X-Mailbox-Sha256")
        if sha:
            resp.headers["X-Mailbox-Sha256"] = sha
        await resp.prepare(request)

        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        await upstream_ctx.__aexit__(None, None, None)


async def handle_delete(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return _forbidden()

    data = await request.post()
    ids = data.getall("ids") if hasattr(data, "getall") else []
    if not ids:
        return web.Response(status=400, text="no ids specified\n")

    session = request.app["session"]
    results = []
    for raw_id in ids:
        if not raw_id.isdigit():
            results.append((raw_id, "invalid id", None))
            continue
        url = f"{HUB_BASE}/attachment/{raw_id}"
        try:
            async with session.delete(url, headers=HUB_HEADERS, timeout=10) as r:
                results.append((raw_id, r.status, await r.text()))
        except aiohttp.ClientError as e:
            results.append((raw_id, "error", str(e)))

    # Redirect back to /list so user sees updated state.
    redirect = f"/list?{urlencode({'token': RELAY_TOKEN})}"
    return web.HTTPSeeOther(location=redirect)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _short_when(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _render_list_html(rows: list[dict]) -> str:
    qs_token = f"?token={RELAY_TOKEN}"
    if not rows:
        body = '<p class="empty">沒有附件。</p>'
    else:
        lines = []
        for r in rows:
            fname = html.escape(r["filename"])
            sender = html.escape(r["from"])
            mime = html.escape(r["mime"])
            size = _human_size(r["size"])
            when = _short_when(r["sent_at"])
            file_href = f"/file/{r['id']}{qs_token}"
            lines.append(
                f'<tr>'
                f'<td><input type="checkbox" name="ids" value="{r["id"]}" form="del-form"></td>'
                f'<td><a href="{file_href}">{fname}</a></td>'
                f'<td class="size">{size}</td>'
                f'<td class="mime">{mime}</td>'
                f'<td class="from">{sender}</td>'
                f'<td class="when">{when}</td>'
                f'</tr>'
            )
        body = (
            f'<form id="del-form" method="post" action="/delete{qs_token}" '
            f'onsubmit="return confirm(\'刪除選取的附件？此動作會清空 hub blob（無其他引用時）+ DB row，不影響 sender 原檔。\');">'
            f'<button type="submit">🗑️ 刪除選取</button>'
            f'</form>'
            f'<table>'
            f'<thead><tr><th></th><th>檔名</th><th>大小</th><th>類型</th><th>From</th><th>時間</th></tr></thead>'
            f'<tbody>{"".join(lines)}</tbody>'
            f'</table>'
        )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mailbox 附件</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; padding: 12px; max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  .meta {{ color: #666; font-size: 12px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #eee; font-size: 13px; }}
  th {{ background: #f6f6f6; }}
  td.size, td.mime, td.when, td.from {{ color: #555; font-size: 12px; white-space: nowrap; }}
  a {{ color: #0077cc; text-decoration: none; word-break: break-all; }}
  a:active {{ color: #cc4400; }}
  button {{ font-size: 14px; padding: 8px 14px; margin: 8px 0; background: #d33; color: white; border: none; border-radius: 6px; }}
  .empty {{ color: #888; text-align: center; padding: 40px 0; }}
</style>
</head>
<body>
<h1>📦 {html.escape(MAILBOX_RECIPIENT)} 收到的附件</h1>
<p class="meta">{len(rows)} 個檔案 · 點檔名下載 · 勾選後按下方按鈕刪除</p>
{body}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession()
    sys.stdout.write(
        f"[relay] up on {BIND}:{INTERNAL_PORT}, "
        f"hub={HUB_VPN_IP}:{HUB_PORT}, recipient={MAILBOX_RECIPIENT}\n"
    )
    sys.stdout.flush()


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()


def main() -> None:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/list", handle_list)
    app.router.add_get(r"/file/{id:[0-9]+}", handle_file)
    app.router.add_post("/delete", handle_delete)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=BIND, port=INTERNAL_PORT, print=None)


if __name__ == "__main__":
    main()
