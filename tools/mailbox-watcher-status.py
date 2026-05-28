"""mailbox-watcher-status — on-demand snapshot of watcher heartbeat state.

The daemon side (mailbox-server.py + mailbox.watcher_monitor) pages Discord
on state transitions; this CLI is the manual `is everything alive right
now?` query, intended for cron-free machines, debugging, or for use by
mailbox-doctor.

Usage:
    py tools/mailbox-watcher-status.py
    py tools/mailbox-watcher-status.py --json
    py tools/mailbox-watcher-status.py --bridge http://other-host:1904/healthz
    py tools/mailbox-watcher-status.py --db /path/mailbox.db
    py tools/mailbox-watcher-status.py --strict  # exit 1 if any DEAD

Read-only. Doesn't write to the DB; doesn't trigger /agent-notify.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from mailbox import watcher_monitor as wm  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"


def _print_table(states: dict) -> None:
    if not states:
        print("No tracked watchers.")
        return
    by_kind: dict[str, list] = {}
    for key, s in sorted(states.items()):
        by_kind.setdefault(s.get("kind", "?"), []).append((key, s))
    for kind, items in sorted(by_kind.items()):
        print(f"\n[{kind}]")
        for _key, s in items:
            name = s.get("name") or _key
            status = s.get("status", wm.UNKNOWN)
            icon = wm.ICON.get(status, "?")
            age = s.get("age_seconds")
            age_str = f"  age={age:.0f}s" if age is not None else ""
            extra = s.get("detail") or ""
            extra_str = f"  ({extra})" if extra else ""
            print(f"  {icon} {name:32s} {status}{age_str}{extra_str}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Snapshot mailbox watcher heartbeat status.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"mailbox SQLite path (default {DEFAULT_DB})")
    p.add_argument("--bridge",
                   default=wm.DEFAULT_BRIDGE_HEALTH_URL,
                   help="bridge /healthz URL "
                        f"(default {wm.DEFAULT_BRIDGE_HEALTH_URL})")
    p.add_argument("--dead-threshold", type=int,
                   default=wm.DEFAULT_DEAD_THRESHOLD_SECONDS,
                   help="seconds-stale = DEAD "
                        f"(default {wm.DEFAULT_DEAD_THRESHOLD_SECONDS})")
    p.add_argument("--track-window-hours", type=float,
                   default=wm.DEFAULT_TRACK_WINDOW_HOURS,
                   help="only track peers active within this window "
                        f"(default {wm.DEFAULT_TRACK_WINDOW_HOURS}h)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any tracked target is DEAD")
    args = p.parse_args()

    # Localhost bridge URL is the default; CLI uses 127.0.0.1 instead of the
    # container DNS name when run on host.
    bridge_url = args.bridge
    if bridge_url == wm.DEFAULT_BRIDGE_HEALTH_URL:
        bridge_url = "http://127.0.0.1:1904/healthz"

    states = wm.status_snapshot(
        args.db, bridge_url=bridge_url,
        dead_threshold=args.dead_threshold,
        track_window_hours=args.track_window_hours,
    )

    if args.json:
        print(json.dumps(states, ensure_ascii=False, indent=2))
    else:
        _print_table(states)

    if args.strict:
        for s in states.values():
            if s.get("status") == wm.DEAD:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
