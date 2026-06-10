"""Spoke onboarding wizard.

Sets up a new Claude Code session to act as a "spoke" — connects via
REST/SSE to an existing mailbox hub, without owning a local SQLite DB.

Usage:
    py bootstrap-spoke.py                          # interactive
    py bootstrap-spoke.py --hub http://192.168.1.10:1905 \
                          --token <bearer> \
                          --name wiki \
                          --project C:/dev/some-project

Steps performed:
    1. Validate hub reachability (curl /health)
    2. Validate token (curl /peers with Bearer auth)
    3. Compute <role>@<hostname> name if --name omitted
    4. Write .mcp.json to <project>/ (refuses if file exists unless --force)
    5. Print Monitor command for starting the watcher
    6. Optionally suggest restart command for Claude Code session

This is the "spoke happy-path" companion to SETUP-CROSS-DEVICE.md. Reading
the doc end-to-end is still recommended for production deployments.

Does NOT install anything (no pip, no system packages). User must have
Python 3.10+ + Claude Code + git already.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def color(s: str, code: str) -> str:
    """ANSI color if stdout is a TTY, else plain."""
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def green(s): return color(s, "32")
def red(s):   return color(s, "31")
def yellow(s): return color(s, "33")
def bold(s):  return color(s, "1")


def prompt(question: str, default: str = "") -> str:
    """Interactive prompt with optional default. Returns stripped input or default."""
    hint = f" [{default}]" if default else ""
    sys.stdout.write(f"{question}{hint}: ")
    sys.stdout.flush()
    val = sys.stdin.readline().rstrip("\r\n")
    return val.strip() or default


def hostname() -> str:
    """Cross-platform hostname for naming convention."""
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def probe_hub(hub_url: str, token: str) -> tuple[bool, str]:
    """Return (ok, message)."""
    hub_url = hub_url.rstrip("/")
    # /health (no auth)
    try:
        with urllib.request.urlopen(f"{hub_url}/health", timeout=5) as r:
            payload = json.loads(r.read().decode("utf-8"))
            if not payload.get("ok"):
                return False, f"hub /health returned ok=false: {payload}"
    except urllib.error.URLError as e:
        return False, f"hub /health unreachable: {e.reason}"
    except json.JSONDecodeError:
        return False, "hub /health returned non-JSON (old hub or proxy?)"

    # /peers (auth check)
    req = urllib.request.Request(f"{hub_url}/peers",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            peers = json.loads(r.read().decode("utf-8"))
            peer_names = [p["name"] for p in peers.get("peers", [])]
            return True, f"hub OK; {len(peer_names)} peers visible ({', '.join(peer_names[:5])}{'...' if len(peer_names) > 5 else ''})"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "401 — bearer token rejected by hub"
        return False, f"hub /peers HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"hub /peers unreachable: {e.reason}"


def write_mcp_json(project_dir: Path, name: str, hub_url: str, token: str,
                   force: bool) -> Path:
    target = project_dir / ".mcp.json"
    if target.exists() and not force:
        raise FileExistsError(
            f".mcp.json exists at {target} — pass --force to overwrite, "
            f"or back it up first"
        )
    # Detect repo path — server.py lives in claude-mailbox repo
    repo_default = Path("C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py")
    if not repo_default.exists():
        # Try sibling-of-this-script
        repo_default = Path(__file__).resolve().parent / "server.py"

    config = {
        "mcpServers": {
            "mailbox": {
                "command": "python",
                "args": [str(repo_default).replace("\\", "/")],
                "env": {
                    "CLAUDE_MAILBOX_NAME": name,
                    "CLAUDE_MAILBOX_REMOTE": hub_url.rstrip("/"),
                    "CLAUDE_MAILBOX_TOKEN": token,
                },
            }
        }
    }
    target.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return target


def main() -> int:
    p = argparse.ArgumentParser(
        description="Spoke onboarding wizard for cross-device mailbox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--hub", help="hub URL (e.g. http://192.168.1.10:1905)")
    p.add_argument("--token", help="bearer token (from hub's ~/.claude/mailbox/token.txt)")
    p.add_argument("--name", help="CLAUDE_MAILBOX_NAME for this spoke; "
                                  "default <role>@<hostname>")
    p.add_argument("--role", default="wiki",
                   help="role half of <role>@<hostname> when --name omitted "
                        "(default 'wiki')")
    p.add_argument("--project", type=Path, default=Path.cwd(),
                   help="project dir to write .mcp.json into (default: cwd)")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing .mcp.json")
    p.add_argument("--skip-probe", action="store_true",
                   help="don't validate hub connectivity (offline / dry-run)")
    args = p.parse_args()

    print(bold("🛰  Mailbox spoke bootstrap"))
    print(f"   project: {args.project}")
    print(f"   host:    {hostname()}")
    print()

    # Interactive fill-in for any missing required field
    hub = args.hub or prompt("Hub URL", "http://192.168.1.10:1905")
    token = args.token or prompt("Bearer token (from hub's token.txt)")
    if not token:
        print(red("ERR: token is required"), file=sys.stderr)
        return 2
    name = args.name or f"{args.role}@{hostname()}"
    print(f"   name:    {name}")

    # Probe hub
    if not args.skip_probe:
        print()
        print("⏳ Probing hub...")
        ok, msg = probe_hub(hub, token)
        if ok:
            print(f"   {green('✓')} {msg}")
        else:
            print(f"   {red('✗')} {msg}", file=sys.stderr)
            print()
            print(yellow("Fix the issue then rerun, or pass --skip-probe to "
                         "write .mcp.json regardless."),
                  file=sys.stderr)
            return 1

    # Write .mcp.json
    print()
    try:
        target = write_mcp_json(args.project, name, hub, token, args.force)
    except FileExistsError as e:
        print(red(f"ERR: {e}"), file=sys.stderr)
        return 3

    print(f"   {green('✓')} Wrote {target}")

    # Print next-step instructions
    print()
    print(bold("Next steps:"))
    print(f"  1. Restart Claude Code in {args.project} so MCP env loads")
    print(f"  2. After restart, in the new session start the watcher via Monitor tool:")
    print(f"     {green('command:')} py \"" +
          str(Path(__file__).resolve().parent / 'mailbox-watch.py').replace("\\", "/") +
          f"\" {name} --remote {hub.rstrip('/')} --token <TOKEN>")
    print(f"     {green('persistent:')} true")
    print(f"     {green('timeout_ms:')} 3600000")
    print(f"  3. mcp__plugin_agent-mailbox_mailbox__whoami() should return mode=remote, hub={hub.rstrip('/')}")
    print()
    print(bold("Verify with:"))
    print(f"     curl {hub.rstrip('/')}/health")
    print(f"     curl -H \"Authorization: Bearer <token>\" {hub.rstrip('/')}/peers")
    print()

    # Suggest gitignore check if project looks like a repo
    gitignore = args.project / ".gitignore"
    if gitignore.exists():
        ignored = ".mcp.json" in gitignore.read_text(encoding="utf-8", errors="replace")
        if not ignored:
            print(yellow(
                f"⚠ .mcp.json is NOT in {gitignore.name} — your bearer "
                f"token is now in the repo. Add `.mcp.json` to .gitignore "
                f"before committing."), file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
