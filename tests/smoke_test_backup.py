"""Smoke test for mailbox backup feature.

Tests run directly against mailbox_backup module + the CLI as a subprocess.
No live server needed — backup operates on db + attachments_dir directly.

Test plan:
  1. backup_once round-trip: seeded db + blobs → .db restores schema/rows;
     tar.gz extracted matches original sha256s
  2. restore() preserves pre-restore data in <path>.before-restore-<ts>;
     restored db reflects the snapshot, not the post-snapshot mutation
  3. Rolling retention prunes correctly given a hand-crafted backup history
     (verifies 7 daily / 4 weekly / 3 monthly classification)
  4. CLI `mailbox-backup.py --list --json` subprocess returns parseable JSON
  5. MAILBOX_BACKUP_DISABLED env var on docker-compose — manual-verify only,
     left as a comment here (would require spinning the server container)
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Local import via path manipulation since this file is alongside the module
sys.path.insert(0, str(Path(__file__).parent.parent))
import mailbox_backup  # noqa: E402


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_messages(db: Path) -> int:
    """Open/close pattern — Windows holds file locks on leaked sqlite handles."""
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def init_db(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT NOT NULL,
            to_name TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            read_at TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE peers (
            name TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime TEXT,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """)
    conn.commit()
    conn.close()


def insert_msg(conn, body: str) -> int:
    row = conn.execute(
        "INSERT INTO messages(from_name, to_name, body, sent_at, read_at, has_attachments) "
        "VALUES('hub', 'spoke', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL, 0) "
        "RETURNING id",
        (body,),
    ).fetchone()
    conn.commit()
    return row[0]


def write_blob(attachments_dir: Path, data: bytes) -> tuple[str, Path]:
    sha = _sha256_bytes(data)
    p = attachments_dir / sha[:2] / sha
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return sha, p


