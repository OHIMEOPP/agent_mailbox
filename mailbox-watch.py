"""Mailbox watcher — polls the shared SQLite mailbox every 5s, exits when a
new message arrives for the given instance name.

Usage:
    py mailbox-watch.py <instance-name>           # e.g. wiki, koatag
    py mailbox-watch.py wiki --tick 5 --max 720   # optional overrides

Designed to be launched as a background bash by Claude Code: when it exits
(due to a new message), the harness wakes the agent. The agent then calls
`mcp__mailbox__inbox` + `mcp__mailbox__mark_read` to handle the message
properly.

Why a background OS process and not /loop / ScheduleWakeup:
- This script is a subprocess on the host OS — it does NOT trigger Claude
  agent turns, does NOT read prompt context, and does NOT touch prompt cache.
  A 5s tick costs essentially nothing.
- ScheduleWakeup / CronCreate fire at the agent-turn level: every wake reads
  the full conversation context, so anything below the 5min prompt-cache TTL
  burns tokens. That's why those mechanisms are clamped to >= 60s.
- Different layers, different cost models. Don't conflate.

The agent is woken EXACTLY ONCE — when a real message arrives — making this
event-driven, not interval-driven.

The query filters by `to_name = <name>` AND `read_at IS NULL`, so each
instance's watcher only fires on messages addressed to itself. Multiple
instances can run their own watchers in parallel against the same DB
without cross-triggering.
"""
import argparse
import io
import sqlite3
import sys
import time

# Force UTF-8 on stdout/stderr so emoji / CJK in previews don't crash on
# Windows consoles defaulted to cp950 / cp1252. `reconfigure()` works for
# real consoles but is unreliable when stdout is redirected to a pipe by
# a subprocess wrapper (e.g. claude-code's run_in_background) — in that
# case the inherited encoding sticks. Re-wrapping the underlying byte
# buffer with our own TextIOWrapper sidesteps that entirely.
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
sys.stderr = io.TextIOWrapper(
    sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

DB = r'C:\Users\User\.claude-mailbox.db'


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('name', help="instance name to watch (e.g. wiki, koatag)")
    p.add_argument('--tick', type=int, default=5, help="seconds between polls")
    p.add_argument('--max', type=int, default=720,
                   help="max ticks before timeout (default 720 = 1hr at 5s)")
    p.add_argument('--db', default=DB, help="path to mailbox SQLite db")
    args = p.parse_args()

    start = time.time()
    for i in range(args.max):
        try:
            conn = sqlite3.connect(args.db)
            rows = list(conn.execute(
                "SELECT id, from_name, sent_at, substr(body, 1, 200) "
                "FROM messages WHERE to_name=? AND read_at IS NULL "
                "ORDER BY id",
                (args.name,),
            ))
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

    print(f"[watcher] timed out after {args.max * args.tick}s "
          f"with no new messages for '{args.name}'")
    return 1


if __name__ == '__main__':
    sys.exit(main())
