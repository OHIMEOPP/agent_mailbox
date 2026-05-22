"""Mailbox webhook admin CLI.

Manage outbound webhooks — endpoints that receive POSTs every time a new
mailbox message lands. The mailbox-server daemon delivers, this CLI is for
register / list / disable / forensics.

Usage:
    py mailbox-webhooks.py --list
    py mailbox-webhooks.py --add my-slack --url https://hooks.slack.com/...
    py mailbox-webhooks.py --add filter-only --url https://x.example.com \
        --to-glob 'koatag*' --from-glob 'wiki'
    py mailbox-webhooks.py --delete 3
    py mailbox-webhooks.py --activate 3
    py mailbox-webhooks.py --deactivate 3
    py mailbox-webhooks.py --tail-deliveries           # last 50, all
    py mailbox-webhooks.py --tail-deliveries --webhook 3 --status failed
    py mailbox-webhooks.py --stats
    py mailbox-webhooks.py --test 3                    # fire a synthetic event

Defaults: --db ~/.claude/mailbox/mailbox.db.

Receivers verify auth via the `X-Mailbox-Sig` header (HMAC-SHA256 of body).
See `mailbox_webhooks.verify_signature()` for the verifier helper.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_webhooks

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"


def _print_webhook_row(w: dict, show_secret: bool = False) -> None:
    print(f"  id={w['id']} name={w['name']} active={bool(w['active'])} "
          f"url={w['url']}")
    if show_secret:
        print(f"    secret: {w['secret_hmac']}")
    if w.get("filter_to_glob"):
        print(f"    to-glob:   {w['filter_to_glob']}")
    if w.get("filter_from_glob"):
        print(f"    from-glob: {w['filter_from_glob']}")
    print(f"    fires={w.get('total_fires', 0)} "
          f"last_fired={w.get('last_fired_at') or '(never)'}")
    if w.get("last_error"):
        print(f"    last_error: {w['last_error']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")

    actions = p.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true",
                          help="list registered webhooks (default action)")
    actions.add_argument("--add", metavar="NAME",
                          help="register a new webhook")
    actions.add_argument("--delete", type=int, metavar="ID",
                          help="delete webhook by id (cascades deliveries)")
    actions.add_argument("--activate", type=int, metavar="ID")
    actions.add_argument("--deactivate", type=int, metavar="ID")
    actions.add_argument("--tail-deliveries", action="store_true",
                          help="show recent delivery attempts")
    actions.add_argument("--stats", action="store_true")
    actions.add_argument("--test", type=int, metavar="ID",
                          help="run one delivery cycle right now (debugging)")

    # Add-specific flags
    p.add_argument("--url", help="endpoint URL (with --add)")
    p.add_argument("--to-glob", help="filter: only fire for messages whose to "
                                     "matches this fnmatch glob")
    p.add_argument("--from-glob", help="filter: only fire for messages whose "
                                       "from matches this fnmatch glob")
    p.add_argument("--secret", help="set the HMAC secret explicitly "
                                    "(default: auto-generated)")

    # tail-deliveries flags
    p.add_argument("--webhook", type=int, help="restrict tail to one webhook id")
    p.add_argument("--status",
                   choices=("pending", "success", "failed", "skipped"),
                   help="restrict tail to one status")
    p.add_argument("--limit", type=int, default=50)

    p.add_argument("--show-secret", action="store_true",
                   help="(on --list) include secret_hmac in output — dangerous, "
                        "default is masked")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human format")
    args = p.parse_args()

    if not args.db.exists():
        # init schema on first use so users don't have to bootstrap the table
        # manually before --add
        args.db.parent.mkdir(parents=True, exist_ok=True)
        # Touch / open writes a 0-byte file when first connecting — fine
    mailbox_webhooks.init_schema(args.db)

    # Default action = list when no action set
    no_action = not any([args.add, args.delete is not None,
                          args.activate is not None,
                          args.deactivate is not None,
                          args.tail_deliveries, args.stats,
                          args.test is not None])
    if no_action:
        args.list = True

    if args.add:
        if not args.url:
            print("--add requires --url", file=sys.stderr)
            return 2
        try:
            row = mailbox_webhooks.register(
                args.db, name=args.add, url=args.url,
                filter_to_glob=args.to_glob,
                filter_from_glob=args.from_glob,
                secret=args.secret,
            )
        except Exception as e:
            print(f"--add failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(row, indent=2, ensure_ascii=False))
        else:
            print(f"Registered webhook id={row['id']} name={row['name']}")
            print(f"  url:    {row['url']}")
            print(f"  secret: {row['secret_hmac']}")
            print(f"  ↑ store this secret somewhere — needed by your receiver "
                  "to verify HMAC signatures.")
        return 0

    if args.list:
        rows = mailbox_webhooks.list_webhooks(args.db, include_secret=args.show_secret)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            if not rows:
                print("(no webhooks registered)")
                return 0
            print(f"{len(rows)} webhook(s):")
            for w in rows:
                _print_webhook_row(w, show_secret=args.show_secret)
        return 0

    if args.delete is not None:
        n = mailbox_webhooks.delete(args.db, args.delete)
        if args.json:
            print(json.dumps({"deleted": n}))
        else:
            print(f"Deleted {n} webhook(s) (and cascaded deliveries)")
        return 0 if n else 2

    if args.activate is not None:
        n = mailbox_webhooks.set_active(args.db, args.activate, True)
        print(f"Activated {n}")
        return 0 if n else 2

    if args.deactivate is not None:
        n = mailbox_webhooks.set_active(args.db, args.deactivate, False)
        print(f"Deactivated {n}")
        return 0 if n else 2

    if args.tail_deliveries:
        rows = mailbox_webhooks.list_deliveries(
            args.db, webhook_id=args.webhook,
            status=args.status, limit=args.limit,
        )
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            if not rows:
                print("(no deliveries match)")
                return 0
            for d in rows:
                tag = ""
                if d["status"] == "failed":
                    tag = " [FAILED]"
                elif d["status"] == "skipped":
                    tag = " [SKIPPED]"
                elif d["status"] == "pending":
                    tag = " [pending]"
                print(f"id={d['id']:>4} webhook={d['webhook_id']:>3} "
                      f"msg={d['message_id']:>5} "
                      f"attempts={d['attempts']} "
                      f"code={d.get('response_code') or '-'} "
                      f"at={d.get('last_attempt_at') or '(not yet)'}{tag}")
        return 0

    if args.stats:
        s = mailbox_webhooks.stats(args.db)
        if args.json:
            print(json.dumps(s, indent=2, ensure_ascii=False))
        else:
            print(f"db:                 {args.db}")
            print(f"active webhooks:    {s['webhook_count']}")
            print(f"pending deliveries: {s['webhook_pending_deliveries']}")
            print(f"failed deliveries:  {s['webhook_failed_deliveries']}")
            print(f"last fired at:      {s['webhook_last_fired_at'] or '(none)'}")
        return 0

    if args.test is not None:
        # Run one tick. since_id=0 forces a re-scan of everything; in practice
        # the daemon would track the high-water mark itself.
        counters = mailbox_webhooks.deliver_pending(args.db, since_id=0)
        if args.json:
            print(json.dumps(counters, indent=2, ensure_ascii=False))
        else:
            print(f"[test] counters: {counters}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