def touch_fake_backup(backup_dir: Path, ts: datetime, kind: str = "db") -> Path:
    """Create a fake backup file at backup_dir with the given timestamp.

    kind: 'db' for the .db file, 'tar' for the attachments tar.gz.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts_str = ts.strftime("%Y%m%d-%H%M%S")
    if kind == "db":
        f = backup_dir / f"mailbox-backup-{ts_str}.db"
    elif kind == "tar":
        f = backup_dir / f"mailbox-backup-{ts_str}-attachments.tar.gz"
    else:
        raise ValueError(f"bad kind: {kind}")
    # Non-empty so size is countable but small
    f.write_bytes(b"FAKE-BACKUP-SENTINEL")
    return f


def test_1_round_trip(workdir: Path) -> None:
    """backup_once → verify db schema/rows + tar sha256 match."""
    print("\n[test 1] backup_once round-trip")
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    attachments.mkdir(parents=True)
    backup_dir = workdir / "backups"

    init_db(db)
    conn = sqlite3.connect(str(db))
    m1 = insert_msg(conn, "msg one")
    m2 = insert_msg(conn, "msg two")
    m3 = insert_msg(conn, "msg three")
    conn.close()
    assert m1 and m2 and m3

    blob_a = b"BLOB-CONTENT-A" * 50
    blob_b = b"BLOB-CONTENT-B" * 80
    sha_a, path_a = write_blob(attachments, blob_a)
    sha_b, path_b = write_blob(attachments, blob_b)

    counters = mailbox_backup.backup_once(db, attachments, backup_dir)
    assert counters["db_backup_path"] is not None, f"no db backup: {counters}"
    assert counters["db_backup_bytes"] > 0, f"empty db backup: {counters}"
    assert counters["attachments_tar_path"] is not None, \
        f"no tar.gz despite non-empty attachments: {counters}"
    assert counters["attachments_tar_bytes"] > 0
    assert counters["backups_pruned"] == 0, \
        f"first backup shouldn't prune: {counters}"

    # Open backup db, verify schema + rows survived
    backup_db = Path(counters["db_backup_path"])
    conn2 = sqlite3.connect(str(backup_db))
    schema = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"messages", "peers", "attachments"}.issubset(schema), \
        f"backup schema missing tables: {schema}"
    row_count = conn2.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert row_count == 3, f"backup row count {row_count} != 3"
    conn2.close()

    # Extract tar.gz, verify sha256 of each blob
    extract_dir = workdir / "extracted"
    extract_dir.mkdir()
    with tarfile.open(counters["attachments_tar_path"], "r:gz") as tf:
        tf.extractall(str(extract_dir))
    # Tarball arcname = "attachments", so extracted path is extract_dir/attachments/
    ex_a = extract_dir / "attachments" / sha_a[:2] / sha_a
    ex_b = extract_dir / "attachments" / sha_b[:2] / sha_b
    assert ex_a.exists(), f"extracted blob A missing: {ex_a}"
    assert ex_b.exists(), f"extracted blob B missing: {ex_b}"
    assert _sha256_file(ex_a) == sha_a, "blob A sha256 mismatch after restore"
    assert _sha256_file(ex_b) == sha_b, "blob B sha256 mismatch after restore"

    print(f"  db rows={row_count} schema={schema}")
    print(f"  tar sha256 verified for {sha_a[:12]}.. and {sha_b[:12]}..")


def test_2_restore_preserves_pre(workdir: Path) -> None:
    """restore() moves current state aside + restores snapshot."""
    print("\n[test 2] restore with .before-restore-* preservation")
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    attachments.mkdir(parents=True)
    backup_dir = workdir / "backups"

    # Snapshot A: 3 rows + 1 blob
    init_db(db)
    conn = sqlite3.connect(str(db))
    for body in ("a", "b", "c"):
        insert_msg(conn, body)
    conn.close()
    sha_a, _ = write_blob(attachments, b"BLOB-A" * 100)

    counters = mailbox_backup.backup_once(db, attachments, backup_dir)
    snapshot_ts = counters["ts"]

    # Mutate to State B: add row + add new blob
    conn = sqlite3.connect(str(db))
    insert_msg(conn, "post-snapshot row")
    conn.close()
    sha_b, _ = write_blob(attachments, b"BLOB-B-NEW" * 100)
    pre_rows = _count_messages(db)
    assert pre_rows == 4, f"expected 4 rows in State B, got {pre_rows}"

    # Restore snapshot A
    out = mailbox_backup.restore(backup_dir, db, attachments, snapshot_ts,
                                  confirm=True)
    assert out["pre_restore_db"], f"no pre_restore_db in output: {out}"
    assert out["pre_restore_attachments"], \
        f"no pre_restore_attachments in output: {out}"
    assert out["tar_restored"] is True, f"tar not restored: {out}"

    # Verify db reverted to 3 rows
    post_rows = _count_messages(db)
    assert post_rows == 3, f"restored db has {post_rows} rows, expected 3"

    # Verify .before-restore-* contains the 4-row State B
    pre_db = Path(out["pre_restore_db"])
    assert pre_db.exists(), f"pre-restore db missing: {pre_db}"
    pre_count = _count_messages(pre_db)
    assert pre_count == 4, f"pre-restore db has {pre_count} rows, expected 4"

    # Verify .before-restore-* attachments dir has the new blob
    pre_att = Path(out["pre_restore_attachments"])
    assert pre_att.exists(), f"pre-restore attachments dir missing: {pre_att}"
    assert (pre_att / sha_b[:2] / sha_b).exists(), \
        "pre-restore attachments missing State-B-only blob"

    # Restored attachments dir should NOT have the State-B blob, but should
    # have the State-A blob
    assert (attachments / sha_a[:2] / sha_a).exists(), \
        "State-A blob missing from restored attachments"
    assert not (attachments / sha_b[:2] / sha_b).exists(), \
        "State-B blob should not exist in restored snapshot"

    print(f"  db reverted 4→3 rows; pre-restore preserved at {pre_db.name}")

    # confirm=False guard
    try:
        mailbox_backup.restore(backup_dir, db, attachments, snapshot_ts)
    except RuntimeError as e:
        assert "confirm=True" in str(e)
    else:
        raise AssertionError("restore() without confirm should raise")
    print("  confirm=False guard ok")


def test_3_rolling_retention(workdir: Path) -> None:
    """Verify 7 daily / 4 weekly / 3 monthly classification.

    Lays down 8 daily-spaced + 5 weekly-spaced + 4 monthly-spaced fake .db
    backups, plus 1 live backup_once, then asserts which timestamps survive.
    """
    print("\n[test 3] rolling retention 7/4/3")
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"  # left empty → no tar generated
    attachments.mkdir(parents=True)
    backup_dir = workdir / "backups"

    init_db(db)  # minimal valid db for backup_once

    # 8 daily candidates: 2024-01-01..08, each at noon UTC, distinct days
    daily_ts = [datetime(2024, 1, d, 12, 0, 0, tzinfo=timezone.utc)
                for d in range(1, 9)]
    # 5 weekly candidates: each Monday going back, 5 distinct ISO weeks
    weekly_ts = [datetime(2023, 12, 25, 12, 0, 0, tzinfo=timezone.utc),
                 datetime(2023, 12, 18, 12, 0, 0, tzinfo=timezone.utc),
                 datetime(2023, 12, 11, 12, 0, 0, tzinfo=timezone.utc),
                 datetime(2023, 12,  4, 12, 0, 0, tzinfo=timezone.utc),
                 datetime(2023, 11, 27, 12, 0, 0, tzinfo=timezone.utc)]
    # 4 monthly candidates: each mid-month, 4 distinct earlier months
    monthly_ts = [datetime(2023, 10, 15, 12, 0, 0, tzinfo=timezone.utc),
                  datetime(2023,  9, 15, 12, 0, 0, tzinfo=timezone.utc),
                  datetime(2023,  8, 15, 12, 0, 0, tzinfo=timezone.utc),
                  datetime(2023,  7, 15, 12, 0, 0, tzinfo=timezone.utc)]

    all_fake = daily_ts + weekly_ts + monthly_ts
    for ts in all_fake:
        touch_fake_backup(backup_dir, ts, kind="db")

    # Now run a live backup. backup_once adds 1 new .db (no tar since empty
    # attachments) then prunes.
    counters = mailbox_backup.backup_once(db, attachments, backup_dir)
    new_ts_str = counters["ts"]
    assert counters["attachments_tar_path"] is None, \
        "empty attachments should produce no tar"

    # Expected keep set: top 7 daily buckets (now + 2024-01-08..03),
    # plus 4 weekly (2023-12-25, -18, -11, -04),
    # plus 3 monthly (2023-11-27, 2023-10-15, 2023-09-15).
    # = 14 unique. From input 8+5+4=17 fakes + 1 new = 18, prune drops 4.
    assert counters["backups_pruned"] == 4, \
        f"expected 4 pruned, got {counters['backups_pruned']}\n  counters={counters}"

    surviving = sorted(p.name for p in backup_dir.iterdir() if p.is_file())
    expected_kept_stems = [
        f"mailbox-backup-{new_ts_str}.db",
        # daily tier survivors: 2024-01-03..08 (top 6 of 8 distinct days; -01, -02 dropped)
        "mailbox-backup-20240108-120000.db",
        "mailbox-backup-20240107-120000.db",
        "mailbox-backup-20240106-120000.db",
        "mailbox-backup-20240105-120000.db",
        "mailbox-backup-20240104-120000.db",
        "mailbox-backup-20240103-120000.db",
        # weekly tier survivors
        "mailbox-backup-20231225-120000.db",
        "mailbox-backup-20231218-120000.db",
        "mailbox-backup-20231211-120000.db",
        "mailbox-backup-20231204-120000.db",
        # monthly tier survivors
        "mailbox-backup-20231127-120000.db",
        "mailbox-backup-20231015-120000.db",
        "mailbox-backup-20230915-120000.db",
    ]
    expected_dropped = [
        "mailbox-backup-20240102-120000.db",
        "mailbox-backup-20240101-120000.db",
        "mailbox-backup-20230815-120000.db",
        "mailbox-backup-20230715-120000.db",
    ]
    for name in expected_kept_stems:
        assert name in surviving, f"expected kept {name} but gone\n  surviving={surviving}"
    for name in expected_dropped:
        assert name not in surviving, \
            f"expected dropped {name} but still present\n  surviving={surviving}"

    print(f"  pruned {counters['backups_pruned']}; "
          f"kept {len(surviving)} = 7 daily + 4 weekly + 3 monthly")


def test_4_cli_list_json(workdir: Path) -> None:
    """CLI mailbox-backup.py --list --json returns parseable JSON."""
    print("\n[test 4] CLI --list --json subprocess")
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    attachments.mkdir(parents=True)
    backup_dir = workdir / "backups"

    init_db(db)
    conn = sqlite3.connect(str(db))
    insert_msg(conn, "for CLI test")
    conn.close()

    # Seed one backup via module (so list has something to show)
    counters = mailbox_backup.backup_once(db, attachments, backup_dir)
    assert counters["db_backup_path"]

    here = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, str(here / "mailbox-backup.py"),
         "--list", "--json",
         "--db", str(db),
         "--attachments-dir", str(attachments),
         "--backup-dir", str(backup_dir)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, \
        f"CLI failed (rc={result.returncode}):\n  stdout={result.stdout}\n  stderr={result.stderr}"
    payload = json.loads(result.stdout)
    assert isinstance(payload, list), f"expected list, got {type(payload)}"
    assert len(payload) >= 1, f"expected ≥1 backup, got {payload}"
    item = payload[0]
    for k in ("timestamp", "db_path", "db_size", "total_size"):
        assert k in item, f"missing key {k} in {item}"
    print(f"  CLI returned {len(payload)} entries; first ts={item['timestamp']}")

    # Also smoke-test --stats --json
    result = subprocess.run(
        [sys.executable, str(here / "mailbox-backup.py"),
         "--stats", "--json",
         "--db", str(db),
         "--backup-dir", str(backup_dir)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"--stats failed: {result.stderr}"
    s = json.loads(result.stdout)
    assert s["backup_count"] >= 1
    assert s["last_backup_at"] is not None
    print(f"  --stats: count={s['backup_count']} last={s['last_backup_at']}")


def test_5_docker_env_var_skip_with_note() -> None:
    """MAILBOX_BACKUP_DISABLED=1 server kill-switch — manual verify only.

    Verifying this would require spinning the docker-compose stack with the
    env var set, then asserting that the backup daemon thread is NOT created
    (see mailbox-server.py lines 712-734). Skipped here; covered in
    SETUP-CROSS-DEVICE.md Phase 6 manual verification checklist.
    """
    print("\n[test 5] docker env-var verify: skipped (manual; see SETUP-CROSS-DEVICE.md)")


def test_6_cli_relative_time_flags(workdir: Path) -> None:
    """--list --since X / --restore now-X relative time syntax."""
    print("\n[test 6] CLI relative-time flags (--since / --restore now-X)")
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    attachments.mkdir(parents=True)
    backup_dir = workdir / "backups"

    init_db(db)
    conn = sqlite3.connect(str(db))
    insert_msg(conn, "x")
    conn.close()

    # Lay down 3 fake backups: 5h ago, 2d ago, 10d ago
    now = datetime.now(timezone.utc)
    for hours_ago, label in [(5, "5h_ago"), (48, "2d_ago"), (240, "10d_ago")]:
        ts = now - timedelta(hours=hours_ago)
        touch_fake_backup(backup_dir, ts, kind="db")

    here = Path(__file__).parent.parent
    cli = str(here / "mailbox-backup.py")

    # --list --since 24h → only the 5h_ago backup
    r = subprocess.run(
        [sys.executable, cli, "--list", "--since", "24h", "--json",
         "--db", str(db), "--backup-dir", str(backup_dir),
         "--attachments-dir", str(attachments)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"--list --since failed: {r.stderr}"
    items = json.loads(r.stdout)
    assert len(items) == 1, \
        f"--since 24h should return 1 backup (5h_ago), got {len(items)}: {items}"

    # --list --since 7d → 5h_ago + 2d_ago
    r = subprocess.run(
        [sys.executable, cli, "--list", "--since", "7d", "--json",
         "--db", str(db), "--backup-dir", str(backup_dir),
         "--attachments-dir", str(attachments)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    items = json.loads(r.stdout)
    assert len(items) == 2, f"--since 7d should return 2 backups, got {len(items)}"

    # Bad --since value
    r = subprocess.run(
        [sys.executable, cli, "--list", "--since", "wat",
         "--db", str(db), "--backup-dir", str(backup_dir),
         "--attachments-dir", str(attachments)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 2 and "bad --since" in r.stderr

    # --restore now-3d (without --yes → dry-run, exits 2 but extracts the
    # newest backup older than 3d, which is 10d_ago)
    r = subprocess.run(
        [sys.executable, cli, "--restore", "now-3d",
         "--db", str(db), "--backup-dir", str(backup_dir),
         "--attachments-dir", str(attachments)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 2, f"dry-run --restore should return 2, got {r.returncode}"
    # Stderr should mention the resolved timestamp (10d_ago)
    ten_days_ts = (now - timedelta(hours=240)).strftime("%Y%m%d-%H%M%S")
    assert ten_days_ts in r.stderr, \
        f"expected resolved ts {ten_days_ts} in stderr, got: {r.stderr}"

    # --restore now-99d when no backup that old → not_found
    r = subprocess.run(
        [sys.executable, cli, "--restore", "now-99d",
         "--db", str(db), "--backup-dir", str(backup_dir),
         "--attachments-dir", str(attachments)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 2
    assert ("invalid --restore" in r.stderr or "restore failed" in r.stderr
            or "no" in r.stderr.lower()), \
        f"expected resolution-fail message, got: {r.stderr}"

    print(f"  --since 24h→1, --since 7d→2, --restore now-3d resolves to {ten_days_ts}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-backup-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        # Each test gets its own subdirectory so they don't clobber each other
        for i, fn in enumerate((test_1_round_trip,
                                 test_2_restore_preserves_pre,
                                 test_3_rolling_retention,
                                 test_4_cli_list_json), start=1):
            sub = workdir / f"t{i}"
            sub.mkdir()
            fn(sub)
        test_5_docker_env_var_skip_with_note()
        sub6 = workdir / "t6"
        sub6.mkdir()
        test_6_cli_relative_time_flags(sub6)
    finally:
        # Best-effort cleanup
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL BACKUP TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(3)
