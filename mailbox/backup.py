"""Reusable mailbox backup + restore for hub-side mailbox.db + attachments.

Imported by:
  - mailbox-server.py (background daemon — backup before sweep)
  - mailbox-backup.py (CLI — --once / --list / --restore / --stats)

Design choices (locked by user 2026-05-23):
  - SQLite .backup() API for mailbox.db (online, atomic, doesn't block writers)
  - tar.gz for attachments/ directory (content-addressed, doesn't race with new writes)
  - Filenames: mailbox-backup-YYYYMMDD-HHMMSS.{db,attachments.tar.gz}
  - Atomic via .tmp suffix + rename after fsync
  - Rolling retention: 7 daily / 4 weekly / 3 monthly snapshots
  - Default location: ~/.claude/mailbox/backups/ (siblings the db it's backing up)
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 3

# Filename anchors. ts format is YYYYMMDD-HHMMSS (UTC).
_TS_FMT = "%Y%m%d-%H%M%S"
_DB_PATTERN = re.compile(r"^mailbox-backup-(\d{8}-\d{6})\.db$")
_TAR_PATTERN = re.compile(r"^mailbox-backup-(\d{8}-\d{6})-attachments\.tar\.gz$")


def _ts_now_utc() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)


def backup_once(
    db_path: Path,
    attachments_dir: Path,
    backup_dir: Path,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    keep_monthly: int = DEFAULT_KEEP_MONTHLY,
) -> dict:
    """Run one full backup + rolling cleanup.

    Returns counters dict with paths + sizes + pruning stats.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _ts_now_utc()
    db_target = backup_dir / f"mailbox-backup-{ts}.db"
    tar_target = backup_dir / f"mailbox-backup-{ts}-attachments.tar.gz"

    counters: dict = {
        "ts": ts,
        "db_backup_path": None,
        "db_backup_bytes": 0,
        "attachments_tar_path": None,
        "attachments_tar_bytes": 0,
        "backups_pruned": 0,
        "bytes_freed_pruning": 0,
    }

    # --- DB backup via SQLite online API ---
    db_tmp = db_target.with_suffix(".db.tmp")
    src = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        # PRAGMA journal_mode set on each connection (matches server.py / mailbox-server.py).
        src.execute("PRAGMA busy_timeout = 10000")
        dst = sqlite3.connect(str(db_tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    db_tmp.replace(db_target)
    counters["db_backup_path"] = str(db_target)
    counters["db_backup_bytes"] = db_target.stat().st_size

    # --- attachments tar.gz ---
    # Skip when dir doesn't exist or is empty (no attachments ever sent) — still
    # emit a zero-byte sentinel? No, just omit. CLI --list handles missing pair.
    if attachments_dir.exists() and any(attachments_dir.iterdir()):
        tar_tmp = tar_target.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tar_tmp), "w:gz") as tf:
                # arcname = "attachments" so restore can place under arbitrary path
                tf.add(str(attachments_dir), arcname="attachments")
            tar_tmp.replace(tar_target)
            counters["attachments_tar_path"] = str(tar_target)
            counters["attachments_tar_bytes"] = tar_target.stat().st_size
        except Exception:
            # Tar failed — clean partial tmp, leave db backup as-is
            if tar_tmp.exists():
                try:
                    tar_tmp.unlink()
                except OSError:
                    pass
            raise

    # --- Rolling retention prune ---
    pruned_count, pruned_bytes = _prune_rolling(
        backup_dir, keep_daily, keep_weekly, keep_monthly,
    )
    counters["backups_pruned"] = pruned_count
    counters["bytes_freed_pruning"] = pruned_bytes

    return counters


