"""Manual mailbox backup CLI.

Runs the same backup logic as mailbox-server.py's daily daemon, but on demand.
Operates directly on the SQLite DB + attachments dir — no need for the server
to be running. SQLite's online .backup() API is safe against a live writer.

Usage:
    py mailbox-backup.py --stats               # last_backup_at + count + bytes
    py mailbox-backup.py --list                # all snapshots, newest first
    py mailbox-backup.py --once                # take one backup + rolling prune
    py mailbox-backup.py --restore <ts>        # restore from a timestamp (needs --yes)
    py mailbox-backup.py --restore mailbox-backup-20260523-020000.db --yes
    py mailbox-backup.py --once --backup-dir /custom/path
    py mailbox-backup.py --once --db /custom/mailbox.db

Defaults: backup dir = ~/.claude/mailbox/backups/ (= <db parent>/backups, matches
server). Rolling retention = 7 daily / 4 weekly / 3 monthly (matches server).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import mailbox_backup

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"

# Accept either bare timestamp (20260523-020000) or full filename
# (mailbox-backup-20260523-020000.db / -attachments.tar.gz).
_TS_BARE = re.compile(r"^(\d{8}-\d{6})$")
_TS_FROM_NAME = re.compile(r"mailbox-backup-(\d{8}-\d{6})")


def _extract_timestamp(arg: str) -> str | None:
    if _TS_BARE.match(arg):
        return arg
    m = _TS_FROM_NAME.search(arg)
    if m:
        return m.group(1)
    return None


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    mb = n / 1024 / 1024
    if mb < 1:
        return f"{n / 1024:.1f}KB"
    return f"{mb:.2f}MB"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--attachments-dir", type=Path, default=None,
                   help="blob storage dir (default: <db parent>/attachments)")
    p.add_argument("--backup-dir", type=Path, default=None,
                   help="backup output dir (default: <db parent>/backups)")
    p.add_argument("--once", action="store_true",
                   help="take one backup + rolling prune (writes!)")
    p.add_argument("--list", action="store_true",
                   help="list existing backups, newest first")
    p.add_argument("--stats", action="store_true",
                   help="print last_backup_at / count / total bytes and exit")
    p.add_argument("--restore", metavar="TS_OR_FILENAME",
                   help="restore from this timestamp (overwrites live data; needs --yes)")
    p.add_argument("--yes", action="store_true",
                   help="skip interactive confirm for --restore")
    p.add_argument("--keep-daily", type=int, default=mailbox_backup.DEFAULT_KEEP_DAILY)
    p.add_argument("--keep-weekly", type=int, default=mailbox_backup.DEFAULT_KEEP_WEEKLY)
    p.add_argument("--keep-monthly", type=int, default=mailbox_backup.DEFAULT_KEEP_MONTHLY)
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human output")
    args = p.parse_args()

    if args.attachments_dir is None:
        args.attachments_dir = args.db.parent / "attachments"
    if args.backup_dir is None:
        args.backup_dir = args.db.parent / "backups"

    actions = [args.once, args.list, args.stats, bool(args.restore)]
    if not any(actions):
        print("nothing to do — pass --once / --list / --stats / --restore "
              "(see --help)", file=sys.stderr)
        return 2
    if sum(bool(a) for a in actions) > 1:
        print("pick exactly one of --once / --list / --stats / --restore",
              file=sys.stderr)
        return 2

    # --- --stats ---
    if args.stats:
        s = mailbox_backup.stats(args.backup_dir)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"backup dir:       {args.backup_dir}")
            print(f"last backup at:   {s['last_backup_at'] or '(none)'}")
            print(f"backup count:     {s['backup_count']}")
            print(f"backup total:     {_human_bytes(s['backup_total_bytes'])}")
        return 0

    # --- --list ---
    if args.list:
        items = mailbox_backup.list_backups(args.backup_dir)
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        else:
            if not items:
                print(f"(no backups in {args.backup_dir})")
                return 0
            print(f"backup dir: {args.backup_dir}")
            print(f"{'timestamp':<17} {'db':>10}  {'attachments':>12}  {'total':>10}")
            for i in items:
                print(
                    f"{i['timestamp']:<17} "
                    f"{_human_bytes(i.get('db_size', 0)):>10}  "
                    f"{_human_bytes(i.get('tar_size', 0)):>12}  "
                    f"{_human_bytes(i['total_size']):>10}"
                )
        return 0

    # --- --once ---
    if args.once:
        if not args.db.exists():
            print(f"db not found: {args.db}", file=sys.stderr)
            return 2
        counters = mailbox_backup.backup_once(
            args.db, args.attachments_dir, args.backup_dir,
            keep_daily=args.keep_daily,
            keep_weekly=args.keep_weekly,
            keep_monthly=args.keep_monthly,
        )
        if args.json:
            print(json.dumps(counters, indent=2, ensure_ascii=False))
        else:
            print(f"[backup] {mailbox_backup.format_summary(counters)}")
            print(f"  db:          {counters.get('db_backup_path')}")
            if counters.get("attachments_tar_path"):
                print(f"  attachments: {counters['attachments_tar_path']}")
            else:
                print("  attachments: (none — dir empty or missing)")
        return 0

    # --- --restore ---
    ts = _extract_timestamp(args.restore)
    if ts is None:
        print(f"invalid --restore arg: {args.restore!r} "
              "(expected YYYYMMDD-HHMMSS or mailbox-backup-...db filename)",
              file=sys.stderr)
        return 2

    if not args.yes:
        print(
            f"--restore is destructive. Will:\n"
            f"  1. move {args.db} → {args.db}.before-restore-<now>\n"
            f"  2. move {args.attachments_dir} → "
            f"{args.attachments_dir}.before-restore-<now>\n"
            f"  3. copy backup db {ts} into place\n"
            f"  4. extract attachments tar.gz into place (if exists)\n"
            f"\nRe-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2

    try:
        out = mailbox_backup.restore(
            args.backup_dir, args.db, args.attachments_dir,
            ts, confirm=True,
        )
    except FileNotFoundError as e:
        print(f"restore failed: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"restored from {ts}")
        print(f"  db:                 {out['restored_db']}")
        if out.get("pre_restore_db"):
            print(f"  pre-restore db:     {out['pre_restore_db']}")
        if out.get("tar_restored"):
            print(f"  attachments:        restored (tar extracted)")
            if out.get("pre_restore_attachments"):
                print(f"  pre-restore attach: {out['pre_restore_attachments']}")
        else:
            print("  attachments:        (no tar in backup — left untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
