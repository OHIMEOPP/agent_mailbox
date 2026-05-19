"""Outbound notifications.

Two purposes:
1. **Internal alerts** to user via the legacy node-red /agent-notify endpoint
   (NOTIFY_URL) — used by the bridge's own offline / stranger / command-result
   helpers. These still POST to node-red so they get the rich rendering. If
   node-red is down, they fail soft (logged).

2. **Public `/agent-notify` endpoint** served on :1904, which the http_server
   module exposes. Uses Discord REST API (no gateway dependency). The helpers
   here (_format_notify_message, _discord_send_dm) implement the work.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from .config import (
    DISCORD_API_BASE,
    DISCORD_BOT_TOKEN,
    NOTIFY_ICON,
    NOTIFY_URL,
)


# === Legacy node-red callbacks (bridge -> user) ============================

def _post_node_red(body, tag):
    try:
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json; charset=utf-8'},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stdout.write(f"[notify] {tag} fail: {e}\n")


def notify_offline(msg_id, to_name):
    """Discord DM: target agent is offline, mail queued for next session."""
    _post_node_red({
        'agent': to_name,
        'task': '⏸ offline，訊息已 queue',
        'status': 'warn',
        'detail': f'msg #{msg_id} 已存進 mailbox，下次 {to_name} session 啟動會處理。'
                  f'若急可直接開 Claude Code 進對應 working dir。',
    }, tag='offline-notify')


def notify_stranger_pending(username, preview):
    """Discord DM: unknown user DMed; await allow/deny."""
    _post_node_red({
        'agent': 'wiki',
        'task': f'👤 stranger DM: {username}',
        'status': 'warn',
        'detail': (
            f'username: {username}\n'
            f'預覽 (200字): {preview[:200]}\n\n'
            f'核可指令（DM 給 wiki）：\n'
            f'- 「allow {username}」  → promote pending DMs to stranger-conv\n'
            f'- 「deny {username}」   → discard pending DMs'
        ),
    }, tag='stranger-notify')


def notify_command_result(action, target, count, err):
    """Discord DM: ack the user's allow/deny command ran."""
    if err:
        status = 'fail'
        task = f'❌ {action} {target} failed'
        detail = f'錯誤: {err}'
    elif action == 'allow':
        status = 'done'
        task = f'✅ allow {target}'
        detail = (f'已加入白名單；{count} 條 pending DM 已搬到 stranger-conv mailbox' if count
                  else f'已加入白名單；無 pending DM 可搬')
    else:  # deny
        status = 'done'
        task = f'✅ deny {target}'
        detail = (f'丟掉 {count} 條 pending DM；白名單不變' if count
                  else f'{target} 沒有 pending DM 可丟')
    _post_node_red(
        {'agent': 'wiki', 'task': task, 'status': status, 'detail': detail},
        tag='cmd-notify',
    )


# === Public /agent-notify endpoint helpers (used by http_server) ============

def format_notify_message(agent, task, status, detail):
    """Render the Discord DM body. Matches node-red /agent-notify flow:
        {icon} **[{agent}]** {task}
        {detail}
    """
    icon = NOTIFY_ICON.get((status or 'info').lower(), NOTIFY_ICON['info'])
    head = f"{icon} **[{agent}]**"
    if task:
        head += f" {task}"
    if detail:
        return f"{head}\n{detail}"
    return head


def discord_send_dm(channel_id, content, attachments=None):
    """POST to Discord REST API. Returns (ok, status_code, body).

    No gateway connection needed. Same bot token can be in use by a separate
    gateway client elsewhere — REST API is stateless.

    attachments: optional list of (filename, bytes) tuples. When present the
    request is sent as multipart/form-data per Discord API v10 spec, with
    payload_json + files[N] parts. None / empty = plain JSON POST.
    """
    if not DISCORD_BOT_TOKEN:
        return (False, 0, 'no_token')
    if not channel_id:
        return (False, 0, 'no_channel')

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    base_headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'User-Agent': 'mailbox-bridge-py/1.0',
    }

    if not attachments:
        payload = json.dumps({'content': content}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload, method='POST',
            headers={**base_headers, 'Content-Type': 'application/json'},
        )
    else:
        # multipart/form-data: payload_json + files[N] parts
        boundary = '----mailboxbridge' + os.urandom(8).hex()
        body_chunks = []
        payload = {
            'content': content,
            'attachments': [
                {'id': i, 'filename': fn}
                for i, (fn, _) in enumerate(attachments)
            ],
        }
        body_chunks.append(f'--{boundary}\r\n'.encode())
        body_chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
        body_chunks.append(b'Content-Type: application/json\r\n\r\n')
        body_chunks.append(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        body_chunks.append(b'\r\n')
        for i, (filename, content_bytes) in enumerate(attachments):
            body_chunks.append(f'--{boundary}\r\n'.encode())
            body_chunks.append(
                f'Content-Disposition: form-data; name="files[{i}]"; filename="{filename}"\r\n'.encode('utf-8')
            )
            body_chunks.append(b'Content-Type: application/octet-stream\r\n\r\n')
            body_chunks.append(content_bytes)
            body_chunks.append(b'\r\n')
        body_chunks.append(f'--{boundary}--\r\n'.encode())
        data = b''.join(body_chunks)
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={
                **base_headers,
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(data)),
            },
        )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (True, resp.status, resp.read().decode('utf-8', 'replace')[:200])
    except urllib.error.HTTPError as e:
        return (False, e.code, e.read().decode('utf-8', 'replace')[:300])
    except Exception as e:
        return (False, 0, f'{type(e).__name__}: {e}')
