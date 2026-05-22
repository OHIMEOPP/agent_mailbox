"""mailbox-doctor — one-stop healthcheck for the entire mailbox stack.

Runs ~12 read-only probes against hub + DB + filesystem + daemons,
classifies each as 🟢 (ok) / 🟡 (warn) / 🔴 (fail), prints a summary
table + overall verdict.

Usage:
    py tools/mailbox-doctor.py                   # full report
    py tools/mailbox-doctor.py --json            # machine-readable
    py tools/mailbox-doctor.py --hub http://... # custom hub
    py tools/mailbox-doctor.py --strict          # exit 1 on any 🟡 or 🔴

Checks (added new categories as feature surface grew):
    1. Hub /health reachable + ok=true
    2. Docker container mailbox-server running (best-effort via docker ps)
    3. DB file exists + readable
    4. Schema migrations head matches MIGRATIONS list (no pending upgrades)
    5. Disk usage of DB + attachments + backups (warn at 1GB, fail at 5GB)
    6. Daemon liveness — last sweep / backup / scheduled run within window
    7. Webhook delivery health — no pending > 100, no failed > 10 stuck
    8. Active claim count sanity (warn if > 50 stale claims)
    9. Schedule queue health — no overdue > 1 hour
   10. Audit log growing (warn if 0 audit rows past 24h — daemon may be dead)
   11. FTS5 index size matches messages count (within 5% — drift = bug)
   12. Latest message timestamp (warn if no msg in 24h — quiet hub OK,
       but worth flagging in case watcher's broken)

Read-only — never writes. Safe to run anytime, no docker stop.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

DEFAULT_DB = Path.home() / ".claude" / "mailbox" / "mailbox.db"
DEFAULT_ATTACH = Path.home() / ".claude" / "mailbox" / "attachments"
DEFAULT_BACKUPS = Path.home() / ".claude" / "mailbox" / "backups"
DEFAULT_HUB = "http://127.0.0.1:1905"


def _ts_age_seconds(iso_or_none: str | None) -> float | None:
    if not iso_or_none:
        return None
    try:
        # Try with milliseconds
        ts = datetime.fromisoformat(iso_or_none.rstrip("Z").rstrip("+00:00")).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            ts = datetime.fromisoformat(iso_or_none.replace("Z", "+00:00"))
        except ValueError:
            return None
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds()


def _dir_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = "?"  # 🟢 ok / 🟡 warn / 🔴 fail / ⚪ skip
        self.summary = ""
        self.details: dict = {}

    def ok(self, msg, **kv):
        self.status = "🟢"
        self.summary = msg
        self.details.update(kv)

    def warn(self, msg, **kv):
        self.status = "🟡"
        self.summary = msg
        self.details.update(kv)

    def fail(self, msg, **kv):
        self.status = "🔴"
        self.summary = msg
        self.details.update(kv)

    def skip(self, msg, **kv):
        self.status = "⚪"
        self.summary = msg
        self.details.update(kv)

    def to_dict(self):
        return {"name": self.name, "status": self.status,
                "summary": self.summary, "details": self.details}


def run_checks(hub: str, db: Path, attach_dir: Path, backup_dir: Path) -> list[Check]:
    checks: list[Check] = []

    # 1. Hub /health reachable
    c = Check("hub_health")
    try:
        with urllib.request.urlopen(f"{hub.rstrip('/')}/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
        if health.get("ok"):
            c.ok(f"hub at {hub} → ok=true", schema_v=health.get("schema_latest_version"),
                 msgs=health.get("message_count"))
        else:
            c.fail(f"hub /health returned ok={health.get('ok')}", health=health)
    except urllib.error.URLError as e:
        c.fail(f"hub /health unreachable: {e.reason}")
    except Exception as e:
        c.fail(f"hub probe error: {type(e).__name__}: {e}")
    checks.append(c)
    health = c.details.get("health", {})
    if c.status == "🟢":
        # Re-fetch full payload for downstream checks
        try:
            with urllib.request.urlopen(f"{hub.rstrip('/')}/health", timeout=5) as r:
                health = json.loads(r.read().decode("utf-8"))
        except Exception:
            pass

    # 2. Docker container running (best-effort)
    c = Check("docker_container")
    if shutil.which("docker"):
        try:
            r = subprocess.run(
                ["docker", "ps", "--filter", "name=mailbox-server",
                 "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=5,
            )
            status_line = r.stdout.strip()
            if "Up" in status_line:
                c.ok(f"container alive: {status_line}")
            elif status_line:
                c.fail(f"container present but not Up: {status_line}")
            else:
                c.warn("no mailbox-server container found (manual run? or stopped?)")
        except Exception as e:
            c.warn(f"docker ps probe failed: {type(e).__name__}: {e}")
    else:
        c.skip("docker CLI not on PATH (running without container?)")
    checks.append(c)

    # 3. DB file
    c = Check("db_file")
    if db.exists():
        try:
            size_mb = db.stat().st_size / 1024 / 1024
            # Try a quick SELECT to confirm not corrupted
            conn = sqlite3.connect(str(db), timeout=2.0)
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            conn.close()
            c.ok(f"{db.name} {size_mb:.2f}MB readable", path=str(db), size_mb=size_mb)
        except sqlite3.Error as e:
            c.fail(f"db readable but query failed: {e}")
    else:
        c.fail(f"db file missing: {db}")
    checks.append(c)

    # 4. Schema migrations head
    c = Check("schema_migrations")
    if health:
        applied = health.get("schema_migrations_applied")
        latest = health.get("schema_latest_version")
        total_known = health.get("schema_total_known")
        if applied is not None and total_known is not None:
            if applied >= total_known:
                c.ok(f"v{latest} applied ({applied}/{total_known})",
                     latest=latest, applied=applied, total_known=total_known)
            else:
                c.warn(f"behind: v{latest} of {total_known} (run mailbox-server boot)",
                       latest=latest, applied=applied)
        else:
            c.skip("schema migration info not in /health")
    else:
        c.skip("no /health data")
    checks.append(c)

    # 5. Disk usage
    c = Check("disk_usage")
    db_bytes = db.stat().st_size if db.exists() else 0
    attach_bytes = _dir_bytes(attach_dir)
    backup_bytes = _dir_bytes(backup_dir)
    total = db_bytes + attach_bytes + backup_bytes
    total_mb = total / 1024 / 1024
    if total_mb < 1024:
        c.ok(f"total {total_mb:.1f} MB (db {db_bytes/1024/1024:.1f} + "
             f"attach {attach_bytes/1024/1024:.1f} + backup {backup_bytes/1024/1024:.1f})",
             db_mb=db_bytes/1024/1024, attach_mb=attach_bytes/1024/1024,
             backup_mb=backup_bytes/1024/1024)
    elif total_mb < 5120:
        c.warn(f"total {total_mb:.1f} MB approaching 5GB threshold")
    else:
        c.fail(f"total {total_mb:.1f} MB > 5GB — review retention / backup rotation")
    checks.append(c)

    # 6. Daemon liveness
    c = Check("daemon_liveness")
    if health:
        sweep_age = _ts_age_seconds(health.get("last_sweep_at"))
        backup_age = _ts_age_seconds(health.get("last_backup_at"))
        scheduled_age = _ts_age_seconds(health.get("last_scheduled_at"))
        webhook_age = _ts_age_seconds(health.get("webhook_last_fired_at"))

        issues = []
        # Sweep: should run within 25hr (24h tick + 1hr grace)
        if sweep_age is None:
            issues.append("no last_sweep_at (boot grace + 1hr?)")
        elif sweep_age > 25 * 3600:
            issues.append(f"sweep stale: {sweep_age/3600:.1f}h ago")
        # Backup: same window
        if backup_age is None:
            issues.append("no last_backup_at (boot grace?)")
        elif backup_age > 25 * 3600:
            issues.append(f"backup stale: {backup_age/3600:.1f}h ago")
        # Webhook: only check if any active webhooks exist (else no fire expected)
        if health.get("webhook_count", 0) > 0 and webhook_age is None:
            issues.append("active webhooks but no fires seen")
        if not issues:
            c.ok(f"sweep={sweep_age/3600:.1f}h backup={backup_age/3600:.1f}h "
                 f"sched={scheduled_age/3600 if scheduled_age else 'N/A'}",
                 sweep_age_h=sweep_age/3600 if sweep_age else None,
                 backup_age_h=backup_age/3600 if backup_age else None)
        else:
            c.warn(", ".join(issues))
    else:
        c.skip("no /health data")
    checks.append(c)

    # 7. Webhook delivery health
    c = Check("webhook_deliveries")
    if health:
        pending = health.get("webhook_pending_deliveries", 0)
        failed = health.get("webhook_failed_deliveries", 0)
        webhook_count = health.get("webhook_count", 0)
        if webhook_count == 0:
            c.skip("no webhooks registered")
        elif pending > 100 or failed > 10:
            c.warn(f"{webhook_count} hooks: {pending} pending, {failed} failed")
        else:
            c.ok(f"{webhook_count} hooks healthy ({pending} pending, {failed} failed)")
    else:
        c.skip("no /health data")
    checks.append(c)

    # 8. Active claims (worker pattern)
    c = Check("active_claims")
    if health:
        claims = health.get("messages_claimed_active", 0)
        if claims < 50:
            c.ok(f"{claims} active claim(s)")
        else:
            c.warn(f"{claims} active claims — workers may be stuck or TTLs too long")
    else:
        c.skip("no /health data")
    checks.append(c)

    # 9. Schedule queue health
    c = Check("scheduled_queue")
    if health:
        pending = health.get("scheduled_pending", 0)
        next_at = health.get("next_deliver_at")
        if pending == 0:
            c.ok("no pending scheduled messages")
        else:
            next_age = _ts_age_seconds(next_at)
            if next_age and next_age > 3600:
                c.warn(f"{pending} pending, next overdue by {next_age/60:.0f}min (daemon dead?)")
            else:
                c.ok(f"{pending} pending, next at {next_at}")
    else:
        c.skip("no /health data")
    checks.append(c)

    # 10. Audit log activity
    c = Check("audit_activity")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=2.0)
            conn.execute("PRAGMA query_only = ON")
            try:
                recent = conn.execute(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-24 hours')"
                ).fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
                if total == 0:
                    c.skip("no audit rows yet (fresh DB?)")
                elif recent == 0:
                    c.warn(f"{total} total but 0 in last 24h — daemon or log_event() dead?")
                else:
                    c.ok(f"{recent} audit rows last 24h (total {total})")
            finally:
                conn.close()
        except sqlite3.OperationalError:
            c.skip("audit_log table missing (pre-2026-05-23 schema?)")
    else:
        c.skip("no db")
    checks.append(c)

    # 11. FTS5 index integrity
    c = Check("fts5_index_drift")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=2.0)
            conn.execute("PRAGMA query_only = ON")
            try:
                msg_count = conn.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                fts_count = conn.execute(
                    "SELECT COUNT(*) FROM messages_fts"
                ).fetchone()[0]
                drift = abs(msg_count - fts_count)
                tolerance = max(5, int(msg_count * 0.05))
                if drift <= tolerance:
                    c.ok(f"msgs={msg_count} fts={fts_count} (drift {drift} within tolerance)")
                else:
                    c.warn(f"msgs={msg_count} fts={fts_count} drift={drift} — rebuild?")
            except sqlite3.OperationalError:
                c.skip("FTS5 messages_fts table not present")
            finally:
                conn.close()
        except sqlite3.Error as e:
            c.skip(f"db query failed: {e}")
    else:
        c.skip("no db")
    checks.append(c)

    # 12. Latest message recency
    c = Check("latest_message")
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=2.0)
            conn.execute("PRAGMA query_only = ON")
            try:
                latest = conn.execute(
                    "SELECT MAX(sent_at) FROM messages"
                ).fetchone()[0]
                if not latest:
                    c.skip("no messages in db")
                else:
                    age = _ts_age_seconds(latest)
                    if age is None:
                        c.skip(f"unparsable ts: {latest}")
                    elif age < 86400:
                        c.ok(f"newest msg {age/3600:.1f}h ago")
                    elif age < 86400 * 7:
                        c.warn(f"newest msg {age/86400:.1f}d ago — watcher OK but quiet")
                    else:
                        c.warn(f"newest msg {age/86400:.1f}d ago — investigate")
            finally:
                conn.close()
        except sqlite3.Error as e:
            c.skip(f"db query failed: {e}")
    else:
        c.skip("no db")
    checks.append(c)

    return checks


def render_text(checks: list[Check]) -> str:
    lines = []
    lines.append("🩺 mailbox-doctor — system healthcheck")
    lines.append("=" * 60)
    for c in checks:
        lines.append(f"  {c.status}  {c.name:<25} {c.summary}")
    lines.append("=" * 60)
    counts = {"🟢": 0, "🟡": 0, "🔴": 0, "⚪": 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    overall = "🟢 HEALTHY"
    if counts["🔴"] > 0:
        overall = f"🔴 UNHEALTHY ({counts['🔴']} failed)"
    elif counts["🟡"] > 0:
        overall = f"🟡 DEGRADED ({counts['🟡']} warning(s))"
    lines.append(f"  Overall: {overall}  "
                 f"({counts['🟢']} ok, {counts['🟡']} warn, "
                 f"{counts['🔴']} fail, {counts['⚪']} skip)")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="One-stop mailbox stack healthcheck")
    p.add_argument("--hub", default=DEFAULT_HUB,
                   help=f"hub URL (default {DEFAULT_HUB})")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"path to mailbox.db (default {DEFAULT_DB})")
    p.add_argument("--attachments-dir", type=Path, default=DEFAULT_ATTACH)
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if any check is 🟡 or 🔴 (default exits 1 only on 🔴)")
    args = p.parse_args()

    checks = run_checks(args.hub, args.db, args.attachments_dir, args.backup_dir)

    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2, ensure_ascii=False))
    else:
        print(render_text(checks))

    has_fail = any(c.status == "🔴" for c in checks)
    has_warn = any(c.status == "🟡" for c in checks)
    if has_fail:
        return 1
    if args.strict and has_warn:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
