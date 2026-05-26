"""Single-source-of-truth handler for a Discord-sourced inbound message.

Called from both directions:
- HTTP /from-discord webhook (legacy path, posted by node-red)
- discord.py on_message (direct gateway, 2026-05-19+)

Returns (status_code, response_dict) so the HTTP side can serialize and the
gateway side can log + decide whether to ack.
"""
import sqlite3
import sys

from .attachments import attachments_dir_for, relay_discord_attachments
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
    if is_trusted:
        for prefix in ('@koatag-frontend ', '@koatag ', '@stranger-conv '):
            if content.lower().startswith(prefix.lower()):
                to_name = prefix[1:-1]
                content = content[len(prefix):]
                break

    # === Build from_name ===
    if to_name == 'stranger-conv' and channel:
        from_name = f'user-discord ({author}) ch={channel}'
    elif author != 'discord-user':
        from_name = f'user-discord ({author})'
    else:
        from_name = 'user-discord'

    # === INSERT ===
    # has_attachments must be set on the messages row at INSERT time so that
    # SSE/inbox readers (which gate the attachments JOIN on this column) see
    # the parent + children atomically.
    has_atts = 1 if attachments else 0
    stored = []
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            'INSERT INTO messages (from_name, to_name, body, has_attachments) '
            'VALUES (?, ?, ?, ?)',
            (from_name, to_name, content, has_atts),
        )
        mid = cur.lastrowid
        if attachments:
            stored = relay_discord_attachments(
                conn, mid, attachments_dir_for(db_path), attachments)
            # Roll back has_attachments to 0 if every attachment download failed
            # (rare — would only happen if Discord CDN is down for ALL files in
            # the same message). Leaves the body text intact so user doesn't
            # lose the caption.
            if not stored:
                conn.execute(
                    'UPDATE messages SET has_attachments=0 WHERE id=?', (mid,))
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
