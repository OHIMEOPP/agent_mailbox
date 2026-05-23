"""mailbox-folder-sync — watch a folder, push new files to hub /send-file.

Used to give a remote phone (via the spoke relay UI) live access to files
produced by a local app (e.g. ComfyUI's output/ dir). New files are detected
by polling, hashed, deduped against a state file, and uploaded in batches.

Usage:
  py mailbox-folder-sync.py --folder C:/path/to/watch --label "ComfyUI/output"

Args:
  --folder         absolute path to watch (required)
  --to             recipient mailbox name (default: wiki)
  --label          message body used as group title on spoke /list
                   (default: folder.name)
  --hub-url        hub URL (default: http://127.0.0.1:1905)
  --token-file     path to file containing hub bearer token
                   (default: %USERPROFILE%/.claude/mailbox/token.txt)
  --token          inline token (overrides --token-file)
  --interval       poll interval seconds (default: 5)
  --stable-secs    file size must be unchanged for this long before
                   we consider it safe to upload (default: 3)
  --state-file     json file tracking sent sha256s
                   (default: %USERPROFILE%/.claude/mailbox/sync-state/<folder>.json)
  --from-name      sender name in mailbox (default: folder-sync@<hostname>)
  --batch-size     max files per /send-file call (default: 8)
  --include-existing
                   on first run, also upload files already in the folder.
                   Default behaviour is to catalogue them as "already sent"
                   without uploading, so only NEW files trigger uploads.
  --log-file       additionally append events to this file (default: none)

Behaviour notes:
  * Polling, not inotify/ReadDirectoryChangesW — keeps deps to stdlib only
    and lets the daemon survive temporary folder unavailability.
  * Files are deduped by sha256 across runs via the state file. Manual cleanup
    of the state file = re-upload everything still present.
  * Subdirectories are NOT walked (intentional: ComfyUI use case is flat).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _log(msg: str, fh=sys.stderr, also_to: pathlib.Path | None = None) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=fh, flush=True)
    if also_to is not None:
        try:
            with also_to.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _wait_for_stable(path: pathlib.Path, stable_secs: int, max_wait: float = 60.0) -> int | None:
    """Wait until path's size has been unchanged for stable_secs.
    Returns final size, or None if file disappeared / never stabilised."""
    deadline = time.monotonic() + max_wait
    last_size = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            sz = path.stat().st_size
        except FileNotFoundError:
            return None
        if sz != last_size:
            last_size = sz
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= stable_secs:
            return sz
        time.sleep(0.5)
    return None


def _send_files(hub_url: str, token: str, payload: dict, files: list[tuple[str, bytes, str]],
                timeout: float = 300.0) -> tuple[int, dict]:
    """POST multipart/form-data to hub /send-file.

    files: list of (filename, bytes, mime_type). Field names are files[0]..files[N]
    per the hub spec in mailbox-server.py:_handle_send_file.
    """
    boundary = "----mailbox-folder-sync-" + os.urandom(12).hex()
    parts: list[bytes] = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n".encode("utf-8")
    )
    parts.append(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    for i, (filename, data, mime) in enumerate(files):
        safe_name = filename.replace('"', "_")
        parts.append(
            f'\r\n--{boundary}\r\n'
            f'Content-Disposition: form-data; name="files[{i}]"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        )
        parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{hub_url.rstrip('/')}/send-file",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"error": f"HTTP {e.code}"}
        return e.code, err_body


def _load_state(state_path: pathlib.Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"sent_sha256": []}


def _save_state(state_path: pathlib.Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    token_path = pathlib.Path(args.token_file)
    if not token_path.exists():
        sys.exit(f"token file not found: {token_path} (use --token or --token-file)")
    return token_path.read_text(encoding="utf-8").strip()


def _default_state_path(folder: pathlib.Path) -> pathlib.Path:
    home = pathlib.Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    safe = folder.name.replace(" ", "_") or "root"
    return home / ".claude" / "mailbox" / "sync-state" / f"{safe}.json"


def main() -> int:
    home = pathlib.Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    default_token = home / ".claude" / "mailbox" / "token.txt"

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--folder", required=True)
    ap.add_argument("--to", default="wiki")
    ap.add_argument("--label", default=None)
    ap.add_argument("--hub-url", default="http://127.0.0.1:1905")
    ap.add_argument("--token-file", default=str(default_token))
    ap.add_argument("--token", default=None)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--stable-secs", type=int, default=3)
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--from-name", default=None)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--include-existing", action="store_true")
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args()

    folder = pathlib.Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f"not a directory: {folder}")

    token = _resolve_token(args)
    label = args.label or folder.name
    from_name = args.from_name or f"folder-sync@{socket.gethostname()}"
    state_path = pathlib.Path(args.state_file) if args.state_file else _default_state_path(folder)
    log_file = pathlib.Path(args.log_file) if args.log_file else None
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    state = _load_state(state_path)
    sent: set[str] = set(state.get("sent_sha256", []))

    # Fast-skip cache populated from (name, size) of files known to be sent.
    # Iteration cost stays O(1) per file: stat + dict lookup. Sha + stable-wait
    # only kicks in for files we haven't seen before.
    fast_seen: dict[tuple[str, int], str] = {}

    is_first_run = not state_path.exists()

    # Always walk the folder at startup. On first run AND when not opted in to
    # uploading existing files, register every current file as "sent" so the
    # daemon's main loop only uploads new arrivals. On subsequent runs, just
    # prime fast_seen so we don't pay the stable-wait + hash cost for files
    # the previous run already shipped.
    if is_first_run and not args.include_existing:
        _log(f"first run: cataloguing existing files (will NOT upload — use --include-existing to override)",
             also_to=log_file)
    else:
        _log(f"priming fast-skip cache from folder", also_to=log_file)
    primed = 0
    for p in folder.iterdir():
        if not p.is_file():
            continue
        try:
            sha = _sha256_of(p)
            sz = p.stat().st_size
        except OSError as e:
            _log(f"  skip {p.name}: {e}", also_to=log_file)
            continue
        if is_first_run and not args.include_existing:
            sent.add(sha)
        if sha in sent:
            fast_seen[(p.name, sz)] = sha
        primed += 1
    if is_first_run and not args.include_existing:
        state["sent_sha256"] = sorted(sent)
        _save_state(state_path, state)
        _log(f"catalogued {primed} existing files into state file", also_to=log_file)
    else:
        _log(f"primed {len(fast_seen)} entries from {primed} files (rest will go through send path)",
             also_to=log_file)

    _log(
        f"watching {folder} → {args.hub_url} → to={args.to} "
        f"(label={label!r}, interval={args.interval}s, stable={args.stable_secs}s, "
        f"batch≤{args.batch_size}, state={state_path})",
        also_to=log_file,
    )

    while True:
        try:
            try:
                children = sorted(folder.iterdir())
            except FileNotFoundError:
                _log(f"folder disappeared, waiting...", also_to=log_file)
                time.sleep(args.interval)
                continue

            pending: list[tuple[pathlib.Path, str, int]] = []
            for p in children:
                if not p.is_file():
                    continue
                if len(pending) >= args.batch_size:
                    break
                # Fast skip: (name, current_size) already in cache → either
                # we've sent this exact content before, or the size matches
                # what we saw last poll. Cheap stat-only check, no sha, no
                # stable-wait. Stale entries get refreshed if size changes
                # (file grew / replaced) since the cache key would miss.
                try:
                    quick_sz = p.stat().st_size
                except OSError:
                    continue
                if (p.name, quick_sz) in fast_seen:
                    continue
                # Slow path: wait until size stops changing, then hash + dedup.
                final_size = _wait_for_stable(p, args.stable_secs)
                if final_size is None:
                    continue
                try:
                    sha = _sha256_of(p)
                except OSError as e:
                    _log(f"  hash failed {p.name}: {e}", also_to=log_file)
                    continue
                if sha in sent:
                    # Content already sent under a different filename.
                    # Cache by current (name, size) so we don't re-hash next loop.
                    fast_seen[(p.name, final_size)] = sha
                    continue
                pending.append((p, sha, final_size))

            if pending:
                files: list[tuple[str, bytes, str]] = []
                for p, _sha, _sz in pending:
                    try:
                        data = p.read_bytes()
                    except OSError as e:
                        _log(f"  read failed {p.name}: {e}", also_to=log_file)
                        continue
                    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    files.append((p.name, data, mime))

                if files:
                    payload = {
                        "from": from_name,
                        "to": args.to,
                        "body": f"{label} (auto-sync, {len(files)} 檔)",
                    }
                    status, resp = _send_files(args.hub_url, token, payload, files)
                    if status == 200 and isinstance(resp, dict) and resp.get("id"):
                        # Persist sent shas only after server confirms.
                        for pp, sha, sz in pending:
                            sent.add(sha)
                            fast_seen[(pp.name, sz)] = sha
                        state["sent_sha256"] = sorted(sent)
                        _save_state(state_path, state)
                        names_preview = ", ".join(f.name for f, _, _ in [(p, s, z) for p, s, z in pending][:4])
                        _log(
                            f"sent msg #{resp['id']} — {len(files)} file(s): {names_preview}"
                            + (" …" if len(pending) > 4 else ""),
                            also_to=log_file,
                        )
                    else:
                        _log(f"send failed status={status}: {resp}", also_to=log_file)

            time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("interrupted, bye", also_to=log_file)
            return 0
        except Exception as e:
            _log(f"loop error: {e!r}", also_to=log_file)
            time.sleep(min(args.interval * 2, 30))


if __name__ == "__main__":
    sys.exit(main() or 0)
