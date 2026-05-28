"""Watcher heartbeat monitor — pushes Discord notifications when an agent
watcher or the Discord bridge gateway goes dark/recovers, with a daily
roll-up briefing at MAILBOX_WATCHER_BRIEFING_HOUR_LOCAL.

Lives in mailbox-server.py's daemon-thread family alongside sweep / backup /
webhook / scheduled. Targets monitored:

1. **Agent watchers** — `peers.last_seen_at` per name. Any peer whose row was
   last updated within `MAILBOX_WATCHER_TRACK_WINDOW_HOURS` (default 1h) is
   "tracked". A tracked peer whose row is >`DEAD_THRESHOLD_SECONDS` stale is
   classified DEAD (default 60s). Peers outside the track window are assumed
   intentionally offline (session ended) and silently ignored — avoids spam.

2. **Bridge Discord gateway** — probe `MAILBOX_WATCHER_BRIDGE_URL`
   (default `http://mailbox-bridge:1904/healthz`). Gateway status is read
   from the `gateway.expected` + `gateway.online` flags.

Notifications go to `MAILBOX_WATCHER_NOTIFY_URL` (default
`http://mailbox-bridge:1904/agent-notify`). Disable everything with
`MAILBOX_WATCHER_MONITOR_DISABLED=1`.

The state machine fires on:
  - HEALTHY/STALE → DEAD (status=fail)
  - DEAD → HEALTHY/STALE (status=done, recovery)
  - First-observation transitions (None → *) are silenced so daemon restart
    doesn't spam.

Module is import-safe (no I/O at module level). All thread spin-up happens
inside `start_daemon()`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# === Defaults — overridable via env ========================================
DEFAULT_TICK_SECONDS = 30
DEFAULT_DEAD_THRESHOLD_SECONDS = 60
DEFAULT_TRACK_WINDOW_HOURS = 1
DEFAULT_BRIEFING_HOUR_LOCAL = 9
DEFAULT_NOTIFY_URL = "http://mailbox-bridge:1904/agent-notify"
DEFAULT_BRIDGE_HEALTH_URL = "http://mailbox-bridge:1904/healthz"

# Status vocabulary (also used by status_snapshot CLI output)
HEALTHY = "HEALTHY"
STALE = "STALE"
DEAD = "DEAD"
UNKNOWN = "UNKNOWN"
DISABLED = "DISABLED"  # bridge gateway: token unset / library missing

ICON = {HEALTHY: "🟢", STALE: "🟡", DEAD: "🔴",
        UNKNOWN: "⚪", DISABLED: "⚫"}


def _log(msg: str) -> None:
    sys.stdout.write(f"[watcher-monitor] {msg}\n")
    sys.stdout.flush()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(ts: str | None, now: datetime) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (now - dt).total_seconds()


def _classify_peer_age(age_s: float | None,
                       dead_threshold: float) -> str:
    """Three-band classifier for agent watcher peer rows.

    The 0.2 multiplier gives a ~12s healthy band when the threshold is 60s,
    which fits two watcher ticks (5s) plus jitter. Tunable but unlikely
    needed.
    """
    if age_s is None:
        return UNKNOWN
    if age_s < dead_threshold * 0.2:
        return HEALTHY
    if age_s < dead_threshold:
        return STALE
    return DEAD


# === Collectors — each returns dict keyed by stable target id ==============

def collect_peers(db_path: Path, dead_threshold: float,
                  track_window_hours: float,
                  now: datetime | None = None) -> dict:
    """Read peers within `track_window_hours`; classify each."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=track_window_hours)).isoformat()
    cutoff = cutoff.replace("+00:00", "Z")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            rows = conn.execute(
                "SELECT name, last_seen_at FROM peers "
                "WHERE last_seen_at >= ? ORDER BY name",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"peers query fail: {e}")
        return {}
    out = {}
    for name, last_seen in rows:
        age = _age_seconds(last_seen, now)
        out[f"peer:{name}"] = {
            "kind": "agent-watcher",
            "name": name,
            "last_seen_at": last_seen,
            "age_seconds": age,
            "status": _classify_peer_age(age, dead_threshold),
        }
    return out


