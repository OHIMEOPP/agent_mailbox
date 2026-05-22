"""CLI for the scheduled-send queue.

Usage:
    py mailbox-scheduled.py --list                       # pending only
    py mailbox-scheduled.py --list --include-delivered   # full history
    py mailbox-scheduled.py --cancel <scheduled_id>      # mark as cancelled
    py mailbox-scheduled.py --stats                      # observability snapshot
    py mailbox-scheduled.py --deliver-now                # one-shot: fire any
                                                          # pending deliveries
                                                          # whose deliver_at has
                                                          # passed (manual flush)

The schedule daemon inside mailbox-server.py polls every 30s and does the same
thing as --deliver-now automatically. This CLI is for inspection + manual
intervention (e.g. cancel a queued reminder before it fires).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mailbox import scheduled as mailbox_scheduled  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--list", action="store_true", help="list scheduled rows")
    p.add_argument("--include-delivered", action="store_true",
                   help="include delivered + cancelled rows in --list")
    p.add_argument("--cancel", type=int, default=None,
                   help="mark scheduled_id as cancelled (refuses if already delivered)")
    p.add_argument("--stats", action="store_true", help="observability snapshot")
    p.add_argument("--deliver-now", action="store_true",
                   help="manually trigger one delivery pass (same as daemon tick)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args()

    if not any([args.list, args.cancel is not None, args.stats, args.deliver_now]):
        print("nothing to do — pass --list / --cancel ID / --stats / --deliver-now",
              file=sys.stderr)
        return 2

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    if args.stats:
        s = mailbox_scheduled.stats(args.db)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"scheduled queue:")
            print(f"  pending:   {s['scheduled_pending']}")
            print(f"  delivered: {s['scheduled_delivered']}")
            print(f"  cancelled: {s['scheduled_cancelled']}")
            print(f"  next:      {s['next_deliver_at'] or '(none pending)'}")
        return 0

    if args.list:
        rows = mailbox_scheduled.list_pending(args.db,
                                              include_delivered=args.include_delivered)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            if not rows:
                print("(no scheduled rows)")
            else:
                for r in rows:
                    status = "pending"
                    if r["delivered_msg_id"] is not None:
                        status = f"delivered → msg #{r['delivered_msg_id']}"
                    elif r["cancelled_at"] is not None:
                        status = f"cancelled @ {r['cancelled_at']}"
                    body_preview = (r["body"] or "")[:60].replace("\n", " | ")
                    print(f"#{r['id']:<5} {status:<32} deliver_at={r['deliver_at']}")
                    print(f"        {r['from_name']} → {r['to_name']}: {body_preview}")
        return 0

    if args.cancel is not None:
        result = mailbox_scheduled.cancel(args.db, args.cancel)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if result["ok"]:
                print(f"cancelled scheduled_id={args.cancel}")
            else:
                print(f"could not cancel: {result.get('error')}", file=sys.stderr)
                if "delivered_msg_id" in result:
                    print(f"  already delivered as msg #{result['delivered_msg_id']}",
                          file=sys.stderr)
                return 1
        return 0

    if args.deliver_now:
        c = mailbox_scheduled.deliver_pending(args.db)
        if args.json:
            print(json.dumps(c, indent=2, ensure_ascii=False))
        else:
            print(f"[scheduled] {mailbox_scheduled.format_summary(c)}")
            if c["delivered_ids"]:
                print(f"  delivered msg ids: {c['delivered_ids']}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
