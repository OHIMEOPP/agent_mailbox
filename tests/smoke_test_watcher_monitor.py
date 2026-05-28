"""Smoke test for mailbox.watcher_monitor — state machine + collectors.

Covers:
  1. collect_peers — only returns peers within track window, classifies by age
  2. collect_bridge — translates /healthz JSON into status target
  3. diff_transitions — first-observation silenced, true flips returned
  4. should_alert — only DEAD-related flips fire (STALE flickers don't)
  5. _run_loop — full daemon iteration via fake clock + stop event +
     captured notify POSTs (no network)
"""
from __future__ import annotations

import http.server
import json
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from mailbox import watcher_monitor as wm  # noqa: E402


def _init_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as c:
        c.executescript("""
            CREATE TABLE peers (
                name TEXT PRIMARY KEY,
                last_seen_at TEXT NOT NULL
            );
        """)


def _set_peer(db: Path, name: str, last_seen_at: str) -> None:
    with sqlite3.connect(str(db)) as c:
        c.execute(
            "INSERT INTO peers(name, last_seen_at) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (name, last_seen_at),
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# === Tests =================================================================

def test_collect_peers(tmpdir: Path) -> None:
    db = tmpdir / "mailbox.db"
    _init_db(db)
    now = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)
    # 3s old → HEALTHY (60s * 0.2 = 12s threshold)
    _set_peer(db, "wiki", _iso(now - timedelta(seconds=3)))
    # 30s old → STALE
    _set_peer(db, "koatag", _iso(now - timedelta(seconds=30)))
    # 90s old → DEAD
    _set_peer(db, "mailbox-dev", _iso(now - timedelta(seconds=90)))
    # 2h old → outside track window, NOT returned
    _set_peer(db, "old-peer", _iso(now - timedelta(hours=2)))

    states = wm.collect_peers(db, dead_threshold=60,
                              track_window_hours=1, now=now)
    assert "peer:wiki" in states and states["peer:wiki"]["status"] == wm.HEALTHY
    assert states["peer:koatag"]["status"] == wm.STALE
    assert states["peer:mailbox-dev"]["status"] == wm.DEAD
    assert "peer:old-peer" not in states, \
        "peer outside track window should be skipped"
    print("[smoke] collect_peers ok — 3 in-window peers correctly classified")


def test_collect_bridge_healthy(tmpdir: Path) -> None:
    """Spin a tiny mock /healthz server returning a healthy gateway dict."""
    port = _free_port()
    payload = {
        "ok": True,
        "gateway": {
            "expected": True, "online": True,
            "last_ready_at": 1735300000.0, "last_error": None,
        },
    }

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = socketserver.TCPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        states = wm.collect_bridge(f"http://127.0.0.1:{port}/healthz")
        assert states["bridge:gateway"]["status"] == wm.HEALTHY
        # Now flip the payload to gateway-offline
        payload["gateway"]["online"] = False
        states = wm.collect_bridge(f"http://127.0.0.1:{port}/healthz")
        assert states["bridge:gateway"]["status"] == wm.DEAD
        # And to disabled (no token)
        payload["gateway"]["expected"] = False
        states = wm.collect_bridge(f"http://127.0.0.1:{port}/healthz")
        assert states["bridge:gateway"]["status"] == wm.DISABLED
    finally:
        srv.shutdown()
    print("[smoke] collect_bridge ok — healthy / dead / disabled transitions")


def test_collect_bridge_unreachable() -> None:
    # Pick a port nothing listens on (free port then close — race-y in
    # theory, but in practice the port stays cold for the second of test).
    port = _free_port()
    states = wm.collect_bridge(f"http://127.0.0.1:{port}/healthz",
                               timeout=1.0)
    assert states["bridge:gateway"]["status"] == wm.DEAD
    assert "unreachable" in states["bridge:gateway"]["detail"]
    print("[smoke] collect_bridge unreachable → DEAD ok")


