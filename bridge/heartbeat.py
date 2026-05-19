"""Agent-online detection via watcher heartbeat + mark_read fallback."""
import sqlite3
import sys
from datetime import datetime, timezone


def agent_recently_active(db_path, agent_name, within_seconds):
    """True if the agent's watcher wrote a heartbeat (peers.last_seen_at) in
    the window. Watcher updates this every 5s tick — the real 'alive' signal
    (mark_read is lumpy; agent may sit idle waiting for mail).

    Falls back to most recent message read_at if peers row is missing
    (backward compat with deployments predating the heartbeat write).
    """
    try:
        conn = sqlite3.connect(db_path)
        # Primary signal
        cur = conn.execute("SELECT last_seen_at FROM peers WHERE name=?", (agent_name,))
        row = cur.fetchone()
        heartbeat = row[0] if row else None
        # Fallback signal (legacy)
        cur = conn.execute(
            "SELECT MAX(read_at) FROM messages "
            "WHERE to_name=? AND read_at IS NOT NULL",
            (agent_name,),
        )
        last_read = cur.fetchone()[0]
        conn.close()
        latest = max(filter(None, [heartbeat, last_read]), default=None)
        if not latest:
            return False
        try:
            parsed = datetime.fromisoformat(latest.replace('Z', '+00:00'))
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        return (now - parsed).total_seconds() <= within_seconds
    except sqlite3.Error as e:
        sys.stdout.write(f"[heartbeat] db error: {e}\n")
        return False