def collect_bridge(bridge_url: str, timeout: float = 5.0) -> dict:
    """Probe bridge /healthz and translate the gateway JSON into a single
    status target keyed `bridge:gateway`. Returns DEAD when the HTTP probe
    itself fails (the bridge container or network is down), DISABLED when
    the bridge intentionally has gateway turned off (no token / no library).
    """
    try:
        req = urllib.request.Request(bridge_url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
        gw = payload.get("gateway") or {}
        expected = bool(gw.get("expected"))
        online = bool(gw.get("online"))
        if not expected:
            status = DISABLED
        elif online:
            status = HEALTHY
        else:
            status = DEAD
        detail = (gw.get("last_error")
                  or f"expected={expected} online={online}")
        return {
            "bridge:gateway": {
                "kind": "bridge-gateway",
                "name": "Discord bot gateway",
                "status": status,
                "detail": detail,
                "last_ready_at": gw.get("last_ready_at"),
            }
        }
    except urllib.error.HTTPError as e:
        return {"bridge:gateway": {
            "kind": "bridge-gateway", "name": "Discord bot gateway",
            "status": DEAD, "detail": f"HTTP {e.code}"}}
    except Exception as e:
        # urllib.error.URLError, ConnectionError, TimeoutError, JSONDecodeError…
        return {"bridge:gateway": {
            "kind": "bridge-gateway", "name": "Discord bot gateway",
            "status": DEAD, "detail": f"unreachable: {type(e).__name__}"}}


def status_snapshot(db_path: Path, bridge_url: str = DEFAULT_BRIDGE_HEALTH_URL,
                    dead_threshold: float = DEFAULT_DEAD_THRESHOLD_SECONDS,
                    track_window_hours: float = DEFAULT_TRACK_WINDOW_HOURS,
                    ) -> dict:
    """One-shot snapshot (no state machine, no /agent-notify). Used by the
    CLI `tools/mailbox-watcher-status.py`."""
    out = {}
    out.update(collect_peers(Path(db_path), dead_threshold, track_window_hours))
    out.update(collect_bridge(bridge_url))
    return out


# === State machine + notifier =============================================

def diff_transitions(prev: dict, curr: dict) -> list[tuple]:
    """Yield (target_key, prev_status, curr_status, curr_target) for status
    changes. Silences first-observation (prev missing) so daemon restart
    doesn't fire on every existing peer."""
    transitions = []
    for key in set(prev) | set(curr):
        p_status = (prev.get(key) or {}).get("status")
        c_target = curr.get(key)
        c_status = (c_target or {}).get("status")
        if p_status is None:
            # First time we see this target — never alert. The next tick
            # has prev_status set, so genuine future flips fire.
            continue
        if c_status is None:
            # Target disappeared (e.g. peer aged out of track window). Treat
            # as a soft drop, no alert — peers naturally rotate out.
            continue
        if p_status == c_status:
            continue
        transitions.append((key, p_status, c_status, c_target))
    return transitions


def should_alert(prev_status: str, curr_status: str) -> tuple[bool, str, str]:
    """Decide whether a transition warrants a Discord ping. Returns
    (fire_alert, agent_notify_status, human_label).

    Only DEAD-related transitions fire — STALE flickers between healthy and
    one-tick-late are noise, never paged.
    """
    if curr_status == DEAD:
        return True, "fail", "DEAD"
    if prev_status == DEAD and curr_status in (HEALTHY, STALE):
        return True, "done", "recovered"
    return False, "info", curr_status


def _notify(notify_url: str, task: str, status: str, detail: str,
            timeout: float = 8.0) -> bool:
    """POST /agent-notify with the schema bridge expects. Swallow any
    transport failure (caller logs)."""
    body = json.dumps({
        "agent": "watcher-monitor",
        "task": task,
        "status": status,
        "detail": detail,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        notify_url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except Exception as e:
        _log(f"notify fail: {type(e).__name__}: {e}")
        return False


def _format_transition_detail(key: str, prev_status: str,
                              curr_status: str, target: dict) -> str:
    name = target.get("name") or key
    kind = target.get("kind") or "?"
    age = target.get("age_seconds")
    age_str = f"{age:.0f}s" if age is not None else "?"
    last = (target.get("last_seen_at")
            or target.get("last_ready_at") or "—")
    extra = target.get("detail") or ""
    return (f"{ICON.get(curr_status, '?')} {kind} \"{name}\": "
            f"{prev_status} → {curr_status} | age={age_str} | "
            f"last={last}{(' | ' + extra) if extra else ''}")


def format_briefing(states: dict) -> str:
    """Multi-line summary for the daily ping. Groups by kind."""
    if not states:
        return "No tracked watchers right now."
    by_kind: dict[str, list] = {}
    for key, s in sorted(states.items()):
        by_kind.setdefault(s.get("kind", "?"), []).append((key, s))
    lines = []
    for kind, items in sorted(by_kind.items()):
        lines.append(f"[{kind}]")
        for _key, s in items:
            name = s.get("name") or _key
            status = s.get("status", UNKNOWN)
            age = s.get("age_seconds")
            age_str = f" age={age:.0f}s" if age is not None else ""
            lines.append(f"  {ICON.get(status, '?')} {name}: {status}{age_str}")
    return "\n".join(lines)


# === Daemon loop ===========================================================

BRIEFING_WINDOW_HOURS = 1  # only fire within this many hours of the hour
                           # — late restart shouldn't re-page today's briefing


def _briefing_due(now_local: datetime, briefing_hour: int,
                  last_date) -> bool:
    """Fire once per local-time day, only within BRIEFING_WINDOW_HOURS of
    `briefing_hour`. last_date is None on first run.

    The window guards against restart spam: if mailbox-server bounces at
    22:00 with briefing_hour=9, last_date is None and 22 > 9, but firing
    here would re-page today's briefing. The window check (22 > 9+1=10)
    silences it. Next 09:00–10:00 fires normally.
    """
    if now_local.hour < briefing_hour:
        return False
    if now_local.hour >= briefing_hour + BRIEFING_WINDOW_HOURS:
        return False
    today = now_local.date()
    return last_date != today


def _run_loop(args: dict, stop_event: threading.Event | None = None,
              clock: "callable | None" = None) -> None:
    """Daemon body. `stop_event` + `clock` are seams for the smoke test —
    in production the thread runs forever and uses real datetime.now."""
    prev_states: dict = {}
    last_briefing_date = None
    if clock is None:
        def clock(tz=None):
            return datetime.now(tz) if tz is not None else datetime.now()
    _log(f"start tick={args['tick']}s dead_threshold={args['dead_threshold']}s "
         f"track_window={args['track_window']}h "
         f"briefing_hour={args['briefing_hour']} (local)")

    while True:
        try:
            now_utc = clock(timezone.utc)
            curr = {}
            curr.update(collect_peers(
                args["db_path"], args["dead_threshold"],
                args["track_window"], now=now_utc))
            curr.update(collect_bridge(args["bridge_url"]))

            for key, prev_st, curr_st, target in diff_transitions(
                    prev_states, curr):
                fire, notify_status, label = should_alert(prev_st, curr_st)
                if not fire:
                    continue
                detail = _format_transition_detail(
                    key, prev_st, curr_st, target)
                task = f"{target.get('name') or key} {label}"
                _log(f"transition {key}: {prev_st} → {curr_st} → notify")
                _notify(args["notify_url"], task, notify_status, detail)

            now_local = clock()
            if _briefing_due(now_local, args["briefing_hour"],
                             last_briefing_date):
                _notify(args["notify_url"],
                        "Daily watcher briefing", "info",
                        format_briefing(curr))
                last_briefing_date = now_local.date()
                _log(f"briefing sent for {last_briefing_date}")

            prev_states = curr
        except Exception as e:
            _log(f"tick error: {type(e).__name__}: {e}")

        if stop_event is not None:
            if stop_event.wait(args["tick"]):
                return
        else:
            time.sleep(args["tick"])


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log(f"bad {name}={raw!r}; falling back to {default}")
        return default


def start_daemon(db_path: Path) -> bool:
    """Spawn the monitor thread. Returns True if started, False if disabled
    via env (MAILBOX_WATCHER_MONITOR_DISABLED=1).
    """
    if os.environ.get("MAILBOX_WATCHER_MONITOR_DISABLED", "").strip() in (
            "1", "true", "yes"):
        _log("disabled via MAILBOX_WATCHER_MONITOR_DISABLED")
        return False
    args = {
        "db_path": Path(db_path),
        "tick": _env_int("MAILBOX_WATCHER_MONITOR_TICK_SECONDS",
                         DEFAULT_TICK_SECONDS),
        "dead_threshold": _env_int(
            "MAILBOX_WATCHER_DEAD_THRESHOLD_SECONDS",
            DEFAULT_DEAD_THRESHOLD_SECONDS),
        "track_window": _env_int(
            "MAILBOX_WATCHER_TRACK_WINDOW_HOURS",
            DEFAULT_TRACK_WINDOW_HOURS),
        "briefing_hour": _env_int(
            "MAILBOX_WATCHER_BRIEFING_HOUR_LOCAL",
            DEFAULT_BRIEFING_HOUR_LOCAL),
        "notify_url": os.environ.get(
            "MAILBOX_WATCHER_NOTIFY_URL", DEFAULT_NOTIFY_URL),
        "bridge_url": os.environ.get(
            "MAILBOX_WATCHER_BRIDGE_URL", DEFAULT_BRIDGE_HEALTH_URL),
    }
    t = threading.Thread(target=_run_loop, args=(args,),
                         daemon=True, name="watcher-monitor")
    t.start()
    return True