def test_diff_transitions_silences_first_observation() -> None:
    curr = {"peer:wiki": {"status": wm.HEALTHY, "kind": "agent-watcher",
                          "name": "wiki"}}
    transitions = wm.diff_transitions({}, curr)
    assert transitions == [], \
        f"first-observation should be silent, got {transitions}"
    # Second tick with the SAME status — no transitions
    transitions = wm.diff_transitions(curr, curr)
    assert transitions == []
    # Third tick flips wiki dead — transition fires
    curr2 = {"peer:wiki": {"status": wm.DEAD, "kind": "agent-watcher",
                           "name": "wiki", "age_seconds": 90.0}}
    transitions = wm.diff_transitions(curr, curr2)
    assert len(transitions) == 1
    key, p, c, target = transitions[0]
    assert key == "peer:wiki" and p == wm.HEALTHY and c == wm.DEAD
    print("[smoke] diff_transitions ok — first-obs silent, flip surfaces")


def test_should_alert_only_dead_path() -> None:
    # HEALTHY → STALE: no alert
    fire, *_ = wm.should_alert(wm.HEALTHY, wm.STALE)
    assert fire is False
    # STALE → DEAD: alert with status=fail
    fire, status, label = wm.should_alert(wm.STALE, wm.DEAD)
    assert fire and status == "fail" and label == "DEAD"
    # DEAD → HEALTHY: recovery alert
    fire, status, label = wm.should_alert(wm.DEAD, wm.HEALTHY)
    assert fire and status == "done" and label == "recovered"
    # DEAD → STALE: also counts as recovery (the agent's back, just lagging)
    fire, status, _ = wm.should_alert(wm.DEAD, wm.STALE)
    assert fire and status == "done"
    # STALE → HEALTHY: not a paging event
    fire, *_ = wm.should_alert(wm.STALE, wm.HEALTHY)
    assert fire is False
    print("[smoke] should_alert ok — only DEAD-related flips page")


