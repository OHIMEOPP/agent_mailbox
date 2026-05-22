"""Mailbox history dumper — read past messages from the shared SQLite DB.

Usage:
    py mailbox-dump.py [peer | agent] [--tail N] [--tree] [--db PATH]

Examples:
    py mailbox-dump.py                    # all messages
    py mailbox-dump.py koatag             # all messages where koatag is sender or recipient
    py mailbox-dump.py koatag --tail 5    # last 5 messages with koatag
    py mailbox-dump.py --tail 20          # last 20 messages overall
    py mailbox-dump.py --tail 20 --tree   # last 20, render as reply tree
    py mailbox-dump.py agent              # list all agent names that ever sent or received

Magic positional values (treated as commands, not peer names):
    agent / agents / peers / list  -> list all distinct agent names with stats

Tree mode:
    --tree groups messages by in_reply_to parent chain. Roots are messages
    with no parent (or whose parent is outside the filtered set). Children
    nest under their parent with box-drawing characters. Broken-chain
    children (parent retention-pruned) are flagged as orphans at root level.
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


def _has_in_reply_to(conn: sqlite3.Connection) -> bool:
    """Whether the messages table has in_reply_to column (post-2026-05-23)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    return "in_reply_to" in cols


def _fetch_audit_for_msgs(conn: sqlite3.Connection, msg_ids: list[int]) -> list:
    """Return audit_log rows referencing any of the given message ids.

    Matches either:
      - audit_log.target = str(msg_id)   (some actions stamp the id directly)
      - JSON LIKE '%"msg_id": <id>%' or '%"scheduled_id": <id>%' (payload-embedded)

    Returns ordered by ts ASC. Empty if audit_log table absent (pre-2026-05-23
    schema) or msg_ids empty.
    """
    if not msg_ids or not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone():
        return []
    # Build OR conditions
    target_in = ",".join("?" * len(msg_ids))
    target_args = [str(i) for i in msg_ids]
    payload_like = " OR ".join(
        ['payload_json LIKE ?' for _ in msg_ids]
    )
    payload_args = [f'%"msg_id": {i}%' for i in msg_ids]
    sql = (
        f"SELECT ts, actor, action, target, payload_json, ok FROM audit_log "
        f"WHERE target IN ({target_in}) OR {payload_like} "
        f"ORDER BY ts ASC, id ASC"
    )
    return list(conn.execute(sql, target_args + payload_args).fetchall())


def _render_audit_trail(rows: list) -> None:
    if not rows:
        return
    print()
    print(f"== Audit trail ({len(rows)} entries) ==")
    for r in rows:
        ts, actor, action, target, payload_json, ok = r
        ok_marker = "" if ok else "  ✗"
        target_str = f" → {target}" if target else ""
        print(f"  📜 {ts}  {actor:<20} {action:<14}{target_str}{ok_marker}")


def _fetch_scheduled_pending(conn: sqlite3.Connection, peer: str | None) -> list:
    """Return pending (not delivered, not cancelled) scheduled_messages rows.
    Optionally filter by peer (sender OR recipient).
    Empty list if the table doesn't exist (pre-scheduled-send schema)."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_messages'"
    ).fetchone():
        return []
    sql = ("SELECT id, from_name, to_name, body, deliver_at, in_reply_to, expires_at "
           "FROM scheduled_messages "
           "WHERE delivered_msg_id IS NULL AND cancelled_at IS NULL")
    params: list = []
    if peer:
        sql += " AND (from_name=? OR to_name=?)"
        params.extend([peer, peer])
    sql += " ORDER BY deliver_at ASC"
    return list(conn.execute(sql, params).fetchall())


def _render_scheduled_pending(rows: list) -> None:
    if not rows:
        return
    print()
    print(f"== Pending scheduled deliveries ({len(rows)}) ==")
    for r in rows:
        sched_id, from_name, to_name, body, deliver_at, in_reply_to, expires_at = r
        preview = (body or "").replace("\r", " ").replace("\n", " | ")[:80]
        meta = f"deliver_at={deliver_at}"
        if in_reply_to is not None:
            meta += f" re=#{in_reply_to}"
        if expires_at is not None:
            meta += f" expires={expires_at}"
        print(f"  ⏳ [sched:{sched_id}] {from_name} → {to_name}  {meta}")
        print(f"        {preview}")


def _fetch_reactions(conn: sqlite3.Connection, msg_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """Return {message_id: [(emoji, actor), ...]} for the given message ids.
    Returns empty dict if reactions table doesn't exist (pre-2026-05-23 DB)."""
    if not msg_ids:
        return {}
    # Probe table existence
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reactions'"
    ).fetchone():
        return {}
    placeholders = ",".join("?" * len(msg_ids))
    out: dict[int, list[tuple[str, str]]] = {}
    for mid, emoji, actor in conn.execute(
        f"SELECT message_id, emoji, actor FROM reactions "
        f"WHERE message_id IN ({placeholders}) "
        f"ORDER BY message_id, created_at",
        msg_ids,
    ).fetchall():
        out.setdefault(mid, []).append((emoji, actor))
    return out


