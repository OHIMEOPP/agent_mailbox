"""mailbox-relay v1 — spoke-side aiohttp app.

Provides a phone-friendly HTML interface plus streaming proxy to hub's
mailbox-server `/attachment/<id>`. Bytes never land on this spoke's disk.

Endpoints (all phone-side requests gated by `?token=<RELAY_TOKEN>`):
  GET  /              → 404
  GET  /health        → 200 "ok" (no token required)
  GET  /list          → HTML page listing attachments for MAILBOX_RECIPIENT
  GET  /file/<id>     → streaming proxy of hub /attachment/<id>
  GET  /thumb/<id>    → streaming proxy of hub /thumb/<id> (JPEG, cached on hub)
  GET  /zip/<ids>     → stream a zip of every attachment for one or more
                        comma-separated msg_ids (e.g. /zip/1090 or /zip/1117,1120)
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
import io
import json
import os
import re
import sys
import zipfile
from urllib.parse import quote, urlencode

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
            "msg_body": a.get("message_body") or "",
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


async def handle_thumb(request: web.Request) -> web.StreamResponse:
    if not _token_ok(request):
        return _forbidden()

    attach_id = request.match_info["id"]
    if not attach_id.isdigit():
        return web.Response(status=400, text="attachment id must be integer\n")

    width = request.query.get("w", "200")
    if not width.isdigit():
        return web.Response(status=400, text="w must be integer\n")

    session = request.app["session"]
    url = f"{HUB_BASE}/thumb/{attach_id}?w={width}"

    try:
        upstream_ctx = session.get(url, headers=HUB_HEADERS, timeout=30)
        upstream = await upstream_ctx.__aenter__()
    except aiohttp.ClientError as e:
        return web.Response(status=502, text=f"upstream unreachable: {e}\n")

    try:
        if upstream.status != 200:
            body = await upstream.read()
            return web.Response(status=upstream.status, body=body)

        headers = {
            "Content-Type": upstream.headers.get("Content-Type", "image/jpeg"),
            # Mirror hub's 24h cache so the phone browser stops re-fetching
            # thumbs on every /list refresh.
            "Cache-Control": upstream.headers.get(
                "Cache-Control", "public, max-age=86400"
            ),
        }
        cl = upstream.headers.get("Content-Length")
        if cl:
            headers["Content-Length"] = cl
        resp = web.StreamResponse(status=200, headers=headers)
        await resp.prepare(request)

        async for chunk in upstream.content.iter_chunked(32 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        await upstream_ctx.__aexit__(None, None, None)


async def handle_zip(request: web.Request) -> web.StreamResponse:
    if not _token_ok(request):
        return _forbidden()

    raw = request.match_info["msg_ids"]
    msg_id_set: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            return web.Response(
                status=400,
                text=f"msg_ids must be comma-separated integers, got {part!r}\n",
            )
        msg_id_set.add(int(part))

    session = request.app["session"]
    # Fetch the same flat attachment list /list uses, then filter to the union
    # of requested msg_ids. limit=500 covers our largest observed batch
    # (ComfyUI/output 47 PNGs); if someone ever sends >500 we'd lose tail.
    list_url = f"{HUB_BASE}/attachments?to={MAILBOX_RECIPIENT}&limit=500"
    try:
        async with session.get(list_url, headers=HUB_HEADERS, timeout=10) as r:
            if r.status != 200:
                body = await r.text()
                return web.Response(
                    status=502,
                    text=f"upstream /attachments returned {r.status}\n{body}\n",
                )
            data = await r.json()
    except aiohttp.ClientError as e:
        return web.Response(status=502, text=f"upstream unreachable: {e}\n")

    target = [
        a for a in data.get("attachments", [])
        if int(a.get("message_id", -1)) in msg_id_set
    ]
    if not target:
        return web.Response(
            status=404,
            text=f"no attachments for msg_ids={sorted(msg_id_set)}\n",
        )

    # Derive a friendly zip filename from the first message body's normalized
    # first line. _normalize_body strips per-message variable counts so
    # "ComfyUI/output (auto-sync, 4 檔)" → "ComfyUI/output". Sanitise so the
    # Content-Disposition value stays sane across phone download UIs.
    msg_body = (target[0].get("message_body") or "")
    label = _normalize_body(msg_body)
    safe = re.sub(r"[^\w\-一-鿿]+", "_", label).strip("_")[:60]
    fallback = f"msg-{min(msg_id_set)}"
    zip_name = f"{safe or fallback}.zip"
    ascii_fallback = zip_name.encode("ascii", errors="replace").decode("ascii")
    cd = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(zip_name, safe='')}"
    )

    return await _stream_zip_response(request, session, target, zip_name)


async def _stream_zip_response(
    request: web.Request,
    session: aiohttp.ClientSession,
    attachments: list[dict],
    zip_name: str,
) -> web.StreamResponse:
    """Stream a ZIP_STORED archive of `attachments` (list of dicts with at
    least `id` and `filename`). Bytes flow chunk-by-chunk; peak RAM = the
    largest single attachment because each entry needs a CRC computed up
    front before we can flush.
    """
    ascii_fallback = zip_name.encode("ascii", errors="replace").decode("ascii")
    cd = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(zip_name, safe='')}"
    )

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/zip",
            "Cache-Control": "no-store",
            "Content-Disposition": cd,
        },
    )
    # Length unknown ahead of time → chunked encoding (aiohttp default when
    # no Content-Length set).
    await resp.prepare(request)

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED,
                         allowZip64=True)

    async def _flush() -> None:
        chunk = buf.getvalue()
        if chunk:
            await resp.write(chunk)
        buf.seek(0)
        buf.truncate(0)

    seen_names: dict[str, int] = {}
    for a in attachments:
        attach_id = a["id"]
        raw_name = a.get("filename") or f"attach-{attach_id}"
        # Dedupe within the zip — same filename twice gets a numbered
        # suffix so unzip tools don't silently overwrite.
        n = seen_names.get(raw_name, 0)
        if n > 0:
            stem, dot, ext = raw_name.rpartition(".")
            if dot:
                fname = f"{stem}_{n}.{ext}"
            else:
                fname = f"{raw_name}_{n}"
        else:
            fname = raw_name
        seen_names[raw_name] = n + 1

        file_url = f"{HUB_BASE}/attachment/{attach_id}"
        try:
            async with session.get(file_url, headers=HUB_HEADERS,
                                   timeout=120) as upstream:
                if upstream.status != 200:
                    zf.writestr(
                        f"{fname}.ERROR.txt",
                        f"hub /attachment/{attach_id} returned "
                        f"{upstream.status}\n",
                    )
                    await _flush()
                    continue
                chunks: list[bytes] = []
                async for c in upstream.content.iter_chunked(64 * 1024):
                    chunks.append(c)
                payload = b"".join(chunks)
        except aiohttp.ClientError as e:
            zf.writestr(
                f"{fname}.ERROR.txt",
                f"hub /attachment/{attach_id} unreachable: {e}\n",
            )
            await _flush()
            continue

        zf.writestr(fname, payload)
        await _flush()

    zf.close()
    await _flush()
    await resp.write_eof()
    return resp


async def handle_delete(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return _forbidden()

    data = await request.post()
    # Same KeyError trap as handle_zip_selected — pass [] default so an
    # empty form lands on the 400 below instead of a stack-trace 500.
    ids = data.getall("ids", []) if hasattr(data, "getall") else []
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


def _normalize_body(body: str) -> str:
    """Reduce a message body to a stable group title.

    Strips parentheticals that carry a per-message variable count (e.g.
    folder-sync's "ComfyUI/output (auto-sync, 4 檔)") so successive sends
    with different counts collapse into one group.
    """
    first_line = (body or "").strip().splitlines()[0] if body else ""
    first_line = re.sub(
        r"\s*\([^()]*\d+\s*(檔|files?)\s*[^()]*\)\s*$",
        "",
        first_line,
    ).strip()
    if len(first_line) > 80:
        first_line = first_line[:80] + "…"
    return first_line


def _render_list_html(rows: list[dict]) -> str:
    qs_token = f"?token={RELAY_TOKEN}"
    if not rows:
        body = '<p class="empty">沒有附件。</p>'
    else:
        # Group rows by (sender, normalized_body). Same source + same logical
        # "folder name" merge into one section regardless of how many separate
        # mailbox messages it took (folder-sync emits one message per detected
        # batch, so a single logical "folder" gets sliced across many msg_ids).
        # Order = first appearance in the DESC stream → newest groups on top.
        groups: list[dict] = []
        groups_by_key: dict[tuple, dict] = {}
        for r in rows:
            key = (r["from"], _normalize_body(r["msg_body"]))
            g = groups_by_key.get(key)
            if g is None:
                g = {
                    "from": r["from"],
                    "title": key[1] or "(沒有訊息標題)",
                    "sent_at": r["sent_at"],
                    "msg_ids": set(),
                    "rows": [],
                }
                groups_by_key[key] = g
                groups.append(g)
            g["rows"].append(r)
            g["msg_ids"].add(r["msg_id"])
            # Latest sent_at across the merged messages — surfaced in header.
            if r["sent_at"] and r["sent_at"] > g["sent_at"]:
                g["sent_at"] = r["sent_at"]

        sections = []
        for g in groups:
            header_text = g["title"]
            sender = html.escape(g["from"])
            when = _short_when(g["sent_at"])
            count = len(g["rows"])
            tr_lines = []
            for r in g["rows"]:
                fname = html.escape(r["filename"])
                row_sender = html.escape(r["from"])
                mime = html.escape(r["mime"])
                size = _human_size(r["size"])
                row_when = _short_when(r["sent_at"])
                file_href = f"/file/{r['id']}{qs_token}"
                # Inline thumbnail for image MIME types. Pulls from hub's
                # /thumb/<id> which Pillow-resizes + JPEG-caches (~10-30KB
                # each) instead of sending the full original — phones loaded
                # 50+ multi-MB PNGs in the original CSS-only build.
                if (r["mime"] or "").startswith("image/"):
                    thumb_src = f"/thumb/{r['id']}{qs_token}&w=200"
                    thumb_html = (
                        f'<a class="thumb-link" href="{file_href}">'
                        f'<img class="thumb" src="{thumb_src}" alt="" '
                        f'loading="lazy"></a> '
                    )
                else:
                    thumb_html = ""
                tr_lines.append(
                    f'<tr>'
                    f'<td><input type="checkbox" name="ids" value="{r["id"]}" form="bulk-form"></td>'
                    f'<td class="name-cell">{thumb_html}<a href="{file_href}">{fname}</a></td>'
                    f'<td class="size">{size}</td>'
                    f'<td class="mime">{mime}</td>'
                    f'<td class="from">{row_sender}</td>'
                    f'<td class="when">{row_when}</td>'
                    f'</tr>'
                )
            # Default open if small group (≤5 files), collapsed otherwise to
            # keep the page scannable when batches are large (e.g. 47 PNGs).
            open_attr = " open" if count <= 5 else ""
            # Zip-all link sits inside <summary> with stopPropagation so the
            # click downloads without toggling the group open/closed. Path is
            # comma-joined msg_ids so handle_zip can fan-out one DB query and
            # filter on the union — folder-sync merged groups end up here too.
            msg_ids_path = ",".join(str(m) for m in sorted(g["msg_ids"]))
            zip_href = f"/zip/{msg_ids_path}{qs_token}"
            zip_link = (
                f'<a class="zip-link" href="{zip_href}" '
                f'onclick="event.stopPropagation()">📦 打包下載</a>'
            )
            sections.append(
                f'<details class="group"{open_attr}>'
                f'<summary class="group-head">'
                f'<span class="group-title">{html.escape(header_text)}</span>'
                f'<span class="group-meta">{count} 檔 · {sender} · {when}</span>'
                f'{zip_link}'
                f'</summary>'
                f'<table>'
                f'<thead><tr><th></th><th>檔名</th><th>大小</th><th>類型</th><th>From</th><th>時間</th></tr></thead>'
                f'<tbody>{"".join(tr_lines)}</tbody>'
                f'</table>'
                f'</details>'
            )

        # Bulk-form keeps the delete submit (POST → server-side fan-out
        # delete via hub). "Download selected" used to also submit-zip but
        # user wanted individual files, not a single archive, so that
        # button now runs client-side JS that triggers one /file/<id>
        # download per checked id (200ms stagger so mobile browsers don't
        # dedupe the consecutive clicks). The per-group 📦 zip-link is
        # still available for cases where a single archive is preferable.
        body = (
            f'<form id="bulk-form" method="post">'
            f'<button type="button" class="btn-selectall" '
            f'onclick="toggleAll()">☑️ 全選 / 全不選</button>'
            f'<button type="button" class="btn-download" '
            f'onclick="downloadSelected()">⬇️ 下載選取</button>'
            f'<button type="submit" class="btn-delete" '
            f'formaction="/delete{qs_token}" '
            f'onclick="return confirm(\'刪除選取的附件？此動作會清空 hub blob（無其他引用時）+ DB row，不影響 sender 原檔。\');">'
            f'🗑️ 刪除選取</button>'
            f'</form>'
            + "".join(sections)
        )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>mailbox 附件</title>
<style>
  :root {{
    --bg: #ffffff;
    --fg: #1a1a1a;
    --muted: #666;
    --border: #e5e5e5;
    --row-alt: #fafafa;
    --link: #0066cc;
    --link-active: #cc4400;
    --danger: #d33;
    --danger-fg: #ffffff;
    --head-bg: #f6f6f6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a1a;
      --fg: #e8e8e8;
      --muted: #999;
      --border: #333;
      --row-alt: #222;
      --link: #4ea1ff;
      --link-active: #ff8a3d;
      --danger: #c44;
      --head-bg: #2a2a2a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--bg); color: var(--fg); }}
  body {{
    font-family: -apple-system, "Segoe UI", "Noto Sans CJK TC", sans-serif;
    padding: 12px;
    padding-left: max(12px, env(safe-area-inset-left));
    padding-right: max(12px, env(safe-area-inset-right));
    padding-bottom: max(12px, env(safe-area-inset-bottom));
    max-width: 720px;
    margin: 0 auto;
    -webkit-text-size-adjust: 100%;
  }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 12px; }}
  a {{ color: var(--link); text-decoration: none; word-break: break-all; }}
  a:active {{ color: var(--link-active); }}
  button {{
    font-size: 15px;
    padding: 12px 18px;
    margin: 8px 6px 8px 0;
    color: var(--danger-fg);
    border: none;
    border-radius: 8px;
    min-height: 44px;
    touch-action: manipulation;
  }}
  .btn-selectall {{ background: var(--muted); }}
  .btn-download {{ background: var(--link); }}
  .btn-delete {{ background: var(--danger); }}
  .btn-selectall:active, .btn-download:active, .btn-delete:active {{ filter: brightness(0.9); }}
  .empty {{ color: var(--muted); text-align: center; padding: 40px 0; }}
  input[type="checkbox"] {{
    width: 22px;
    height: 22px;
    margin: 0;
    cursor: pointer;
    accent-color: var(--link);
  }}
  details.group {{ margin: 14px 0 22px; }}
  details.group > summary.group-head {{
    font-size: 14px;
    font-weight: 600;
    margin: 0 0 8px;
    padding: 10px 12px 10px 36px;
    background: var(--head-bg);
    border-radius: 8px;
    display: flex;
    gap: 6px 10px;
    flex-wrap: wrap;
    align-items: baseline;
    line-height: 1.4;
    cursor: pointer;
    list-style: none;
    position: relative;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
  }}
  details.group > summary.group-head::-webkit-details-marker {{ display: none; }}
  details.group > summary.group-head::before {{
    content: "▶";
    position: absolute;
    left: 14px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 11px;
    color: var(--muted);
    transition: transform 0.15s ease;
  }}
  details.group[open] > summary.group-head::before {{ transform: translateY(-50%) rotate(90deg); }}
  details.group > summary.group-head:hover {{ filter: brightness(1.05); }}
  .group-title {{ white-space: pre-wrap; word-break: break-word; flex: 1 1 auto; min-width: 0; }}
  .group-meta {{ color: var(--muted); font-size: 12px; font-weight: 400; white-space: nowrap; }}
  .zip-link {{
    font-size: 12px;
    font-weight: 500;
    padding: 6px 10px;
    background: var(--link);
    color: var(--bg);
    border-radius: 6px;
    white-space: nowrap;
    flex: 0 0 auto;
    text-decoration: none;
    min-height: 28px;
    display: inline-flex;
    align-items: center;
  }}
  .zip-link:hover {{ filter: brightness(1.1); }}
  .zip-link:active {{ filter: brightness(0.9); color: var(--bg); }}
  img.thumb {{
    max-width: 64px;
    max-height: 64px;
    vertical-align: middle;
    margin-right: 8px;
    border-radius: 4px;
    background: var(--row-alt);
    object-fit: cover;
  }}
  .thumb-link {{ display: inline-block; vertical-align: middle; }}

  /* ---------- desktop / tablet (≥600px): table layout ---------- */
  @media (min-width: 600px) {{
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 10px 6px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: middle; }}
    th {{ background: var(--head-bg); }}
    td.size, td.mime, td.when, td.from {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
    tr:hover td {{ background: var(--row-alt); }}
  }}

  /* ---------- phone (<600px): card layout ---------- */
  @media (max-width: 599px) {{
    table, tbody {{ display: block; width: 100%; }}
    thead {{ display: none; }}
    tr {{
      display: grid;
      grid-template-columns: 32px 1fr auto;
      grid-template-areas:
        "check name name"
        "check size when";
      gap: 2px 10px;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      margin-bottom: 10px;
    }}
    tr td {{ padding: 0; border: none; display: block; }}
    tr td:nth-child(1) {{ grid-area: check; align-self: start; padding-top: 4px; }}
    tr td:nth-child(2) {{ grid-area: name; font-size: 16px; line-height: 1.4; }}
    tr td:nth-child(2) a {{ display: inline-block; padding: 6px 0; min-height: 32px; }}
    tr td:nth-child(2) img.thumb {{ max-width: 96px; max-height: 96px; display: block; margin: 0 0 6px; }}
    tr td:nth-child(3) {{ grid-area: size; color: var(--muted); font-size: 12px; }}
    tr td:nth-child(6) {{ grid-area: when; color: var(--muted); font-size: 12px; text-align: right; white-space: nowrap; }}
    /* hide mime + from on phone (low signal, eat space) */
    tr td:nth-child(4), tr td:nth-child(5) {{ display: none; }}
    tr td:nth-child(1) input[type="checkbox"] {{ width: 26px; height: 26px; }}
  }}
</style>
</head>
<body>
<h1>📦 {html.escape(MAILBOX_RECIPIENT)} 收到的附件</h1>
<p class="meta">{len(rows)} 個檔案 · 點檔名下載 · 勾選後上方按鈕「下載」逐檔抓 / 「刪除」清除</p>
{body}
<script>
  const RELAY_TOKEN_Q = {json.dumps('?token=' + RELAY_TOKEN)};
  function toggleAll() {{
    const boxes = document.querySelectorAll('input[name="ids"]');
    const allChecked = boxes.length > 0 && Array.from(boxes).every(b => b.checked);
    boxes.forEach(b => {{ b.checked = !allChecked; }});
  }}
  function downloadSelected() {{
    const checked = document.querySelectorAll('input[name="ids"]:checked');
    if (checked.length === 0) {{ alert('沒選任何檔案'); return; }}
    Array.from(checked).forEach((c, idx) => {{
      setTimeout(() => {{
        const a = document.createElement('a');
        a.href = '/file/' + c.value + RELAY_TOKEN_Q;
        a.setAttribute('download', '');
        a.rel = 'noopener';
        document.body.appendChild(a);
        a.click();
        a.remove();
      }}, idx * 250);
    }});
  }}
</script>
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
    app.router.add_get(r"/thumb/{id:[0-9]+}", handle_thumb)
    app.router.add_get(r"/zip/{msg_ids:[0-9,]+}", handle_zip)
    app.router.add_post("/delete", handle_delete)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=BIND, port=INTERNAL_PORT, print=None)


if __name__ == "__main__":
    main()