def test_run_loop_fires_on_death(tmpdir: Path) -> None:
    """Spin _run_loop with a fake clock + stop event. Two ticks:
       tick 1: wiki is HEALTHY (first observation — no fire)
       tick 2: wiki age >60s → DEAD (transition fires)
    Confirm one /agent-notify POST captured, with status=fail.
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    db = tmpdir / "mailbox.db"
    _init_db(db)
    base = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc)
    _set_peer(db, "wiki", _iso(base - timedelta(seconds=3)))

    # Capture /agent-notify POSTs by patching _notify (no real network)
    captured: list[dict] = []

    def fake_notify(url, task, status, detail, timeout=8.0):
        captured.append({"url": url, "task": task,
                         "status": status, "detail": detail})
        return True

    # Fake bridge collector — always HEALTHY (so it never enters transitions)
    def fake_collect_bridge(url, timeout=5.0):
        return {"bridge:gateway": {"kind": "bridge-gateway",
                                   "name": "Discord bot gateway",
                                   "status": wm.HEALTHY,
                                   "detail": "fake"}}

    # Clock advances 120s between ticks, so wiki's row is way past dead
    # threshold by tick 2. Briefing hour is set to 25 so it never fires.
    times = [base, base + timedelta(seconds=120),
             base + timedelta(seconds=240)]
    idx = {"i": 0}

    def fake_clock(tz=None):
        # Called twice per loop iter (UTC for collect_peers, local for
        # briefing). We bump only on the UTC call to keep both calls within
        # the same logical tick.
        t = times[min(idx["i"], len(times) - 1)]
        if tz is timezone.utc:
            idx["i"] += 1
            return t
        return t  # local time stamp for briefing logic — same tick

    stop = threading.Event()

    def stop_after_two_ticks():
        # Let the loop run, then signal stop after enough ticks
        time.sleep(0.5)
        stop.set()

    args = {
        "db_path": db,
        "tick": 0.05,  # short tick — stop_event.wait honors it quickly
        "dead_threshold": 60,
        "track_window": 1,
        "briefing_hour": 25,  # never triggers (hour < 25 is always true,
                              # but `now_local.hour < briefing_hour` is True so
                              # _briefing_due returns False before date check)
        "notify_url": "http://fake/agent-notify",
        "bridge_url": "http://fake/healthz",
    }
    with patch.object(wm, "_notify", side_effect=fake_notify), \
         patch.object(wm, "collect_bridge", side_effect=fake_collect_bridge):
        t = threading.Thread(target=wm._run_loop,
                             args=(args, stop, fake_clock),
                             daemon=True)
        t.start()
        stop_after_two_ticks()
        t.join(timeout=5)
        assert not t.is_alive(), "loop didn't exit on stop event"

    # We expect at least one DEAD ping. (We don't assert exactly one because
    # the loop may have iterated several times during the 0.5s window —
    # but tick 1's first-observation should be silent, and tick 2's HEALTHY
    # → DEAD fires once, after which the state stays DEAD so no further
    # notifications.)
    dead_pings = [c for c in captured if c["status"] == "fail"]
    assert dead_pings, \
        f"expected at least one DEAD ping, got {captured}"
    assert "wiki" in dead_pings[0]["task"], \
        f"DEAD ping should reference wiki: {dead_pings[0]}"
    assert "DEAD" in dead_pings[0]["task"]
    assert len(dead_pings) == 1, \
        f"DEAD should only fire once (state sticks), got {len(dead_pings)}"
    print(f"[smoke] _run_loop ok — 1 DEAD ping captured: "
          f"{dead_pings[0]['task']!r}")


def test_briefing_due_window() -> None:
    """_briefing_due should be False outside the briefing window so a late
    container restart doesn't re-page today's briefing."""
    # In-window, never fired today → due
    in_window = datetime(2026, 5, 28, 9, 15)
    assert wm._briefing_due(in_window, 9, last_date=None) is True
    # Same hour, already sent today → not due
    today = in_window.date()
    assert wm._briefing_due(in_window, 9, last_date=today) is False
    # Before window → not due
    too_early = datetime(2026, 5, 28, 7, 0)
    assert wm._briefing_due(too_early, 9, last_date=None) is False
    # After window (restart at 22:00 with briefing_hour=9 + 1h window) →
    # not due — this is the spam guard we just added
    too_late = datetime(2026, 5, 28, 22, 0)
    assert wm._briefing_due(too_late, 9, last_date=None) is False
    # Right at window edge (briefing_hour + window = 10:00 → boundary exclusive)
    edge = datetime(2026, 5, 28, 10, 0)
    assert wm._briefing_due(edge, 9, last_date=None) is False
    print("[smoke] _briefing_due window ok — late-restart spam guard")


def test_format_briefing_groups_by_kind() -> None:
    states = {
        "peer:wiki": {"kind": "agent-watcher", "name": "wiki",
                      "status": wm.HEALTHY, "age_seconds": 3.0},
        "peer:koatag": {"kind": "agent-watcher", "name": "koatag",
                        "status": wm.DEAD, "age_seconds": 90.0},
        "bridge:gateway": {"kind": "bridge-gateway",
                           "name": "Discord bot gateway",
                           "status": wm.HEALTHY},
    }
    out = wm.format_briefing(states)
    assert "[agent-watcher]" in out and "[bridge-gateway]" in out
    assert "wiki: HEALTHY" in out
    assert "koatag: DEAD" in out
    print("[smoke] format_briefing ok — kind grouping + status lines")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="watcher-monitor-smoke-"))
    failures = []
    tests = [
        ("collect_peers", lambda: test_collect_peers(workdir)),
        ("collect_bridge healthy/dead/disabled",
         lambda: test_collect_bridge_healthy(workdir)),
        ("collect_bridge unreachable",
         lambda: test_collect_bridge_unreachable()),
        ("diff_transitions first-obs silence",
         test_diff_transitions_silences_first_observation),
        ("should_alert dead-only", test_should_alert_only_dead_path),
        ("_run_loop fires on death",
         lambda: test_run_loop_fires_on_death(workdir / "run-loop")),
        ("_briefing_due window", test_briefing_due_window),
        ("format_briefing", test_format_briefing_groups_by_kind),
    ]
    for name, fn in tests:
        try:
            sub = workdir / name.replace(" ", "_").replace("/", "_")
            sub.mkdir(parents=True, exist_ok=True)
            fn()
        except AssertionError as e:
            print(f"[smoke] FAIL {name}: {e}", file=sys.stderr)
            failures.append(name)
        except Exception as e:
            print(f"[smoke] ERR  {name}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            import traceback
            traceback.print_exc()
            failures.append(name)
    if failures:
        print(f"\n[smoke] FAILED {len(failures)}/{len(tests)}: {failures}",
              file=sys.stderr)
        return 2
    print(f"\n[smoke] ALL {len(tests)} TESTS PASSED")
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
