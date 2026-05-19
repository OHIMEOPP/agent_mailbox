"""Mailbox watcher -- polls the shared SQLite mailbox and signals the agent
when new mail arrives for the given instance name.

Two operating modes:

    py mailbox-watch.py <name>              # exit-mode (default, legacy)
    py mailbox-watch.py <name> --monitor    # stream-mode (event-driven, no death)

EXIT-MODE (--max controls ticks, default infinite):
    The script EXITS with code 0 the first time it sees unread mail addressed
    to <name>. Launched via Claude Code's Bash tool with run_in_background:true
    so the harness's task-notification fires on exit -> agent wake.

    Limitation: between exit and next watcher restart there is a gap during
    which incoming mail piles up unseen. The agent has to remember to relaunch
    the watcher after every wake. Hook + UserPromptSubmit reminder is a safety
    net but only fires when the user types into CLI.

STREAM-MODE (--monitor):
    The script NEVER exits on mail. Instead, every new unread message produces
    one stdout line, then polling continues. Launched via Claude Code's Monitor
    tool with persistent:true so each stdout line becomes a wake notification.

    Advantage: watcher stays alive across an unbounded number of mail events.
    No gap, no manual restart cycle. Only dies on script error or session end.

Per-mail stdout format (stream-mode):
    MAIL id=<int> from=<peer> sent=<iso8601> preview=<first 200 chars,
    newlines as " | ">

Why an OS subprocess (not /loop or ScheduleWakeup):
- Subprocess polls SQLite directly -- no prompt context reread, no token cost.
- ScheduleWakeup / CronCreate fire at the agent-turn level: every wake reads
  the full conversation context. Clamped to >= 60s due to 5min prompt-cache
  TTL. Different cost model entirely.
"""
import argparse
import io
import sqlite3
import sys
import time

# Force UTF-8 on stdout/stderr so emoji / CJK in previews don't crash on
# Windows consoles defaulted to cp950 / cp1252.
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
sys.stderr = io.TextIOWrapper(
    sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

DB = r'C:\Users\User\.claude\mailbox\mailbox.db'


def heartbeat(conn: sqlite3.Connection, name: str) -> None:
    """Upsert peers.last_seen_at so bridge / dump tools can detect 'online'."""
    conn.execute(
        "INSERT INTO peers(name, last_seen_at) VALUES(?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT(name) DO UPDATE SET "
        "last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (name,),
    )
    conn.commit()


def fetch_unread(conn: sqlite3.Connection, name: str, since_id: int) -> list:
    return list(conn.execute(
        "SELECT id, from_name, sent_at, substr(body, 1, 200) "
        "FROM messages WHERE to_name=? AND read_at IS NULL AND id > ? "
        "ORDER BY id",
        (name, since_id),
    ))


def fetch_unread_all(conn: sqlite3.Connection, since_id: int) -> list:
    """Same as fetch_unread but across every to_name (supervisor mode)."""
    return list(conn.execute(
        "SELECT id, from_name, to_name, sent_at, substr(body, 1, 200) "
        "FROM messages WHERE read_at IS NULL AND id > ? "
        "ORDER BY id",
        (since_id,),
    ))


def run_exit_mode(args) -> int:
    """Legacy: exit code 0 on first sight of unread mail."""
    start = time.time()
    i = 0
    while args.max == 0 or i < args.max:
        try:
            conn = sqlite3.connect(args.db)
            heartbeat(conn, args.name)
            rows = fetch_unread(conn, args.name, 0)
            conn.close()
        except sqlite3.Error as e:
            print(f"[watcher] db error tick={i}: {e}", file=sys.stderr)
            time.sleep(args.tick)
            continue

        if rows:
            elapsed = int(time.time() - start)
            print(f"[watcher] {len(rows)} new message(s) for "
                  f"'{args.name}' after {elapsed}s:")
            for mid, sender, sent, preview in rows:
                print(f"  id={mid} from={sender} at={sent}")
                print(f"    {preview}")
            return 0

        time.sleep(args.tick)
        i += 1

    print(f"[watcher] timed out after {args.max * args.tick}s "
          f"with no new messages for '{args.name}'")
    return 1


def run_monitor_mode(args) -> int:
    """Stream: print one stdout line per new mail, keep polling forever.

    Tracks last_id (monotonic SQLite rowid) so re-announcing is avoided even
    if the agent is slow to mark_read. Each tick:
      1. heartbeat peers (own name only — peer rows are touched by their own
         watchers, supervisor mode doesn't impersonate)
      2. SELECT unread with id > last_id
         - default: filtered by to_name = our name
         - --watch-all: any to_name (supervisor mode for wiki)
      3. for each row: print line, advance last_id
    """
    last_id = 0
    watch_all = bool(args.watch_all)

    # On startup, baseline last_id to the current max so we don't re-announce
    # already-present unread mail. In watch-all mode baseline against the
    # whole messages table so historical mail to other peers doesn't replay.
    try:
        conn = sqlite3.connect(args.db)
        if watch_all:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE to_name=?",
                (args.name,),
            ).fetchone()
        last_id = row[0] if row else 0
        conn.close()
        mode_label = "WATCH-ALL" if watch_all else f"name={args.name}"
        print(f"[watcher] monitor-mode start {mode_label} "
              f"tick={args.tick}s baseline_id={last_id}",
              file=sys.stderr)
    except sqlite3.Error as e:
        print(f"[watcher] startup db error: {e}", file=sys.stderr)

    while True:
        try:
            conn = sqlite3.connect(args.db)
            heartbeat(conn, args.name)
            if watch_all:
                rows = fetch_unread_all(conn, last_id)
            else:
                rows = fetch_unread(conn, args.name, last_id)
            conn.close()
        except sqlite3.Error as e:
            print(f"[watcher] db error: {e}", file=sys.stderr)
            time.sleep(args.tick)
            continue

        for row in rows:
            if watch_all:
                mid, sender, recipient, sent, preview = row
                safe_preview = (preview or "").replace("\r", " ").replace("\n", " | ")
                print(f"MAIL id={mid} from={sender} to={recipient} sent={sent} preview={safe_preview}")
            else:
                mid, sender, sent, preview = row
                safe_preview = (preview or "").replace("\r", " ").replace("\n", " | ")
                print(f"MAIL id={mid} from={sender} sent={sent} preview={safe_preview}")
            last_id = mid

        time.sleep(args.tick)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('name', help="instance name to watch (e.g. wiki, koatag)")
    p.add_argument('--tick', type=int, default=5, help="seconds between polls")
    p.add_argument('--monitor', action='store_true',
                   help="stream-mode: print stdout line per new mail, never exit "
                        "(use with Claude Code Monitor tool, persistent=true)")
    p.add_argument('--max', type=int, default=0,
                   help="exit-mode only: max ticks before self-kill "
                        "(0 = infinite, default)")
    p.add_argument('--db', default=DB, help="path to mailbox SQLite db")
    p.add_argument('--watch-all', action='store_true',
                   help="supervisor mode: fire on ANY recipient's new mail "
                        "(not just to_name=<NAME>). Output adds 'to=<peer>' "
                        "field per line. Used by wiki to oversee whole mailbox.")
    args = p.parse_args()

    if args.monitor:
        return run_monitor_mode(args)
    return run_exit_mode(args)


if __name__ == '__main__':
    sys.exit(main())
