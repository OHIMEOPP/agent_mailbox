"""Stranger gate: whitelist DB ops + pending queue + allow/deny actions.

Schema lives in WHITELIST_DB (separate from messages.db):

    whitelist (discord_username PK, discord_id, approved_at, approved_by, note)
    pending   (id PK auto, discord_username, discord_id, discord_channel, body, received_at)

The whitelist table is the binary trust gate; pending stores DMs awaiting
user approve/deny via Discord commands.
"""
import sqlite3
import sys

from .config import WHITELIST_DB


def is_whitelisted(username):
    try:
        conn = sqlite3.connect(WHITELIST_DB)
        row = conn.execute(
            "SELECT 1 FROM whitelist WHERE discord_username=?",
            (username.lower(),),
        ).fetchone()
        conn.close()
        return bool(row)
    except sqlite3.Error:
        # Fail-closed: if whitelist DB doesn't exist yet, nobody is approved.
        return False


def queue_pending(username, body_text, author_id=None, channel=None):
    """INSERT into pending; ensures schema (idempotent). Returns pending id."""
    try:
        conn = sqlite3.connect(WHITELIST_DB)
        conn.executescript("""
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
            discord_channel  TEXT,
            body             TEXT NOT NULL,
            received_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """)
        # Backward-compat: add columns to existing pending table if missing.
        for col in ('discord_id TEXT', 'discord_channel TEXT'):
            try:
                conn.execute(f"ALTER TABLE pending ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already there
        cur = conn.execute(
            "INSERT INTO pending (discord_username, discord_id, discord_channel, body) "
            "VALUES (?, ?, ?, ?)",
            (username.lower(), author_id, channel, body_text),
        )
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        return pid
    except sqlite3.Error as e:
        sys.stdout.write(f"[whitelist] pending-queue fail: {e}\n")
        return None


def approve_user(username, mailbox_db):
    """Add user to whitelist + move pending DMs to stranger-conv mailbox.

    Atomically across two DB files (whitelist.db + messages.db) — best-effort,
    no two-phase commit. Returns (promoted_count, error_str_or_None).
    """
    username = username.lower()
    wl = None
    mb = None
    try:
        wl = sqlite3.connect(WHITELIST_DB, timeout=5.0)
        wl.execute("PRAGMA busy_timeout = 5000")
        wl.execute(
            "INSERT OR IGNORE INTO whitelist (discord_username) VALUES (?)",
            (username,),
        )
        pending = wl.execute(
            "SELECT discord_username, body, received_at, discord_channel FROM pending "
            "WHERE discord_username=? ORDER BY id",
            (username,),
        ).fetchall()
        if pending:
            mb = sqlite3.connect(mailbox_db, timeout=5.0)
            mb.execute("PRAGMA busy_timeout = 5000")
            for uname, body_text, recv_at, ch in pending:
                fname = f"user-discord ({uname}) ch={ch}" if ch else f"user-discord ({uname})"
                mb.execute(
                    "INSERT INTO messages (from_name, to_name, body, sent_at) "
                    "VALUES (?, ?, ?, ?)",
                    (fname, "stranger-conv", body_text, recv_at),
                )
            mb.commit()
        wl.execute("DELETE FROM pending WHERE discord_username=?", (username,))
        wl.commit()
        return (len(pending), None)
    except sqlite3.Error as e:
        return (0, str(e))
    finally:
        if mb is not None:
            try: mb.close()
            except Exception: pass
        if wl is not None:
            try: wl.close()
            except Exception: pass


def deny_user(username):
    """Discard pending DMs for user; whitelist unchanged. Returns (count, err)."""
    username = username.lower()
    wl = None
    try:
        wl = sqlite3.connect(WHITELIST_DB, timeout=5.0)
        wl.execute("PRAGMA busy_timeout = 5000")
        cur = wl.execute(
            "DELETE FROM pending WHERE discord_username=?", (username,)
        )
        n = cur.rowcount
        wl.commit()
        return (n, None)
    except sqlite3.Error as e:
        return (0, str(e))
    finally:
        if wl is not None:
            try: wl.close()
            except Exception: pass
