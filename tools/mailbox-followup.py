"""Follow-up scheduler for wiki supervisor proxy mode.

When wiki sees `MAIL id=N to=<peer>` for a user-originated routed mail and
decides it warrants follow-up, fire-and-forget this script in the background:

    py mailbox-followup.py --id <N> --delay 30

After <delay> seconds, the script checks `messages.read_at` for that id:
  * Still NULL → INSERT a `wiki <- mailbox-admin` ping mail summarizing the
    stuck inbound (so wiki's watcher fires again and wiki can proxy)
  * Already has a timestamp → silent exit (peer handled it)

Why a ping mail instead of e.g. ScheduleWakeup: the existing watcher path is
the reliable wake mechanism for wiki — reuse it rather than introduce a
parallel scheduler. The follow-up shows up naturally in mailbox-dump too,
making the supervisor flow auditable.

Spawn from wiki:
    bash -lc 'nohup py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-followup.py" \\
              --id 123 --delay 30 >/dev/null 2>&1 &'
or just `run_in_background:true` via Claude Code Bash.
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone


DEFAULT_DB = r'C:\Users\User\.claude\mailbox\mailbox.db'
DEFAULT_DELAY = 30
DEFAULT_TO = 'wiki'


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--id', type=int, required=True,
                   help='message id to follow up on')
    p.add_argument('--delay', type=int, default=DEFAULT_DELAY,
                   help=f'seconds to wait before checking (default {DEFAULT_DELAY})')
    p.add_argument('--to', default=DEFAULT_TO,
                   help='supervisor to ping if still unread (default wiki)')
    p.add_argument('--db', default=DEFAULT_DB)
    args = p.parse_args()

    time.sleep(args.delay)

    try:
        db = sqlite3.connect(args.db)
        row = db.execute(
            "SELECT read_at, from_name, to_name, substr(body, 1, 200) "
            "FROM messages WHERE id=?",
            (args.id,),
        ).fetchone()
    except sqlite3.Error as e:
        print(f"[followup] db error: {e}", file=sys.stderr)
        return 1

    if row is None:
        print(f"[followup] id={args.id} not found", file=sys.stderr)
        return 1

    read_at, from_name, to_name, preview = row
    if read_at is not None:
        # Peer handled it; nothing to do
        print(f"[followup] id={args.id} read_at={read_at}, peer handled it",
              file=sys.stderr)
        return 0

    # Build ping body
    safe_preview = (preview or "").replace("\r", " ").replace("\n", " | ")
    body = (
        f"⏰ Followup: msg #{args.id} from={from_name} to={to_name} "
        f"unread for {args.delay}s. Wiki should proxy if user-originated.\n"
        f"Preview: {safe_preview}"
    )
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        db.execute(
            "INSERT INTO messages(from_name, to_name, body, sent_at) "
            "VALUES(?, ?, ?, ?)",
            ('mailbox-admin', args.to, body, now),
        )
        db.commit()
        print(f"[followup] id={args.id} still unread, ping inserted to {args.to}",
              file=sys.stderr)
    except sqlite3.Error as e:
        print(f"[followup] insert failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
