"""Smoke test for tools/mailbox-doctor.py — system healthcheck wrapper.

Uses a temp hub + temp DB; can't easily mock docker, so skips that check
in test mode.
"""
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if json.loads(r.read()).get("ok"):
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server never came up at {url}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-doctor-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    backups = workdir / "backups"
    backups.mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"[smoke] workdir={workdir} port={port}")

    env = os.environ.copy()
    env["CLAUDE_MAILBOX_TOKEN"] = token
    env["MAILBOX_WEBHOOKS_DISABLED"] = "1"
    env["MAILBOX_RETENTION_DISABLED"] = "1"
    env["MAILBOX_BACKUP_DISABLED"] = "1"
    env["MAILBOX_SCHEDULED_DISABLED"] = "1"
    here = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, str(here / "mailbox-server.py"),
         "--host", "127.0.0.1", "--port", str(port),
         "--db", str(db), "--attachments-dir", str(attachments)],
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        wait_health(base, timeout=15)

        # Seed a message + some audit + a webhook + a scheduled item via direct SQL
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO messages(from_name, to_name, body) "
            "VALUES('alice', 'bob', 'test msg')")
        conn.commit()
        conn.close()

        # Run doctor in JSON mode
        doctor = here / "tools" / "mailbox-doctor.py"
        r = subprocess.run(
            [sys.executable, str(doctor), "--hub", base, "--db", str(db),
             "--attachments-dir", str(attachments), "--backup-dir", str(backups),
             "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
        # rc may be 0 or 1 depending on warnings; we examine content
        assert r.returncode in (0, 1), f"unexpected exit {r.returncode}: {r.stderr}"
        checks = json.loads(r.stdout)
        by_name = {c["name"]: c for c in checks}

        # ---- Test 1: hub_health is green ----
        assert by_name["hub_health"]["status"] == "🟢"
        print(f"[smoke] hub_health 🟢 ok ({by_name['hub_health']['summary']})")

        # ---- Test 2: db_file is green ----
        assert by_name["db_file"]["status"] == "🟢"
        print("[smoke] db_file 🟢 ok")

        # ---- Test 3: schema_migrations green (applied = total) ----
        assert by_name["schema_migrations"]["status"] == "🟢"
        print(f"[smoke] schema_migrations 🟢 ok ({by_name['schema_migrations']['summary']})")

        # ---- Test 4: disk_usage green (tiny temp DB) ----
        assert by_name["disk_usage"]["status"] == "🟢"
        print("[smoke] disk_usage 🟢 ok")

        # ---- Test 5: webhook_deliveries skip (no webhooks registered) ----
        assert by_name["webhook_deliveries"]["status"] == "⚪"
        print("[smoke] webhook_deliveries ⚪ skip ok (no webhooks)")

        # ---- Test 6: scheduled_queue green (no pending) ----
        assert by_name["scheduled_queue"]["status"] == "🟢"
        print("[smoke] scheduled_queue 🟢 ok (no pending)")

        # ---- Test 7: fts5_index_drift green (1 msg, 1 indexed) ----
        assert by_name["fts5_index_drift"]["status"] == "🟢"
        print("[smoke] fts5_index_drift 🟢 ok")

        # ---- Test 8: latest_message green (just sent) ----
        assert by_name["latest_message"]["status"] == "🟢"
        print("[smoke] latest_message 🟢 ok")

        # ---- Test 9: text mode also runs ----
        r2 = subprocess.run(
            [sys.executable, str(doctor), "--hub", base, "--db", str(db),
             "--attachments-dir", str(attachments), "--backup-dir", str(backups)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
        assert "🩺 mailbox-doctor" in r2.stdout
        assert "Overall:" in r2.stdout
        print("[smoke] text mode renders ok")

        # ---- Test 10: bad hub URL → 🔴 hub_health (and exit 1) ----
        r3 = subprocess.run(
            [sys.executable, str(doctor),
             "--hub", "http://nonexistent-host.invalid:9999",
             "--db", str(db), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
        assert r3.returncode == 1, f"expected exit 1 on dead hub, got {r3.returncode}"
        checks3 = json.loads(r3.stdout)
        hh = next(c for c in checks3 if c["name"] == "hub_health")
        assert hh["status"] == "🔴"
        print("[smoke] dead hub → 🔴 hub_health + exit 1 ok")

        print(f"\n[smoke] ALL DOCTOR TESTS PASSED")
        return 0
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
