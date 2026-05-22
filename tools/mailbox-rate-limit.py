"""Mailbox rate limit admin CLI.

Inspect the sliding-window rate limiter state: who's hitting the limit, what
recent traffic looks like, and override (reset) specific scopes when needed.

Usage:
    py mailbox-rate-limit.py --stats           # totals + limit + active scopes
    py mailbox-rate-limit.py --top             # top 20 scopes by recent count
    py mailbox-rate-limit.py --top --limit 50
    py mailbox-rate-limit.py --reset from:wiki # wipe a scope's buckets
    py mailbox-rate-limit.py --prune           # delete buckets > 1h old
    py mailbox-rate-limit.py --stats --json    # machine-readable

Default --db: ~/.claude/mailbox/mailbox.db.

Env vars affecting runtime (NOT the CLI):
    MAILBOX_RATE_LIMIT_DISABLED=1    daemon skips check
    MAILBOX_RATE_LIMIT_PER_MIN=120   override default limit
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mailbox import rate_limit as mailbox_rate_limit

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)

    action = p.add_mutually_exclusive_group()
    action.add_argument("--stats", action="store_true")
    action.add_argument("--top", action="store_true")
    action.add_argument("--reset", metavar="SCOPE_KEY",
                         help="wipe buckets for a specific scope_key (e.g. from:wiki)")
    action.add_argument("--prune", action="store_true",
                         help="delete buckets older than 1 hour")

    p.add_argument("--limit", type=int, default=20,
                   help="row limit for --top (default 20)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    # Default action = --stats
    if not any([args.stats, args.top, args.reset, args.prune]):
        args.stats = True

    if args.stats:
        s = mailbox_rate_limit.stats(args.db)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"db:                {args.db}")
            print(f"limit/min:         {s['rate_limit_limit_per_min']}")
            print(f"disabled:          {s['rate_limit_disabled']}")
            print(f"active scopes (5m): {s['rate_limit_active_scopes']}")
            print(f"buckets stored:    {s['rate_limit_buckets_total']}")
        return 0

    if args.top:
        rows = mailbox_rate_limit.top_scopes(args.db, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            if not rows:
                print("(no recent activity)")
                return 0
            print(f"top {len(rows)} scopes by 5-min recent count:")
            print(f"{'scope_key':<40} {'count':>7}  last_seen")
            for r in rows:
                print(f"{r['scope_key']:<40} {r['recent_count']:>7}  {r['last_seen']}")
        return 0

    if args.reset:
        n = mailbox_rate_limit.reset_scope(args.db, args.reset)
        if args.json:
            print(json.dumps({"reset": args.reset, "rows_deleted": n}))
        else:
            print(f"Reset scope {args.reset!r}: deleted {n} bucket(s)")
        return 0 if n else 2

    if args.prune:
        n = mailbox_rate_limit.prune_old_buckets(args.db)
        if args.json:
            print(json.dumps({"pruned": n}))
        else:
            print(f"Pruned {n} old bucket(s) (>1h)")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
