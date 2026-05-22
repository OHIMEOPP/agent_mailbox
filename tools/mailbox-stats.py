"""Read-only stats / activity report for mailbox.db.

Usage:
    py mailbox-stats.py                        # full report
    py mailbox-stats.py --since 24h            # filter to last 24 hours
    py mailbox-stats.py --json                 # machine-readable
    py mailbox-stats.py --db /path/mailbox.db  # custom DB

Sections:
    - Overview: message_count / unread / attachment_count / blob_count / blob_total_bytes
    - Top senders (by message count + total body bytes + last activity)
    - Top recipients (by inbox + unread split)
    - Hour-of-day histogram (when messages get sent)
    - Per-peer unread breakdown
    - Oldest unread message age
    - FTS5 index size (if FTS5 enabled)
    - Feature-table counts: audit_log / webhooks / reactions (if tables exist)
    - Threading stats: thread roots / orphan replies
    - TTL stats: messages with expires_at / expired-pending-sweep

Pure read — never modifies DB. Concurrent with mailbox-server.py is safe.
"""
from __future__ import annotations

import argparse
import io
import json
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
    """Convert '24h' / '7d' / '30m' / '60s' / ISO to ISO cutoff string."""
    if spec is None or not spec.strip():
        return None
    s = spec.strip()
    m = _RELATIVE_RE.match(s)
    if not m:
        return s  # assume ISO
    n = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
    # Use SQLite modifier — convert to compatible form
    return f"-{seconds} seconds"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone()
    return r is not None


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def collect_stats(db: Path, since: str | None) -> dict:
    """Run all read queries; return a dict the renderer can format."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")  # belt + braces against accidental writes
    out: dict = {"db_path": str(db), "since": since}

    # since filter for sent_at — pass to queries as a parameter list
    if since:
        # since is either ISO or sqlite modifier ('-X seconds')
        if since.startswith("-") and "second" in since:
            cutoff = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (since,),
            ).fetchone()[0]
        else:
            cutoff = since
    else:
        cutoff = None

    def where_since(prefix: str = "") -> tuple[str, list]:
        if cutoff:
            return f" AND {prefix}sent_at >= ?", [cutoff]
        return "", []

    # --- Overview ---
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    unread = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE read_at IS NULL"
    ).fetchone()[0]
    if cutoff:
        total_in_window = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sent_at >= ?", (cutoff,)
        ).fetchone()[0]
    else:
        total_in_window = total

    out["overview"] = {
        "message_count": total,
        "unread_count": unread,
        "in_window": total_in_window,
    }

    if _table_exists(conn, "attachments"):
        att_count = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        unique_blobs = conn.execute(
            "SELECT COUNT(DISTINCT sha256) FROM attachments"
        ).fetchone()[0]
        total_attach_bytes = conn.execute(
            "SELECT COALESCE(SUM(size), 0) FROM attachments"
        ).fetchone()[0]
        out["overview"]["attachment_rows"] = att_count
        out["overview"]["unique_blobs"] = unique_blobs
        out["overview"]["attachment_bytes_logical"] = total_attach_bytes

    # --- Top senders ---
    extra_sql, extra_args = where_since("")
    top_senders = conn.execute(
        f"SELECT from_name AS name, COUNT(*) AS sent, "
        f"COALESCE(SUM(LENGTH(body)), 0) AS body_bytes, "
        f"MAX(sent_at) AS last_at "
        f"FROM messages WHERE 1=1{extra_sql} "
        f"GROUP BY from_name ORDER BY sent DESC LIMIT 10",
        extra_args,
    ).fetchall()
    out["top_senders"] = [dict(r) for r in top_senders]

    # --- Top recipients (by total + unread split) ---
    top_recip = conn.execute(
        f"SELECT to_name AS name, COUNT(*) AS received, "
        f"COALESCE(SUM(read_at IS NULL), 0) AS unread, "
        f"MAX(sent_at) AS last_at "
        f"FROM messages WHERE 1=1{extra_sql} "
        f"GROUP BY to_name ORDER BY received DESC LIMIT 10",
        extra_args,
    ).fetchall()
    out["top_recipients"] = [dict(r) for r in top_recip]

    # --- Hour-of-day histogram ---
    hours = conn.execute(
        f"SELECT CAST(strftime('%H', sent_at) AS INTEGER) AS hr, COUNT(*) AS n "
        f"FROM messages WHERE 1=1{extra_sql} "
        f"GROUP BY hr ORDER BY hr",
        extra_args,
    ).fetchall()
    hist = {r["hr"]: r["n"] for r in hours}
    out["hour_histogram"] = [{"hour": h, "count": hist.get(h, 0)} for h in range(24)]

    # --- Per-peer unread breakdown ---
    unread_breakdown = conn.execute(
        "SELECT to_name AS name, COUNT(*) AS unread "
        "FROM messages WHERE read_at IS NULL "
        "GROUP BY to_name ORDER BY unread DESC"
    ).fetchall()
    out["unread_by_peer"] = [dict(r) for r in unread_breakdown]

    # --- Oldest unread + oldest in window ---
    oldest_unread = conn.execute(
        "SELECT id, from_name, to_name, sent_at, "
        "(julianday('now') - julianday(sent_at)) AS age_days "
        "FROM messages WHERE read_at IS NULL "
        "ORDER BY sent_at ASC LIMIT 1"
    ).fetchone()
    out["oldest_unread"] = dict(oldest_unread) if oldest_unread else None

    # --- Threading stats ---
    if _col_exists(conn, "messages", "in_reply_to"):
        roots = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE in_reply_to IS NULL"
        ).fetchone()[0]
        replies = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE in_reply_to IS NOT NULL"
        ).fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM messages m "
            "WHERE m.in_reply_to IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.id = m.in_reply_to)"
        ).fetchone()[0]
        out["threading"] = {
            "roots": roots,
            "replies": replies,
            "orphan_replies": orphans,
        }

    # --- TTL stats ---
    if _col_exists(conn, "messages", "expires_at"):
        with_ttl = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE expires_at IS NOT NULL"
        ).fetchone()[0]
        expired_pending = conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE expires_at IS NOT NULL AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
        expiring_24h = conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE expires_at IS NOT NULL "
            "AND expires_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+24 hours')"
        ).fetchone()[0]
        out["ttl"] = {
            "with_ttl": with_ttl,
            "expired_pending_sweep": expired_pending,
            "expiring_24h": expiring_24h,
        }

    # --- FTS5 index size ---
    if _table_exists(conn, "messages_fts"):
        try:
            fts_size = conn.execute(
                "SELECT COUNT(*) FROM messages_fts"
            ).fetchone()[0]
            out["fts5"] = {"indexed_rows": fts_size}
        except sqlite3.OperationalError:
            pass

    # --- Audit / webhook / reactions counts (if tables exist) ---
    for table, key in [("audit_log", "audit"), ("webhooks", "webhooks"),
                        ("webhook_deliveries", "webhook_deliveries"),
                        ("reactions", "reactions")]:
        if _table_exists(conn, table):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            out[key] = {"count": n}

    if _table_exists(conn, "peers"):
        peer_count = conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM peers WHERE last_seen_at >= "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days')"
        ).fetchone()[0]
        out["peers"] = {"total": peer_count, "active_7d": active}

    conn.close()
    return out


def render_text(s: dict) -> str:
    lines: list[str] = []
    lines.append(f"📬 mailbox stats — {s['db_path']}")
    if s["since"]:
        lines.append(f"   filter: --since={s['since']}")
    lines.append("")

    o = s["overview"]
    lines.append("== Overview ==")
    lines.append(f"  messages:    {o['message_count']} total  ({o['unread_count']} unread)")
    if s.get("since"):
        lines.append(f"  in window:   {o['in_window']}")
    if "attachment_rows" in o:
        mb = o["attachment_bytes_logical"] / 1024 / 1024
        lines.append(f"  attachments: {o['attachment_rows']} rows  "
                     f"{o['unique_blobs']} unique blobs  {mb:.2f} MB logical")

    if s.get("peers"):
        p = s["peers"]
        lines.append(f"  peers:       {p['total']} known  ({p['active_7d']} active in last 7d)")

    if s.get("threading"):
        t = s["threading"]
        lines.append(f"  threading:   {t['roots']} roots, {t['replies']} replies, "
                     f"{t['orphan_replies']} orphan")

    if s.get("ttl"):
        t = s["ttl"]
        lines.append(f"  TTL:         {t['with_ttl']} with expires_at, "
                     f"{t['expired_pending_sweep']} expired-pending, "
                     f"{t['expiring_24h']} expiring 24h")

    if s.get("fts5"):
        lines.append(f"  FTS5 index:  {s['fts5']['indexed_rows']} rows")

    for key, label in [("audit", "audit_log"), ("webhooks", "webhooks"),
                       ("webhook_deliveries", "webhook_deliveries"),
                       ("reactions", "reactions")]:
        if s.get(key):
            lines.append(f"  {label:12s} {s[key]['count']} rows")

    # Top senders
    lines.append("")
    lines.append("== Top senders ==")
    lines.append(f"  {'name':<30} {'sent':>6} {'bytes':>10}   last activity")
    for r in s["top_senders"][:10]:
        kb = r["body_bytes"] / 1024
        lines.append(f"  {r['name']:<30} {r['sent']:>6} {kb:>8.1f}KB   {r['last_at']}")

    # Top recipients
    lines.append("")
    lines.append("== Top recipients ==")
    lines.append(f"  {'name':<30} {'recv':>6} {'unread':>7}   last activity")
    for r in s["top_recipients"][:10]:
        lines.append(f"  {r['name']:<30} {r['received']:>6} {r['unread']:>7}   {r['last_at']}")

    # Per-peer unread
    if s["unread_by_peer"]:
        lines.append("")
        lines.append("== Unread by peer ==")
        for r in s["unread_by_peer"][:10]:
            lines.append(f"  {r['name']:<30} {r['unread']:>6}")

    # Hour-of-day histogram
    hist = s["hour_histogram"]
    max_n = max((h["count"] for h in hist), default=0)
    if max_n > 0:
        lines.append("")
        lines.append("== Hour-of-day (UTC) ==")
        bar_w = 40
        for h in hist:
            if h["count"] == 0:
                continue
            bar = "█" * int(round(h["count"] / max_n * bar_w))
            lines.append(f"  {h['hour']:02d}h {bar} {h['count']}")

    # Oldest unread
    ou = s.get("oldest_unread")
    if ou:
        lines.append("")
        lines.append(f"== Oldest unread ==")
        lines.append(f"  id={ou['id']}  {ou['from_name']} -> {ou['to_name']}  "
                     f"({ou['age_days']:.2f} days old)")
        lines.append(f"  sent_at: {ou['sent_at']}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only mailbox stats / activity report")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--since", default=None,
                   help="filter to messages after this time. "
                        "Accepts relative (30m / 24h / 7d) or ISO 8601.")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of text report")
    args = p.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    since = parse_since(args.since)
    s = collect_stats(args.db, since)

    if args.json:
        print(json.dumps(s, indent=2, ensure_ascii=False))
    else:
        print(render_text(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
