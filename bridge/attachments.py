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


def fetch_and_blob(atts_dir: Path, discord_atts: list[dict],
                   log_prefix: str = "") -> list[dict]:
    """Download + blob-store all Discord attachments. NO DB I/O — the caller
    INSERTs the rows once the parent message_id is known. Designed so the
    DB write transaction stays short: network downloads + filesystem blob
    writes happen BEFORE the sqlite connection opens.

    Picks `url` (Discord's original CDN, works for every content_type) over
    `proxy_url` (Discord's media proxy, image-only — returns 415 for
    xlsx/pdf/etc). proxy_url is kept only as a last-resort fallback.

    Args:
        atts_dir: <db_parent>/attachments — content-addressed blob root.
        discord_atts: list of {filename, url, proxy_url, content_type, size}.
        log_prefix: optional tag for stdout lines (e.g. "msg-pending").

    Returns:
        list of {filename, mime, size, sha256} for blobs successfully on disk.
        Failed downloads are logged + skipped (best-effort relay).
    """
    stored = []
    for att in discord_atts:
        filename = att.get("filename") or f"attachment-{att.get('id', 'unknown')}"
        # `url` works for every attachment type. `proxy_url` is image-only
        # (Discord's media proxy 415s on xlsx/pdf/etc), so it's the fallback.
        candidates = [u for u in (att.get("url"), att.get("proxy_url")) if u]
        if not candidates:
            sys.stdout.write(f"[attach] {log_prefix}skip {filename}: no url\n")
            continue
        data = None
        last_err = None
        for src in candidates:
            try:
                data = _download(src)
                break
            except RuntimeError as e:
                last_err = e
                continue
        if data is None:
            sys.stdout.write(f"[attach] {log_prefix}skip {filename}: {last_err}\n")
            continue
        sha, size = _write_blob(data, atts_dir)
        mime = att.get("content_type") or "application/octet-stream"
        stored.append({
            "filename": filename, "mime": mime,
            "size": size, "sha256": sha,
        })
        sys.stdout.write(f"[attach] {log_prefix}stored {filename} "
                         f"({size}B sha={sha[:8]}…)\n")
    return stored


def insert_attachment_rows(conn, msg_id: int, stored: list[dict]) -> list[dict]:
    """Quick DB INSERT pass. Returns stored items with `id` populated."""
    out = []
    for s in stored:
        cur = conn.execute(
            "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_id, s["filename"], s["mime"], s["size"], s["sha256"]),
        )
        out.append({"id": cur.lastrowid, **s})
    return out