def _format_reactions(rxns: list[tuple[str, str]]) -> str:
    """Format reactions list as "❤ alice,bob  👍 carol" (grouped by emoji)."""
    if not rxns:
        return ""
    by_emoji: dict[str, list[str]] = {}
    for emoji, actor in rxns:
        by_emoji.setdefault(emoji, []).append(actor)
    parts = []
    for emoji, actors in by_emoji.items():
        parts.append(f"{emoji} {','.join(actors)}")
    return "  ".join(parts)


def _render_flat(rows: list, reactions_by_id: dict | None = None) -> None:
    """Original flat per-message rendering, with optional reactions footer."""
    reactions_by_id = reactions_by_id or {}
    for r in rows:
        mid, sender, recipient, sent, body = r[0], r[1], r[2], r[3], r[4]
        ref_suffix = ""
        # rows may include in_reply_to as r[5] if tree-aware fetch was used
        if len(r) > 5 and r[5] is not None:
            ref_suffix = f"  (re: #{r[5]})"
        print(f"\n[{mid}] {sent}  {sender} -> {recipient}{ref_suffix}")
        print(body)
        rxns = reactions_by_id.get(mid, [])
        if rxns:
            print(f"  💬 {_format_reactions(rxns)}")
        print("-" * 60)


def _render_tree(rows: list, reactions_by_id: dict | None = None) -> None:
    """Tree rendering using in_reply_to chains, with reactions footer per node.

    Layout:
      [root_id] ts  sender -> recipient
          body...
          💬 ❤ alice,bob  👍 carol
          ├─[child_id] ts  sender -> recipient  (re: #root_id)
          │     body...
          │     └─[grandchild_id] ...
          └─[sibling_id] ...

    Rows must include in_reply_to as column index 5.
    """
    reactions_by_id = reactions_by_id or {}
    by_id = {r[0]: r for r in rows}
    children: dict[int, list[int]] = {}
    roots: list[int] = []

    for r in rows:
        mid = r[0]
        parent = r[5] if len(r) > 5 else None
        if parent is None or parent not in by_id:
            roots.append(mid)
            if parent is not None and parent not in by_id:
                # orphan — parent was pruned or outside filter
                pass
        else:
            children.setdefault(parent, []).append(mid)

    # Sort children by id (chronological)
    for parent_id in children:
        children[parent_id].sort()

    def _print_node(node_id: int, prefix: str, is_last: bool, is_root: bool) -> None:
        r = by_id[node_id]
        mid, sender, recipient, sent, body = r[0], r[1], r[2], r[3], r[4]
        parent = r[5] if len(r) > 5 else None
        # Determine connector
        if is_root:
            connector = ""
            child_prefix = ""
        else:
            connector = "└─" if is_last else "├─"
            child_prefix = "   " if is_last else "│  "

        # Orphan flag — parent referenced but not in dataset
        orphan_flag = ""
        if parent is not None and parent not in by_id:
            orphan_flag = f"  ⚠ broken-chain (parent #{parent} not in view)"
        elif parent is not None:
            orphan_flag = f"  (re: #{parent})"

        # Header line
        print(f"{prefix}{connector}[{mid}] {sent}  {sender} -> {recipient}{orphan_flag}")
        # Body lines indented under header
        body_indent = f"{prefix}{child_prefix}  " if not is_root else "  "
        for line in body.splitlines() or [""]:
            print(f"{body_indent}{line}")
        # Reactions footer (if any)
        rxns = reactions_by_id.get(mid, [])
        if rxns:
            print(f"{body_indent}💬 {_format_reactions(rxns)}")

        # Recurse into children
        kids = children.get(node_id, [])
        for i, kid in enumerate(kids):
            kid_is_last = (i == len(kids) - 1)
            kid_prefix = prefix + child_prefix
            _print_node(kid, kid_prefix, kid_is_last, is_root=False)

    for i, root_id in enumerate(roots):
        _print_node(root_id, "", is_last=(i == len(roots) - 1), is_root=True)
        print("-" * 60)


