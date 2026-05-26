"""Inbound Discord attachment relay — download from Discord CDN, write to
mailbox content-addressed blob store, INSERT attachment rows.

Mirrors server.py:_write_blob layout (<dir>/<sha[:2]>/<sha>) so reads via
mailbox MCP download() / mailbox-server.py /attachment/<id> work transparently.

Designed to be called from inbound.process_discord_inbound after the parent
message row is inserted, within the same sqlite connection (so the INSERTs
land in one transaction).
"""
import hashlib
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_PER_FILE_BYTES = 100 * 1024 * 1024  # 100 MB, matches mailbox server cap
DOWNLOAD_TIMEOUT_SECONDS = 30


def attachments_dir_for(db_path: str) -> Path:
    """Mirror server.py: ATTACHMENTS_DIR = DB_PATH.parent / "attachments"."""
    return Path(db_path).parent / "attachments"


def _download(url: str, max_bytes: int = MAX_PER_FILE_BYTES) -> bytes:
    """GET a Discord CDN URL and return bytes, capped at max_bytes.

    Raises RuntimeError on oversize / network failure so caller can log + skip.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "mailbox-bridge/1"})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as r:
            # Streamed read with cap; stop+raise once we exceed max_bytes
            buf = bytearray()
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise RuntimeError(
                        f"oversize: >{max_bytes} bytes from {url[:80]}…")
            return bytes(buf)
    except (urllib.error.URLError, urllib.error.HTTPError,
            ssl.SSLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise RuntimeError(f"download_fail: {type(e).__name__}: {e}") from e


def _write_blob(data: bytes, atts_dir: Path) -> tuple[str, int]:
    """Content-addressed atomic write. Returns (sha256, size). Idempotent —
    re-writing the same bytes is a no-op (dedup via sha-keyed path).
    """
    sha = hashlib.sha256(data).hexdigest()
    target = atts_dir / sha[:2] / sha
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return sha, len(data)


def relay_discord_attachments(conn, msg_id: int, atts_dir: Path,
                              discord_atts: list[dict]) -> list[dict]:
    """For each Discord attachment dict, download + store blob + INSERT row.

    Args:
        conn: open sqlite3 Connection (caller owns commit/rollback)
        msg_id: parent messages.id
        atts_dir: <db_parent>/attachments
        discord_atts: list of {filename, url, proxy_url, content_type, size}
                      (the subset of Discord's attachment object we care about)

    Returns:
        list of {id, filename, mime, size, sha256} for successfully stored
        attachments. Failed ones are logged + skipped (best-effort relay —
        partial success > rejecting the whole DM).
    """
    stored = []
    for att in discord_atts:
        filename = att.get("filename") or f"attachment-{att.get('id', 'unknown')}"
        # proxy_url is Discord's CDN-accelerated mirror, preferred over url
        src = att.get("proxy_url") or att.get("url")
        if not src:
            sys.stdout.write(f"[attach] msg #{msg_id} skip {filename}: no url\n")
            continue
        try:
            data = _download(src)
        except RuntimeError as e:
            sys.stdout.write(f"[attach] msg #{msg_id} skip {filename}: {e}\n")
            continue
        sha, size = _write_blob(data, atts_dir)
        mime = att.get("content_type") or "application/octet-stream"
        cur = conn.execute(
            "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_id, filename, mime, size, sha),
        )
        att_id = cur.lastrowid
        stored.append({
            "id": att_id, "filename": filename, "mime": mime,
            "size": size, "sha256": sha,
        })
        sys.stdout.write(f"[attach] msg #{msg_id} stored {filename} "
                         f"({size}B sha={sha[:8]}…)\n")
    return stored
