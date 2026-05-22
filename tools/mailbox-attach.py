"""Send a cross-device mailbox message with file attachment(s).

CLI counterpart of the `send(to=..., body=..., files=[...])` MCP tool. Posts
multipart/form-data to a mailbox-server :1905 instance (LAN / Tailscale / etc).

Use this when:
  - You're on the HUB and want to push a file/zip to a SPOKE agent's inbox.
  - You're on a SPOKE and want to push a file back to the HUB or another peer.
  - You want to script file transfer outside of an MCP-enabled Claude Code
    session (e.g. shell-only access on a server).

Not to confuse with mailbox-discord-file.py — that one notifies your Discord DM
via the bridge :1904. This one delivers into the agent mailbox so the recipient
peer's watcher fires a MAIL notification with attach=N.

Discord file size limit does NOT apply here — see MAX_SINGLE_FILE /
MAX_TOTAL_PAYLOAD on the server (default 100MB single / 500MB total).

Usage:
    py mailbox-attach.py --from wiki@DESKTOP-ABC --to wiki@LAPTOP-XYZ \\
        --body "see attached zip" --files C:/path/to/foo.zip

    py mailbox-attach.py --from wiki@hub --to koatag@hub \\
        --body "config snapshot" --files cfg.json other.txt \\
        --hub http://192.168.1.10:1905 --token <bearer-token>

If --hub / --token are omitted, falls back to CLAUDE_MAILBOX_REMOTE and
CLAUDE_MAILBOX_TOKEN env vars (same convention as mailbox-watch.py --remote).
"""
import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="sender", required=True,
                   help="your CLAUDE_MAILBOX_NAME (e.g. wiki@DESKTOP-ABC123)")
    p.add_argument("--to", required=True,
                   help="recipient CLAUDE_MAILBOX_NAME")
    p.add_argument("--body", default="",
                   help="message text (default empty)")
    p.add_argument("--files", nargs="+", required=True,
                   help="one or more file paths to attach")
    p.add_argument("--hub", default=None,
                   help="hub URL (e.g. http://192.168.1.10:1905). "
                        "Defaults to CLAUDE_MAILBOX_REMOTE env var.")
    p.add_argument("--token", default=None,
                   help="bearer token. Defaults to CLAUDE_MAILBOX_TOKEN env var.")
    args = p.parse_args()

    hub = (args.hub or os.environ.get("CLAUDE_MAILBOX_REMOTE", "")).strip().rstrip("/")
    token = (args.token or os.environ.get("CLAUDE_MAILBOX_TOKEN", "")).strip()
    if not hub:
        print("--hub or CLAUDE_MAILBOX_REMOTE required", file=sys.stderr)
        return 2
    if not token:
        print("--token or CLAUDE_MAILBOX_TOKEN required", file=sys.stderr)
        return 2

    # Read all files
    file_parts: list[tuple[str, str, bytes]] = []
    total = 0
    for fp in args.files:
        path = Path(fp)
        if not path.exists():
            print(f"file not found: {fp}", file=sys.stderr)
            return 1
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        file_parts.append((path.name, mime, data))
        total += len(data)
        print(f"  + {path.name}  ({len(data):,} bytes, {mime})")
    print(f"  total: {total:,} bytes ({total / 1024 / 1024:.1f} MB)")

    # Build multipart body
    boundary = "----mailboxattach" + os.urandom(8).hex()
    chunks: list[bytes] = []
    payload = {"from": args.sender, "to": args.to, "body": args.body}
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    chunks.append(b"Content-Type: application/json; charset=utf-8\r\n\r\n")
    chunks.append(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    chunks.append(b"\r\n")
    for i, (fname, mime, data) in enumerate(file_parts):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n'
            .encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    url = f"{hub}/send-file"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            text = resp.read().decode("utf-8", "replace")
            print(f"HTTP {resp.status}")
            try:
                obj = json.loads(text)
                print(json.dumps(obj, ensure_ascii=False, indent=2))
                if "id" in obj:
                    print(f"\nmessage id: {obj['id']}")
                    for a in obj.get("attachments", []):
                        print(f"  attachment id={a['id']} {a['filename']} "
                              f"({a['size']:,}B sha256={a['sha256'][:12]}...)")
            except json.JSONDecodeError:
                print(text)
            return 0
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        print(f"HTTP {e.code} error: {body_txt}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"hub unreachable: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
