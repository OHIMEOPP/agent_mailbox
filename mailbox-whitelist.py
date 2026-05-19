"""Mailbox whitelist CLI — manage approved Discord users for stranger-conv.

Schema lives at ~/.claude/mailbox/whitelist.db (separate from message DB).

Usage:
  py mailbox-whitelist.py list                      # list approved + pending
  py mailbox-whitelist.py add <username> [note]     # approve a user
  py mailbox-whitelist.py remove <username>         # revoke
  py mailbox-whitelist.py pending                   # list pending DMs
  py mailbox-whitelist.py promote <username>        # approve + move pending DMs → stranger-conv mailbox
  py mailbox-whitelist.py deny <username>           # discard pending DMs for this user, leave whitelist unchanged

Username matching is case-insensitive (Discord usernames are lowercase anyway).
"""
import argparse
import io
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

WHITELIST_DB = r'C:\Users\User\.claude\mailbox\whitelist.db'
MAILBOX_DB = r'C:\Users\User\.claude\mailbox\mailbox.db'


SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    discord_username TEXT PRIMARY KEY,
    discord_id       TEXT,
    approved_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    approved_by      TEXT NOT NULL DEFAULT 'ohimeopp',
    note             TEXT
);

CREATE TABLE IF NOT EXISTS pending (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_username TEXT NOT NULL,
    discord_id       TEXT,
    body             TEXT NOT NULL,
    received_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_pending_username ON pending(discord_username);
"""


def db():
    os.makedirs(os.path.dirname(WHITELIST_DB), exist_ok=True)
    conn = sqlite3.connect(WHITELIST_DB)
    conn.executescript(SCHEMA)
    return conn


def cmd_list(args):
    conn = db()
    print("=== Approved ===")
    rows = conn.execute("SELECT discord_username, discord_id, approved_at, note FROM whitelist ORDER BY approved_at").fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r[0]:<24} id={r[1] or '-':<20} since={r[2]}  {r[3] or ''}")
    print()
    print("=== Pending (DMs awaiting approval) ===")
    rows = conn.execute(
        "SELECT discord_username, COUNT(*) as cnt, MAX(received_at) "
        "FROM pending GROUP BY discord_username ORDER BY MAX(received_at) DESC"
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r[0]:<24} {r[1]} msg(s), last={r[2]}")


def cmd_add(args):
    conn = db()
    try:
        conn.execute(
            "INSERT INTO whitelist (discord_username, note) VALUES (?, ?)",
            (args.username.lower(), args.note),
        )
        conn.commit()
        print(f"approved: {args.username}")
    except sqlite3.IntegrityError:
        print(f"already approved: {args.username}")


def cmd_remove(args):
    conn = db()
    cur = conn.execute("DELETE FROM whitelist WHERE discord_username=?", (args.username.lower(),))
    conn.commit()
    print(f"removed {cur.rowcount} row(s)")


def cmd_pending(args):
    conn = db()
    rows = conn.execute(
        "SELECT id, discord_username, received_at, substr(body,1,80) FROM pending ORDER BY id"
    ).fetchall()
    if not rows:
        print("(no pending)")
        return
    for r in rows:
        print(f"[{r[0]:>4}] {r[1]:<24} {r[2]}")
        print(f"        {r[3]!r}")


def cmd_promote(args):
    """Approve user + move all their pending DMs into stranger-conv mailbox."""
    username = args.username.lower()
    conn = db()
    # Add to whitelist (idempotent)
    conn.execute(
        "INSERT OR IGNORE INTO whitelist (discord_username, note) VALUES (?, ?)",
        (username, args.note),
    )
    # Move pending
    pending = conn.execute(
        "SELECT id, discord_username, body, received_at FROM pending WHERE discord_username=? ORDER BY id",
        (username,),
    ).fetchall()
    if not pending:
        conn.commit()
        print(f"approved {username}; no pending DMs to promote")
        return
    mailbox = sqlite3.connect(MAILBOX_DB)
    for pid, uname, body, recv_at in pending:
        mailbox.execute(
            "INSERT INTO messages (from_name, to_name, body, sent_at) VALUES (?, ?, ?, ?)",
            (f"user-discord ({uname})", "stranger-conv", body, recv_at),
        )
    mailbox.commit()
    mailbox.close()
    conn.execute("DELETE FROM pending WHERE discord_username=?", (username,))
    conn.commit()
    print(f"approved {username}; promoted {len(pending)} pending DM(s) to stranger-conv mailbox")


def cmd_deny(args):
    """Discard pending DMs for this user (whitelist unchanged)."""
    username = args.username.lower()
    conn = db()
    cur = conn.execute("DELETE FROM pending WHERE discord_username=?", (username,))
    conn.commit()
    print(f"denied: discarded {cur.rowcount} pending DM(s) from {username}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    sub.add_parser("pending")

    add = sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("note", nargs="?", default=None)

    rm = sub.add_parser("remove")
    rm.add_argument("username")

    promote = sub.add_parser("promote")
    promote.add_argument("username")
    promote.add_argument("note", nargs="?", default=None)

    deny = sub.add_parser("deny")
    deny.add_argument("username")

    args = p.parse_args()
    {
        "list": cmd_list,
        "add": cmd_add,
        "remove": cmd_remove,
        "pending": cmd_pending,
        "promote": cmd_promote,
        "deny": cmd_deny,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
