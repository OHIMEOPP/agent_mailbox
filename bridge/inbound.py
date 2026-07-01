"""Single-source-of-truth handler for a Discord-sourced inbound message.

Called from both directions:
- HTTP /from-discord webhook (legacy path, posted by node-red)
- discord.py on_message (direct gateway, 2026-05-19+)

Returns (status_code, response_dict) so the HTTP side can serialize and the
gateway side can log + decide whether to ack.
"""
import sqlite3
import sys

from .attachments import (attachments_dir_for, fetch_and_blob,
                          insert_attachment_rows)
from .config import ALLOW_DENY_RE, OFFLINE_THRESHOLD_SECONDS, TRUSTED_USER
from .heartbeat import agent_recently_active
from .notify import notify_command_result, notify_offline, notify_stranger_pending
from .whitelist import approve_user, deny_user, is_whitelisted, queue_pending


def process_discord_inbound(content, author, author_id, channel, to_name_hint, db_path,
                            attachments=None):
    content = (content or '').strip()
    attachments = attachments or []
    if not content and not attachments:
        return (400, {'ok': False, 'error': 'empty_content'})

    author = author or 'discord-user'
    is_trusted = author.lower() == TRUSTED_USER
    to_name = to_name_hint or 'wiki'

    # === Stranger gate ===
    if not is_trusted:
        if is_whitelisted(author):
            to_name = 'stranger-conv'
        else:
            pid = queue_pending(author, content, author_id, channel)
            sys.stdout.write(f"[inbound] stranger DM queued pending #{pid} from {author!r} "
                             f"(id={author_id} ch={channel}): {content[:80]!r}\n")
            notify_stranger_pending(author, content)
            return (202, {'ok': True, 'pending': pid, 'note': 'awaiting approval'})

    # === Trusted-user inline commands (allow / deny) ===
    if is_trusted:
        m = ALLOW_DENY_RE.match(content)
        if m:
            action, target = m.group(1).lower(), m.group(2)
            if action == 'allow':
                count, err = approve_user(target, db_path)
            else:
                count, err = deny_user(target)
            sys.stdout.write(f"[inbound] cmd {action} {target} -> count={count} err={err}\n")
            notify_command_result(action, target, count, err)
            return (200, {'ok': err is None, 'cmd': action, 'target': target,
                          'count': count, 'error': err})

    # === Trusted-user @prefix routing override ===
    # Map a DM prefix -> recipient mailbox name. Bare-name targets map to
    # themselves; aliases (laptop) map to the full <role>@<hostname> name so the
    # user doesn't have to type the hostname. Order matters: longer/more-specific
    # prefixes first (e.g. @koatag-frontend before @koatag).
    if is_trusted:
        route_prefixes = {
            '@koatag-frontend ': 'koatag-frontend',
            '@koatag ': 'koatag',
            '@stranger-conv ': 'stranger-conv',
            '@mailbox-dev ': 'mailbox-dev',
            '@laptop ': 'wiki@LAPTOP-MQ1OGSN5',
            '@wiki-laptop ': 'wiki@LAPTOP-MQ1OGSN5',
            '@supporters ': 'supporters',
        }
        for prefix, target in route_prefixes.items():
            if content.lower().startswith(prefix.lower()):
                to_name = target
                content = content[len(prefix):]
                break

    # === Build from_name ===
    if to_name == 'stranger-conv' and channel:
        from_name = f'user-discord ({author}) ch={channel}'
    elif author != 'discord-user':
        from_name = f'user-discord ({author})'
    else:
        from_name = 'user-discord'

    # === Download + blob first (no DB lock held during multi-second network) ===
    # Keeping the SQLite write txn open while we download from Discord CDN
    # caused "disk I/O error" under contention with the watcher heartbeat
    # writer (#1539). Network + filesystem I/O now happen BEFORE we touch
    # the DB; the txn below is sub-ms.
    blobs = []
    if attachments:
        blobs = fetch_and_blob(
            attachments_dir_for(db_path), attachments,
            log_prefix=f"{from_name} -> {to_name} ")

    # === INSERT ===
    # has_attachments computed from blobs (post-download) — if all downloads
    # failed we cleanly store 0 from the start, no rollback UPDATE needed.
    has_atts = 1 if blobs else 0
    stored = []
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        cur = conn.execute(
            'INSERT INTO messages (from_name, to_name, body, has_attachments) '
            'VALUES (?, ?, ?, ?)',
            (from_name, to_name, content, has_atts),
        )
        mid = cur.lastrowid
        if blobs:
            stored = insert_attachment_rows(conn, mid, blobs)
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        return (500, {'ok': False, 'error': f'db: {e}'})

    attach_tag = f" attach={len(stored)}" if stored else ""
    sys.stdout.write(f"[inbound] msg #{mid} {from_name} -> {to_name} "
                     f"(ch={channel}){attach_tag}: {content[:80]!r}\n")

    # === Offline detection ===
    if not agent_recently_active(db_path, to_name, OFFLINE_THRESHOLD_SECONDS):
        sys.stdout.write(f"[inbound] {to_name} appears offline "
                         f"(>{OFFLINE_THRESHOLD_SECONDS}s), notifying user\n")
        notify_offline(mid, to_name)

    return (200, {'ok': True, 'id': mid, 'to': to_name,
                  'attachments': stored})
