#!/usr/bin/env python3
"""SessionStart hook — auto-nudge the agent to start its mailbox watcher.

Why a nudge and not a spawned process:
    The watcher's value is *waking the agent* on new mail. That wake only
    works when the watcher is launched by the Monitor tool (stream-mode) so
    the harness wires each stdout line back into the agent loop. A hook can
    spawn a detached process, but its output never reaches the agent. So this
    hook does the next best thing: it resolves the project's mailbox identity,
    builds the exact Monitor command (hub or spoke), and injects it as
    SessionStart `additionalContext`. The agent then starts the watcher via
    Monitor — preserving the wake mechanism while automating the trigger.

Mailbox-enabled detection (only nudge when mailbox is explicitly configured —
never nudge in unrelated projects):
    1. CLAUDE_MAILBOX_NAME env set, OR
    2. a `.mailbox-name` file in the project root, OR
    3. a `mailbox` server declared in the project's `.mcp.json`.
    None of the above -> this project is not mailbox-enabled -> stay silent.

Never raise: a crashing SessionStart hook must not break the session, so the
whole body is guarded and any failure exits 0 with no context.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _project_dir(stdin: dict) -> str:
    return (
        (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
        or (stdin.get("cwd") or "").strip()
        or os.getcwd()
    )


def _read_first_line(path: Path) -> str | None:
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def _name_from_mcp(proj: Path) -> str | None:
    try:
        f = proj / ".mcp.json"
        if not f.is_file():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        srv = (data.get("mcpServers") or {}).get("mailbox") or {}
        name = ((srv.get("env") or {}).get("CLAUDE_MAILBOX_NAME") or "").strip()
        return name or None
    except (OSError, ValueError):
        return None


def _resolve_name(proj: Path) -> str | None:
    env_name = (os.environ.get("CLAUDE_MAILBOX_NAME") or "").strip()
    if env_name:
        return env_name
    file_name = _read_first_line(proj / ".mailbox-name")
    if file_name:
        return file_name
    return _name_from_mcp(proj)


def _resolve_watch_args(proj: Path) -> str:
    env_args = (os.environ.get("CLAUDE_MAILBOX_WATCH_ARGS") or "").strip()
    if env_args:
        return env_args
    return _read_first_line(proj / ".mailbox-watch-args") or ""


def main() -> int:
    plugin_root = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "CLAUDE_PLUGIN_ROOT", ""
    )
    stdin = _read_stdin_json()
    proj = Path(_project_dir(stdin))

    name = _resolve_name(proj)
    if not name:
        return 0  # not a mailbox-enabled project — stay silent

    watcher = str(Path(plugin_root) / "mailbox-watch.py") if plugin_root else "mailbox-watch.py"
    remote = (os.environ.get("CLAUDE_MAILBOX_REMOTE") or "").strip().rstrip("/")
    token = (os.environ.get("CLAUDE_MAILBOX_TOKEN") or "").strip()

    if remote:
        # Spoke mode: Monitor subprocess does NOT inherit .mcp.json env, so the
        # remote URL + token must be passed explicitly. --watch-all unsupported here.
        command = f'py "{watcher}" {name} --remote {remote}'
        if token:
            command += f" --token {token}"
        mode_note = f"（spoke mode → hub {remote}）"
    else:
        # Hub / local mode.
        watch_args = _resolve_watch_args(proj)
        command = f'py "{watcher}" {name} --monitor'
        if watch_args:
            command += f" {watch_args}"
        mode_note = "（hub 本機 mode）"

    context = (
        f"📬 Mailbox plugin（agent-mailbox）已啟用。本專案 mailbox 身分為 `{name}` {mode_note}。\n"
        f"若你**本 session 還沒**啟動 mailbox watcher，請立即用 **Monitor tool** 啟動"
        f"（persistent: true, timeout_ms: 3600000）：\n"
        f"    {command}\n"
        f"啟動後回報一句「mailbox watcher 已啟動」。已在本 session 啟動過則略過、勿重複起。\n"
        f"收信／回信／mark_read 等照 mailbox 既有流程；新訊息會經 Monitor stdout 喚醒你。"
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # SessionStart hook must never break the session.
        sys.exit(0)
