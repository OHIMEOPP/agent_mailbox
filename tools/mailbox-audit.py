"""Mailbox audit log CLI.

Tail, filter, and inspect the passive audit log produced by mailbox-server
REST endpoints and the per-instance MCP server. Operates directly on the
SQLite DB — no need for the server to be running.

Usage:
    py mailbox-audit.py --tail                       # last 50 entries
    py mailbox-audit.py --tail --limit 200           # bigger window
    py mailbox-audit.py --since 1h                   # last hour
    py mailbox-audit.py --since 2026-05-23T00:00:00Z # absolute lower bound
    py mailbox-audit.py --actor wiki                 # only events from this actor
    py mailbox-audit.py --action send                # only send events
    py mailbox-audit.py --actor wiki --action send --since 1h
    py mailbox-audit.py --stats                      # count + first/last + by-action
    py mailbox-audit.py --json                       # machine-readable

Relative `--since` shortcuts: `15m`, `1h`, `24h`, `7d`. Anything else is treated
as an ISO timestamp.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mailbox import audit as mailbox_audit

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"

_RELATIVE = re.compile(r"^(\d+)([mhd])$")


def _resolve_since(arg: str) -> str:
    """Parse `--since`. Accept ISO or relative like `1h` / `30m` / `7d`."""
    m = _RELATIVE.match(arg)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "m":
            delta = timedelta(minutes=n)
        elif unit == "h":
            delta = timedelta(hours=n)
        elif unit == "d":
            delta = timedelta(days=n)
        else:  # unreachable per regex
            raise ValueError(unit)
        ts = datetime.now(timezone.utc) - delta
        return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"
    # Assume ISO — pass through; SQLite text-compares ISO strings cleanly.
    return arg


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--tail", action="store_true",
                   help="print most recent audit rows (default action)")
    p.add_argument("--stats", action="store_true",
                   help="print count + first/last + by-action breakdown")
    p.add_argument("--since",
                   help="lower bound — ISO ts or relative (e.g. 1h, 30m, 7d)")
    p.add_argument("--until",
                   help="upper bound (ISO ts only)")
    p.add_argument("--actor", help="filter by exact actor name")
    p.add_argument("--action", help="filter by action (send/inbox/mark_read/download/whoami/peers)")
    p.add_argument("--limit", type=int, default=mailbox_audit.DEFAULT_TAIL_LIMIT,
                   help=f"max rows (default {mailbox_audit.DEFAULT_TAIL_LIMIT})")
    p.add_argument("--asc", action="store_true",
                   help="oldest first (default: newest first)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human format")
    args = p.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    if args.action and args.action not in mailbox_audit.ACTIONS:
        print(f"unknown --action: {args.action!r} "
              f"(valid: {sorted(mailbox_audit.ACTIONS)})", file=sys.stderr)
        return 2

    # If neither --tail nor --stats explicitly set, default to --tail
    if not args.stats:
        args.tail = True

    if args.stats:
        s = mailbox_audit.stats(args.db)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"db:               {args.db}")
            print(f"audit_count:      {s['audit_count']}")
            print(f"first_at:         {s['audit_first_at'] or '(none)'}")
            print(f"last_at:          {s['audit_last_at'] or '(none)'}")
            if s["by_action"]:
                print("by_action:")
                for action, count in sorted(s["by_action"].items(),
                                             key=lambda x: -x[1]):
                    print(f"  {action:<12} {count}")
        return 0

    since = _resolve_since(args.since) if args.since else None
    rows = mailbox_audit.query_audit(
        args.db,
        since=since,
        until=args.until,
        actor=args.actor,
        action=args.action,
        limit=args.limit,
        order_desc=not args.asc,
    )

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        if not rows:
            print("(no audit rows matching filters)")
            return 0
        print(mailbox_audit.format_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