def main() -> int:
    p = argparse.ArgumentParser(description="Dump mailbox chat history")
    p.add_argument('peer', nargs='?', default=None,
                   help='only show messages where this peer is sender or recipient '
                        "(magic words 'agent' / 'agents' / 'peers' / 'list' "
                        "list all agent names instead)")
    p.add_argument('--tail', type=int, default=None,
                   help='only show last N messages (after peer filter)')
    p.add_argument('--tree', action='store_true',
                   help='render as reply tree using in_reply_to chains')
    p.add_argument('--include-scheduled', action='store_true',
                   help='append a "Pending scheduled deliveries" section listing '
                        'scheduled_messages rows not yet delivered (peer filter applies)')
    p.add_argument('--audit-trail', action='store_true',
                   help='append a "Audit trail" section showing audit_log entries '
                        'referencing any of the rendered message ids '
                        '(send / inbox / react / mark_read / etc.)')
    p.add_argument('--db', default=DB, help='path to mailbox SQLite db')
    args = p.parse_args()

    if args.peer and args.peer.lower() in LIST_KEYWORDS:
        return list_agents(args.db)

    conn = sqlite3.connect(args.db)
    try:
        tree_capable = _has_in_reply_to(conn)
        col_list = "id, from_name, to_name, sent_at, body"
        if tree_capable:
            col_list += ", in_reply_to"
        if args.peer:
            sql = (f"SELECT {col_list} "
                   "FROM messages WHERE from_name=? OR to_name=? "
                   "ORDER BY id")
            rows = list(conn.execute(sql, (args.peer, args.peer)))
        else:
            sql = (f"SELECT {col_list} FROM messages ORDER BY id")
            rows = list(conn.execute(sql))
    finally:
        conn.close()

    if args.tail is not None and len(rows) > args.tail:
        rows = rows[-args.tail:]

    if not rows:
        scope = f"with peer '{args.peer}'" if args.peer else ""
        print(f"[dump] no messages {scope}".strip())
        return 0

    # Fetch reactions for the rendered set (may be empty if reactions table absent)
    conn2 = sqlite3.connect(args.db)
    try:
        reactions_by_id = _fetch_reactions(conn2, [r[0] for r in rows])
    finally:
        conn2.close()

    if args.tree:
        if not tree_capable:
            print("[dump] --tree requires in_reply_to column (DB is pre-2026-05-23 schema)",
                  file=sys.stderr)
            return 2
        _render_tree(rows, reactions_by_id)
    else:
        _render_flat(rows, reactions_by_id)

    # Optional scheduled-pending footer
    if args.include_scheduled:
        conn3 = sqlite3.connect(args.db)
        try:
            sched_rows = _fetch_scheduled_pending(conn3, args.peer)
        finally:
            conn3.close()
        _render_scheduled_pending(sched_rows)

    # Optional audit-trail footer
    if args.audit_trail:
        conn4 = sqlite3.connect(args.db)
        try:
            audit_rows = _fetch_audit_for_msgs(conn4, [r[0] for r in rows])
        finally:
            conn4.close()
        _render_audit_trail(audit_rows)

    filter_desc = []
    if args.peer:
        filter_desc.append(f"peer={args.peer}")
    if args.tail is not None:
        filter_desc.append(f"tail={args.tail}")
    if args.tree:
        filter_desc.append("tree")
    if args.include_scheduled:
        filter_desc.append("+scheduled")
    if args.audit_trail:
        filter_desc.append("+audit")
    suffix = f" ({', '.join(filter_desc)})" if filter_desc else ""
    print(f"\n[dump] {len(rows)} message(s) shown{suffix}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
