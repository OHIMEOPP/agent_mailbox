"""Send a Discord DM with file attachment(s) via the bridge.

(Renamed 2026-05 from mailbox-send-file.py — that name was ambiguous because
the cross-device mailbox now also has file transfer. This script ONLY targets
the Discord bridge at :1904; for cross-device peer-to-peer file transfer over
LAN/VPN see mailbox-attach.py.)

Agent-side CLI (runs on host, reads files from host filesystem, POSTs as
multipart to bridge :1904/agent-notify-file). Bridge then forwards via
Discord REST API. No container mount changes needed — bytes go through the
HTTP body.

Usage:
    py mailbox-discord-file.py --task "screenshot" --detail "..." \\
        --files C:/path/to/foo.png C:/path/to/bar.pdf

    py mailbox-discord-file.py --task "..." --files foo.png --channel <id> \\
        --agent wiki --status done

Discord file size limit: 25 MB / file (50 MB Nitro Basic, 500 MB Nitro).
Total payload also subject to Discord's overall per-message cap (~25 MB
default). Multi-file totals are summed.
"""
import argparse
import json
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--agent', default='wiki', help='instance name in the Discord [tag] (default wiki)')
    p.add_argument('--task', default='', help='short title shown on line 1')
    p.add_argument('--status', default='info', choices=['info', 'done', 'fail', 'warn'],
                   help='icon mapping (info=📋, done=✅, fail=❌, warn=⚠️)')
    p.add_argument('--detail', default='', help='body text shown after title')
    p.add_argument('--channel', default='',
                   help='Discord channel id; default uses bridge DISCORD_DEFAULT_CHANNEL '
                        '(trusted user DM)')
    p.add_argument('--files', nargs='+', required=True,
                   help='one or more file paths to attach (host filesystem)')
    p.add_argument('--url', default='http://localhost:1904/agent-notify-file',
                   help='bridge endpoint (default :1904 host port)')
    args = p.parse_args()

    # Read all files into memory + sanity-check sizes.
    parts = []
    total_bytes = 0
    for fp in args.files:
        path = Path(fp)
        if not path.exists():
            print(f"file not found: {fp}", file=sys.stderr)
            return 1
        data = path.read_bytes()
        parts.append((path.name, data, mimetypes.guess_type(path.name)[0]
                      or 'application/octet-stream'))
        total_bytes += len(data)
        print(f"  + {path.name}  ({len(data)} bytes)")

    if total_bytes > 25 * 1024 * 1024:
        print(f"WARNING: total {total_bytes / 1024 / 1024:.1f} MB exceeds Discord's 25 MB "
              f"default limit; will likely fail unless on Nitro", file=sys.stderr)

    # Build multipart body
    boundary = '----mailboxbridgecli' + os.urandom(8).hex()
    chunks = []
    payload = {
        'agent': args.agent,
        'task': args.task,
        'status': args.status,
        'detail': args.detail,
    }
    if args.channel:
        payload['channel'] = args.channel

    chunks.append(f'--{boundary}\r\n'.encode())
    chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    chunks.append(b'Content-Type: application/json; charset=utf-8\r\n\r\n')
    chunks.append(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    chunks.append(b'\r\n')

    for i, (filename, data, mime) in enumerate(parts):
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{filename}"\r\n'.encode('utf-8')
        )
        chunks.append(f'Content-Type: {mime}\r\n\r\n'.encode())
        chunks.append(data)
        chunks.append(b'\r\n')

    chunks.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(chunks)

    req = urllib.request.Request(
        args.url,
        data=body,
        method='POST',
        headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode('utf-8', 'replace')
            print(f"HTTP {resp.status}")
            print(text)
            return 0
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode('utf-8', 'replace')
        print(f"HTTP {e.code} error: {body_txt}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
