"""Mailbox digest — actionable summary for inbox-zero workflow.

Different from `tools/mailbox-stats.py` (broad activity statistics).
This focuses on "what should I act on right now?":

- Unread by sender (top contributors to your inbox load)
- Unread messages with high priority (priority >= N)
- TTL expiring within window (act before they auto-prune)
- Threads with replies you haven't read (conversations expecting follow-up)
- Most-reacted-to messages (signal that others find them important)
- Claimable backlog vs already-claimed (worker visibility)

Inspired by 2026 agentic email trends — Gmail / Outlook test AI agents
summarizing inbox threads. Same pattern for agent-to-agent: surface
what's actionable instead of forcing the agent to scan ~hundreds of
messages every session start.

Usage:
    py tools/mailbox-digest.py                       # for current $CLAUDE_MAILBOX_NAME
    py tools/mailbox-digest.py --peer wiki           # for arbitrary peer
    py tools/mailbox-digest.py --peer wiki --since 24h
    py tools/mailbox-digest.py --peer wiki --json
    py tools/mailbox-digest.py --peer wiki --threshold-priority 5

Read-only (PRAGMA query_only). Safe with concurrent mailbox-server writes.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"
_RELATIVE_RE = re.compile(r"^(\d+)([smhd])$")


def parse_since(spec: str | None) -> str | None:
    """Convert '24h' / '7d' / '30m' to SQLite relative modifier."""
    if spec is None or not spec.strip():
        return None
    s = spec.strip()
    m = _RELATIVE_RE.match(s)
    if not m:
        return s  # assume ISO
    n = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
    return f"-{seconds} seconds"


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone())


def _col_exists(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def collect_digest(db: Path, peer: str, since: str | None,
                   threshold_priority: int) -> dict:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")

    cutoff: str | None
    if since:
        if since.startswith("-") and "second" in since:
            cutoff = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (since,),
            ).fetchone()[0]
        else:
            cutoff = since
    else:
        cutoff = None

    has_priority = _col_exists(conn, "messages", "priority")
    has_expires = _col_exists(conn, "messages", "expires_at")
    has_claim = _col_exists(conn, "messages", "claimed_by")
    has_in_reply_to = _col_exists(conn, "messages", "in_reply_to")
    has_reactions_table = _table_exists(conn, "reactions")

    out: dict = {
        "peer": peer,
        "since": since,
        "as_of": conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()[0],
    }

    # ---- Unread by sender ----
    base_filter = " AND sent_at >= ?" if cutoff else ""
    args: list = [peer]
    if cutoff:
        args.append(cutoff)
    out["unread_by_sender"] = [dict(r) for r in conn.execute(
        f"SELECT from_name AS sender, COUNT(*) AS count, "
        f"COALESCE(SUM(LENGTH(body)), 0) AS body_bytes, "
        f"MAX(sent_at) AS latest "
        f"FROM messages WHERE to_name=? AND read_at IS NULL{base_filter} "
        f"GROUP BY from_name ORDER BY count DESC LIMIT 10",
        args,
    ).fetchall()]

    # ---- Unread by priority bucket ----
    if has_priority:
        rows = conn.execute(
            f"SELECT priority, COUNT(*) AS count "
            f"FROM messages WHERE to_name=? AND read_at IS NULL{base_filter} "
            f"GROUP BY priority ORDER BY priority DESC",
            args,
        ).fetchall()
        out["unread_by_priority"] = [dict(r) for r in rows]

        # High-priority actionable list
        hp_args = [peer, threshold_priority]
        if cutoff:
            hp_args.append(cutoff)
        out["high_priority_unread"] = [dict(r) for r in conn.execute(
            f"SELECT id, from_name, sent_at, priority, "
            f"substr(body, 1, 100) AS preview "
            f"FROM messages WHERE to_name=? AND read_at IS NULL AND priority >= ?{base_filter} "
            f"ORDER BY priority DESC, sent_at DESC LIMIT 20",
            hp_args,
        ).fetchall()]

    # ---- TTL expiring within 24h ----
    if has_expires:
        out["ttl_expiring_24h"] = [dict(r) for r in conn.execute(
            "SELECT id, from_name, sent_at, expires_at, "
            "substr(body, 1, 100) AS preview "
            "FROM messages WHERE to_name=? AND read_at IS NULL "
            "AND expires_at IS NOT NULL "
            "AND expires_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ','now', '+24 hours') "
            "ORDER BY expires_at ASC LIMIT 20",
            (peer,),
        ).fetchall()]

    # ---- Claimable backlog (worker pattern) ----
    if has_claim:
        out["claim_status"] = dict(conn.execute(
            "SELECT "
            "  SUM(CASE WHEN claimed_by IS NULL OR claimed_until < "
            "    strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 1 ELSE 0 END) AS claimable, "
            "  SUM(CASE WHEN claimed_by=? AND claimed_until >= "
            "    strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 1 ELSE 0 END) AS held_by_you, "
            "  SUM(CASE WHEN claimed_by IS NOT NULL AND claimed_by != ? "
            "    AND claimed_until >= strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "    THEN 1 ELSE 0 END) AS held_by_others "
            "FROM messages WHERE to_name=? AND read_at IS NULL",
            (peer, peer, peer),
        ).fetchone())

    # ---- Reply-thread chains involving you ----
    if has_in_reply_to:
        out["unread_replies_to_you"] = [dict(r) for r in conn.execute(
            "SELECT m.id, m.from_name, m.in_reply_to, m.sent_at, "
            "substr(m.body, 1, 80) AS preview, "
            "p.body AS parent_preview "
            "FROM messages m LEFT JOIN messages p ON p.id = m.in_reply_to "
            "WHERE m.to_name=? AND m.read_at IS NULL "
            "AND m.in_reply_to IS NOT NULL "
            "AND (p.from_name = ? OR p.to_name = ?) "
            "ORDER BY m.sent_at DESC LIMIT 10",
            (peer, peer, peer),
        ).fetchall()]

    # ---- Most-reacted-to messages in your inbox ----
    if has_reactions_table:
        out["most_reacted"] = [dict(r) for r in conn.execute(
            "SELECT m.id, m.from_name, m.sent_at, "
            "substr(m.body, 1, 80) AS preview, "
            "COUNT(r.id) AS reaction_count "
            "FROM messages m JOIN reactions r ON r.message_id = m.id "
            "WHERE m.to_name=? "
            "GROUP BY m.id ORDER BY reaction_count DESC LIMIT 5",
            (peer,),
        ).fetchall()]

    # ---- Totals ----
    out["totals"] = dict(conn.execute(
        "SELECT COUNT(*) AS unread, "
        "COUNT(DISTINCT from_name) AS distinct_senders "
        "FROM messages WHERE to_name=? AND read_at IS NULL",
        (peer,),
    ).fetchone())

    conn.close()
    return out


def render_text(d: dict) -> str:
    lines = []
    lines.append(f"📨 mailbox digest for {d['peer']}")
    if d["since"]:
        lines.append(f"   filter: --since={d['since']}")
    lines.append(f"   as of:  {d['as_of']}")
    t = d["totals"]
    lines.append(f"   total unread: {t['unread']} from {t['distinct_senders']} senders")
    lines.append("")

    if d.get("unread_by_sender"):
        lines.append("== Top unread by sender ==")
        for r in d["unread_by_sender"]:
            kb = r["body_bytes"] / 1024
            lines.append(f"  {r['sender']:<30} {r['count']:>4} unread  "
                         f"{kb:>6.1f}KB  last={r['latest']}")
        lines.append("")

    if d.get("unread_by_priority"):
        lines.append("== Unread by priority bucket ==")
        for r in d["unread_by_priority"]:
            label = "URGENT" if r["priority"] >= 7 else (
                "high" if r["priority"] >= 4 else (
                    "normal" if r["priority"] == 0 else "low"))
            lines.append(f"  P{r['priority']} ({label:<7}) {r['count']:>4}")
        lines.append("")

    if d.get("high_priority_unread"):
        lines.append(f"== High-priority unread (P>={d.get('threshold_priority', 'N')}) ==")
        for r in d["high_priority_unread"]:
            preview = (r["preview"] or "").replace("\n", " | ")[:80]
            lines.append(f"  P{r['priority']} #{r['id']:<5} {r['from_name']:<20} "
                         f"{r['sent_at'][:19]}  {preview}")
        lines.append("")

    if d.get("ttl_expiring_24h"):
        lines.append("== TTL expiring within 24h (act before auto-prune) ==")
        for r in d["ttl_expiring_24h"]:
            preview = (r["preview"] or "").replace("\n", " | ")[:60]
            lines.append(f"  #{r['id']:<5} expires={r['expires_at']}  "
                         f"{r['from_name']:<20}  {preview}")
        lines.append("")

    if d.get("claim_status"):
        c = d["claim_status"]
        lines.append("== Worker-claim status (unread inbox) ==")
        lines.append(f"  claimable (free or expired): {c.get('claimable') or 0}")
        lines.append(f"  held by you:                 {c.get('held_by_you') or 0}")
        lines.append(f"  held by other agents:        {c.get('held_by_others') or 0}")
        lines.append("")

    if d.get("unread_replies_to_you"):
        lines.append("== Unread replies referring to your messages ==")
        for r in d["unread_replies_to_you"]:
            preview = (r["preview"] or "").replace("\n", " | ")[:60]
            parent = (r["parent_preview"] or "")[:40].replace("\n", " | ")
            lines.append(f"  #{r['id']} from={r['from_name']} re=#{r['in_reply_to']}")
            lines.append(f"    └ parent: \"{parent}...\"")
            lines.append(f"      reply:  \"{preview}\"")
        lines.append("")

    if d.get("most_reacted"):
        lines.append("== Most-reacted-to messages in your inbox ==")
        for r in d["most_reacted"]:
            preview = (r["preview"] or "").replace("\n", " | ")[:60]
            lines.append(f"  #{r['id']:<5} reactions={r['reaction_count']:<3} "
                         f"{r['from_name']:<20}  {preview}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Mailbox actionable digest")
    p.add_argument("--peer", default=os.environ.get("CLAUDE_MAILBOX_NAME"),
                   help="peer whose inbox to summarize "
                        "(default: $CLAUDE_MAILBOX_NAME)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--since", default=None,
                   help="window for 'unread by sender' / priority queries "
                        "(30m / 24h / 7d / ISO 8601)")
    p.add_argument("--threshold-priority", type=int, default=3,
                   help="show messages with priority >= this in 'high priority' "
                        "section (default 3)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of text")
    args = p.parse_args()

    if not args.peer:
        print("error: --peer required (or set $CLAUDE_MAILBOX_NAME)", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    since = parse_since(args.since)
    d = collect_digest(args.db, args.peer, since, args.threshold_priority)
    d["threshold_priority"] = args.threshold_priority

    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
    else:
        print(render_text(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