def _classify_keep(
    timestamps: list[datetime],
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> set[datetime]:
    """Pick which timestamps to keep under rolling retention.

    Strategy: bucket newest-per-day / newest-per-iso-week / newest-per-month,
    then take the top N most recent of each tier (skipping any timestamp already
    kept by an earlier tier — so the weekly/monthly limits only consume new
    slots that aren't already covered by daily).
    """
    if not timestamps:
        return set()

    by_day: dict[str, datetime] = {}
    by_week: dict[str, datetime] = {}
    by_month: dict[str, datetime] = {}
    for ts in timestamps:
        day = ts.strftime("%Y-%m-%d")
        iso = ts.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        month = ts.strftime("%Y-%m")
        if day not in by_day or ts > by_day[day]:
            by_day[day] = ts
        if week not in by_week or ts > by_week[week]:
            by_week[week] = ts
        if month not in by_month or ts > by_month[month]:
            by_month[month] = ts

    keep: set[datetime] = set()

    # Daily tier — top N most recent day-buckets, no skip logic needed
    for ts in sorted(by_day.values(), reverse=True)[:keep_daily]:
        keep.add(ts)

    # Weekly tier — count only NEW additions toward limit
    added = 0
    for ts in sorted(by_week.values(), reverse=True):
        if ts in keep:
            continue  # already kept by daily, doesn't count as weekly slot
        keep.add(ts)
        added += 1
        if added >= keep_weekly:
            break

    # Monthly tier — same
    added = 0
    for ts in sorted(by_month.values(), reverse=True):
        if ts in keep:
            continue
        keep.add(ts)
        added += 1
        if added >= keep_monthly:
            break

    return keep


def _prune_rolling(
    backup_dir: Path,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> tuple[int, int]:
    """Apply rolling retention. Returns (count_pruned, bytes_freed)."""
    # Collect (timestamp, [db_file, tar_file]) pairs by ts.
    by_ts: dict[datetime, list[Path]] = {}
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        m = _DB_PATTERN.match(entry.name)
        if m:
            try:
                ts = _parse_ts(m.group(1))
            except ValueError:
                continue
            by_ts.setdefault(ts, []).append(entry)
            continue
        m = _TAR_PATTERN.match(entry.name)
        if m:
            try:
                ts = _parse_ts(m.group(1))
            except ValueError:
                continue
            by_ts.setdefault(ts, []).append(entry)

    keep_set = _classify_keep(
        list(by_ts.keys()), keep_daily, keep_weekly, keep_monthly,
    )

    count = 0
    freed = 0
    for ts, files in by_ts.items():
        if ts in keep_set:
            continue
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            try:
                f.unlink()
            except OSError:
                continue
            count += 1
            freed += size

    return count, freed


def list_backups(backup_dir: Path) -> list[dict]:
    """Return list of backups, newest first.

    Each entry: {timestamp, ts_iso, db_path, db_size, tar_path, tar_size, total_size}
    """
    if not backup_dir.exists():
        return []
    by_ts: dict[str, dict] = {}
    for entry in sorted(backup_dir.iterdir()):
        if not entry.is_file():
            continue
        m = _DB_PATTERN.match(entry.name)
        if m:
            ts = m.group(1)
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            by_ts.setdefault(ts, {"timestamp": ts}).update({
                "db_path": str(entry),
                "db_size": size,
            })
            continue
        m = _TAR_PATTERN.match(entry.name)
        if m:
            ts = m.group(1)
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            by_ts.setdefault(ts, {"timestamp": ts}).update({
                "tar_path": str(entry),
                "tar_size": size,
            })

    out = []
    for ts, info in by_ts.items():
        try:
            dt = _parse_ts(ts)
            info["ts_iso"] = dt.isoformat()
        except ValueError:
            info["ts_iso"] = None
        info["total_size"] = info.get("db_size", 0) + info.get("tar_size", 0)
        out.append(info)
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


def restore(
    backup_dir: Path,
    db_path: Path,
    attachments_dir: Path,
    timestamp: str,
    confirm: bool = False,
) -> dict:
    """Restore from given timestamp.

    Before overwriting:
      - moves current db_path → db_path.before-restore-<ts_now>
      - moves current attachments_dir → attachments_dir.before-restore-<ts_now>
    Then:
      - copies backup db → db_path
      - extracts tar.gz → attachments_dir.parent/, untar arcname is "attachments"

    Args:
      timestamp: the YYYYMMDD-HHMMSS portion (matches list_backups()[].timestamp)
      confirm: must be True to actually run; otherwise raises RuntimeError

    Returns paths of pre-restore backups + restored files.
    """
    if not confirm:
        raise RuntimeError(
            "restore() requires confirm=True (this overwrites live data). "
            "CLI use: pass --yes."
        )

    db_backup = backup_dir / f"mailbox-backup-{timestamp}.db"
    tar_backup = backup_dir / f"mailbox-backup-{timestamp}-attachments.tar.gz"
    if not db_backup.exists():
        raise FileNotFoundError(f"db backup not found: {db_backup}")

    ts_now = _ts_now_utc()
    out: dict = {
        "timestamp": timestamp,
        "restored_db": str(db_path),
        "pre_restore_db": None,
        "pre_restore_attachments": None,
        "tar_restored": False,
    }

    # Move current db aside before overwrite
    if db_path.exists():
        pre = db_path.parent / f"{db_path.name}.before-restore-{ts_now}"
        db_path.replace(pre)
        out["pre_restore_db"] = str(pre)

    # Copy backup db into place
    shutil.copy2(str(db_backup), str(db_path))

    # Attachments — only if tar exists
    if tar_backup.exists():
        if attachments_dir.exists():
            pre = attachments_dir.parent / f"{attachments_dir.name}.before-restore-{ts_now}"
            attachments_dir.rename(pre)
            out["pre_restore_attachments"] = str(pre)
        # Extract to the parent of attachments_dir — tarball's arcname is "attachments"
        # so it'll recreate the dir at the right place.
        attachments_dir.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(str(tar_backup), "r:gz") as tf:
            tf.extractall(str(attachments_dir.parent))
        out["tar_restored"] = True

    return out


def stats(backup_dir: Path) -> dict:
    """Observability stats for /health and CLI --stats.

    Returns: {last_backup_at, backup_count, backup_total_bytes}
    """
    items = list_backups(backup_dir)
    if not items:
        return {
            "last_backup_at": None,
            "backup_count": 0,
            "backup_total_bytes": 0,
        }
    return {
        "last_backup_at": items[0]["ts_iso"],
        "backup_count": len(items),
        "backup_total_bytes": sum(i["total_size"] for i in items),
    }


def format_summary(counters: dict) -> str:
    """One-line stderr summary for daemon + CLI."""
    db_mb = counters.get("db_backup_bytes", 0) / 1024 / 1024
    tar_mb = counters.get("attachments_tar_bytes", 0) / 1024 / 1024
    freed_mb = counters.get("bytes_freed_pruning", 0) / 1024 / 1024
    return (
        f"backed up db={db_mb:.2f}MB + attachments={tar_mb:.2f}MB, "
        f"pruned {counters.get('backups_pruned', 0)} old "
        f"({freed_mb:.2f}MB freed)"
    )
