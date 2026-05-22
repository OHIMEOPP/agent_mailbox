"""Manual mailbox retention CLI.

Runs the same sweep logic as mailbox-server.py's daily daemon, but on demand.
Operates directly on the SQLite DB + attachments dir — no need for the server
to be running. SQLite's busy_timeout handles concurrency with a live server.

Usage:
    py mailbox-retention.py --stats             # show current numbers
    py mailbox-retention.py --dry-run           # report what would be deleted
    py mailbox-retention.py --once              # execute sweep with defaults
    py mailbox-retention.py --once --read-days 3 --unread-days 7  # override
    py mailbox-retention.py --once --db /custom/path/mailbox.db

Defaults: read=7d, unread=14d, peer=30d (matches server defaults).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mailbox import sweep as mailbox_sweep

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--attachments-dir", type=Path, default=None,
                   help="blob storage dir (default: <db parent>/attachments)")
    p.add_argument("--once", action="store_true",
                   help="execute sweep once and exit (writes!)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be deleted without writing")
    p.add_argument("--stats", action="store_true",
                   help="print observability snapshot and exit")
    p.add_argument("--read-days", type=int, default=mailbox_sweep.DEFAULT_READ_DAYS)
    p.add_argument("--unread-days", type=int, default=mailbox_sweep.DEFAULT_UNREAD_DAYS)
    p.add_argument("--peer-days", type=int, default=mailbox_sweep.DEFAULT_PEER_DAYS)
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human summary")
    args = p.parse_args()

    if args.attachments_dir is None:
        args.attachments_dir = args.db.parent / "attachments"

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    if not any([args.once, args.dry_run, args.stats]):
        print("nothing to do — pass --stats / --dry-run / --once "
              "(see --help)", file=sys.stderr)
        return 2

    if args.stats:
        s = mailbox_sweep.stats(args.db, args.attachments_dir)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"db:               {args.db}")
            print(f"attachments dir:  {args.attachments_dir}")
            print(f"messages total:   {s['message_count']}  (unread: {s['unread_count']})")
            print(f"attachments rows: {s['attachment_count']}")
            print(f"blobs on disk:    {s['blob_count']}  ({s['blob_total_bytes'] / 1024 / 1024:.2f} MB)")
            print(f"peers:            {s['peer_count']}")
            if s["oldest_message_age_days"] is not None:
                print(f"oldest message:   {s['oldest_message_age_days']} days old")
        return 0

    counters = mailbox_sweep.sweep_all(
        args.db, args.attachments_dir,
        read_days=args.read_days,
        unread_days=args.unread_days,
        peer_days=args.peer_days,
        dry_run=args.dry_run,
    )
    label = "DRY-RUN: would have " if args.dry_run else ""
    if args.json:
        print(json.dumps({"dry_run": args.dry_run, "counters": counters},
                         indent=2, ensure_ascii=False))
    else:
        print(f"[sweep] {label}{mailbox_sweep.format_summary(counters)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
