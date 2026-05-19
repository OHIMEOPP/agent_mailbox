"""Mailbox history dumper — read past messages from the shared SQLite DB.

Usage:
    py mailbox-dump.py [peer | agent] [--tail N] [--db PATH]

Examples:
    py mailbox-dump.py                    # all messages
    py mailbox-dump.py koatag             # all messages where koatag is sender or recipient
    py mailbox-dump.py koatag --tail 5    # last 5 messages with koatag
    py mailbox-dump.py --tail 20          # last 20 messages overall
    py mailbox-dump.py agent              # list all agent names that ever sent or received

Magic positional values (treated as commands, not peer names):
    agent / agents / peers / list  -> list all distinct agent names with stats
"""
import argparse
import io
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
sys.stderr = io.TextIOWrapper(
    sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

DB = r'C:\Users\User\.claude\mailbox\mailbox.db'

LIST_KEYWORDS = {'agent', 'agents', 'peers', 'list'}


def list_agents(db_path: str) -> int:
    """Print all distinct agent names with message counts and last-seen time."""
    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute(
            "SELECT from_name AS name, COUNT(*) AS sent, 0 AS recv, "
            "       MAX(sent_at) AS last_at "
            "FROM messages GROUP BY from_name "
            "UNION ALL "
            "SELECT to_name AS name, 0 AS sent, COUNT(*) AS recv, "
            "       MAX(sent_at) AS last_at "
            "FROM messages GROUP BY to_name"
        ))
        # collapse the two halves per name
        agg: dict[str, dict] = {}
        for name, sent, recv, last_at in rows:
            slot = agg.setdefault(name, {'sent': 0, 'recv': 0, 'last_at': ''})
            slot['sent'] += sent
            slot['recv'] += recv
            if last_at and last_at > slot['last_at']:
                slot['last_at'] = last_at

        # also include peers from the peers table (might never have sent/recv)
        peer_rows = list(conn.execute("SELECT name, last_seen_at FROM peers"))
    finally:
        conn.close()

    for name, last_seen in peer_rows:
        slot = agg.setdefault(name, {'sent': 0, 'recv': 0, 'last_at': ''})
        if last_seen and last_seen > slot['last_at']:
            slot['last_at'] = last_seen

    if not agg:
        print("[dump] no agents found")
        return 0

    names_sorted = sorted(agg.items(), key=lambda kv: kv[1]['last_at'], reverse=True)
    print(f"{'name':<20} {'sent':>5} {'recv':>5}  last_seen")
    print("-" * 60)
    for name, stat in names_sorted:
        print(f"{name:<20} {stat['sent']:>5} {stat['recv']:>5}  {stat['last_at']}")
    print(f"\n[dump] {len(agg)} agent(s)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Dump mailbox chat history")
    p.add_argument('peer', nargs='?', default=None,
                   help='only show messages where this peer is sender or recipient '
                        "(magic words 'agent' / 'agents' / 'peers' / 'list' "
                        "list all agent names instead)")
    p.add_argument('--tail', type=int, default=None,
                   help='only show last N messages (after peer filter)')
    p.add_argument('--db', default=DB, help='path to mailbox SQLite db')
    args = p.parse_args()

    if args.peer and args.peer.lower() in LIST_KEYWORDS:
        return list_agents(args.db)

    conn = sqlite3.connect(args.db)
    try:
        if args.peer:
            sql = ("SELECT id, from_name, to_name, sent_at, body "
                   "FROM messages WHERE from_name=? OR to_name=? "
                   "ORDER BY id")
            rows = list(conn.execute(sql, (args.peer, args.peer)))
        else:
            sql = ("SELECT id, from_name, to_name, sent_at, body "
                   "FROM messages ORDER BY id")
            rows = list(conn.execute(sql))
    finally:
        conn.close()

    if args.tail is not None and len(rows) > args.tail:
        rows = rows[-args.tail:]

    if not rows:
        scope = f"with peer '{args.peer}'" if args.peer else ""
        print(f"[dump] no messages {scope}".strip())
        return 0

    for mid, sender, recipient, sent, body in rows:
        print(f"\n[{mid}] {sent}  {sender} -> {recipient}")
        print(body)
        print("-" * 60)

    filter_desc = []
    if args.peer:
        filter_desc.append(f"peer={args.peer}")
    if args.tail is not None:
        filter_desc.append(f"tail={args.tail}")
    suffix = f" ({', '.join(filter_desc)})" if filter_desc else ""
    print(f"\n[dump] {len(rows)} message(s) shown{suffix}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
