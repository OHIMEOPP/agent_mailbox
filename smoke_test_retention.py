"""Smoke test for mailbox retention sweep.

Tests run directly against mailbox_sweep functions (no server process needed
for most of these — sweep operates on the DB + attachments dir directly).

Test plan:
  1. Seed DB with mix of old/new read/unread messages + attachments
  2. --stats reflects seeded state
  3. --dry-run reports correct counters but writes nothing
  4. Sweep deletes correct rows + blobs
  5. Stale peers dropped
  6. Standalone orphan blob (no DB row) is cleaned up
  7. Shared blob (referenced by deleted + surviving message) is preserved
  8. Disabled flag (server-side test would require subprocess — skipped here;
     covered by docker-compose env wiring instead)
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Local import via path manipulation since this file is alongside the module
sys.path.insert(0, str(Path(__file__).parent))
import mailbox_sweep  # noqa: E402


def init_schema(db: Path) -> None:
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


def write_blob(attachments_dir: Path, data: bytes) -> tuple[str, int]:
    sha = hashlib.sha256(data).hexdigest()
    p = attachments_dir / sha[:2] / sha
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return sha, len(data)


def insert_msg(conn, from_name: str, to_name: str, body: str,
               days_old: int, read: bool) -> int:
    sent_at = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{days_old} days",),
    ).fetchone()[0]
    read_at = sent_at if read else None
    row = conn.execute(
        "INSERT INTO messages(from_name, to_name, body, sent_at, read_at, has_attachments) "
        "VALUES(?, ?, ?, ?, ?, 0) RETURNING id",
        (from_name, to_name, body, sent_at, read_at),
    ).fetchone()
    conn.commit()
    return row[0]


def attach(conn, msg_id: int, filename: str, sha: str, size: int) -> int:
    row = conn.execute(
        "INSERT INTO attachments(message_id, filename, mime, size, sha256) "
        "VALUES(?, ?, ?, ?, ?) RETURNING id",
        (msg_id, filename, "application/octet-stream", size, sha),
    ).fetchone()
    conn.execute(
        "UPDATE messages SET has_attachments=1 WHERE id=?",
        (msg_id,),
    )
    conn.commit()
    return row[0]


def insert_peer(conn, name: str, days_old: int) -> None:
    conn.execute(
        "INSERT INTO peers(name, last_seen_at) "
        "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))",
        (name, f"-{days_old} days"),
    )
    conn.commit()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-retention-smoke-"))
    db = workdir / "mailbox.db"
    attachments = workdir / "attachments"
    attachments.mkdir(parents=True)
    print(f"[smoke] workdir={workdir}")

    init_schema(db)
    conn = sqlite3.connect(str(db))

    # Seed:
    # m1: read, 10 days old (>7 read TTL) → DELETE
    # m2: read, 2 days old → KEEP
    # m3: unread, 20 days old (>14 unread TTL) → DELETE
    # m4: unread, 5 days old → KEEP
    # m5: read, 30 days old, with attachment shared with m6 → DELETE, blob KEPT
    # m6: unread, 1 day old, with same blob as m5 → KEEP, blob KEPT
    # m7: read, 30 days old, unique attachment → DELETE, blob DELETED
    m1 = insert_msg(conn, "hub", "spoke", "old read msg", days_old=10, read=True)
    m2 = insert_msg(conn, "hub", "spoke", "fresh read msg", days_old=2, read=True)
    m3 = insert_msg(conn, "hub", "spoke", "stale unread", days_old=20, read=False)
    m4 = insert_msg(conn, "hub", "spoke", "fresh unread", days_old=5, read=False)
    m5 = insert_msg(conn, "hub", "spoke", "old shared blob", days_old=30, read=True)
    m6 = insert_msg(conn, "hub", "spoke", "fresh shared blob", days_old=1, read=False)
    m7 = insert_msg(conn, "hub", "spoke", "old unique blob", days_old=30, read=True)

    shared_blob = b"SHARED" * 200
    unique_blob = b"UNIQUE" * 300

    sha_shared, size_shared = write_blob(attachments, shared_blob)
    sha_unique, size_unique = write_blob(attachments, unique_blob)

    attach(conn, m5, "shared.bin", sha_shared, size_shared)
    attach(conn, m6, "shared-from-fresh.bin", sha_shared, size_shared)
    attach(conn, m7, "unique.bin", sha_unique, size_unique)

    # Standalone orphan blob (no DB row at all) — should be picked up by Stage 6
    orphan_blob = b"ORPHAN" * 100
    sha_orphan, size_orphan = write_blob(attachments, orphan_blob)

    # Peers:
    # p1: heartbeat 60 days ago → DELETE
    # p2: heartbeat 1 day ago → KEEP
    insert_peer(conn, "stale-laptop", days_old=60)
    insert_peer(conn, "active-spoke", days_old=1)

    conn.close()

    failures: list[str] = []

    # --- Test 1: --stats reflects seeded state ---
    s = mailbox_sweep.stats(db, attachments)
    assert s["ok"] is True, f"stats ok=False: {s}"
    assert s["message_count"] == 7, f"expected 7 msgs, got {s['message_count']}"
    assert s["unread_count"] == 3, f"expected 3 unread, got {s['unread_count']}"
    assert s["attachment_count"] == 3
    assert s["blob_count"] == 3  # shared + unique + orphan
    assert s["peer_count"] == 2
    print(f"[smoke] stats ok: {s}")

    # --- Test 2: --dry-run reports counters, writes nothing ---
    counters_dry = mailbox_sweep.sweep_all(db, attachments, dry_run=True)
    assert counters_dry["read_messages_deleted"] == 3, \
        f"dry-run read count {counters_dry['read_messages_deleted']}"  # m1, m5, m7
    assert counters_dry["unread_messages_deleted"] == 1, \
        f"dry-run unread count"  # m3
    assert counters_dry["attachment_rows_deleted"] == 2  # m5's + m7's
    assert counters_dry["blobs_deleted"] == 1, \
        f"dry-run blobs (orphan from m7) got {counters_dry['blobs_deleted']}"
    assert counters_dry["standalone_orphan_blobs_deleted"] == 1, \
        f"standalone got {counters_dry['standalone_orphan_blobs_deleted']}"
    assert counters_dry["peer_rows_deleted"] == 1

    # Verify dry-run did NOT change disk
    s_after_dry = mailbox_sweep.stats(db, attachments)
    assert s_after_dry["message_count"] == 7, "dry-run shouldn't delete msgs"
    assert s_after_dry["blob_count"] == 3, "dry-run shouldn't delete blobs"
    print(f"[smoke] dry-run ok: {counters_dry}")

    # --- Test 3: real sweep ---
    counters = mailbox_sweep.sweep_all(db, attachments)
    assert counters == counters_dry, \
        f"dry-run / real counters differ:\n  dry:  {counters_dry}\n  real: {counters}"
    print(f"[smoke] sweep ok: {counters}")

    # --- Test 4: post-sweep DB state ---
    s_after = mailbox_sweep.stats(db, attachments)
    assert s_after["message_count"] == 3, \
        f"expected 3 surviving msgs (m2, m4, m6), got {s_after['message_count']}"
    assert s_after["unread_count"] == 2, f"expected 2 unread (m4, m6)"
    assert s_after["attachment_count"] == 1, \
        f"expected 1 attachment row (m6's), got {s_after['attachment_count']}"
    assert s_after["peer_count"] == 1

    # --- Test 5: shared blob preserved ---
    conn = sqlite3.connect(str(db))
    surviving_ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
    conn.close()
    assert surviving_ids == {m2, m4, m6}, f"surviving msgs: {surviving_ids}"
    assert (attachments / sha_shared[:2] / sha_shared).exists(), \
        "shared blob deleted but m6 still references it!"

    # --- Test 6: unique blob removed ---
    assert not (attachments / sha_unique[:2] / sha_unique).exists(), \
        "unique blob from deleted m7 should be gone"

    # --- Test 7: standalone orphan blob removed ---
    assert not (attachments / sha_orphan[:2] / sha_orphan).exists(), \
        "standalone orphan blob should be cleaned by Stage 6"
    print(f"[smoke] post-sweep state correct: {s_after}")

    # --- Test 8: re-run sweep on clean state is no-op ---
    counters_2 = mailbox_sweep.sweep_all(db, attachments)
    assert counters_2["read_messages_deleted"] == 0
    assert counters_2["unread_messages_deleted"] == 0
    assert counters_2["blobs_deleted"] == 0
    assert counters_2["standalone_orphan_blobs_deleted"] == 0
    print(f"[smoke] idempotent re-sweep ok")

    # --- Test 9: CLI subprocess test ---
    here = Path(__file__).parent
    result = subprocess.run(
        [sys.executable, str(here / "mailbox-retention.py"),
         "--db", str(db), "--stats", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    cli_stats = json.loads(result.stdout)
    assert cli_stats["message_count"] == 3
    print(f"[smoke] CLI --stats ok")

    # CLI --dry-run on clean state
    result = subprocess.run(
        [sys.executable, str(here / "mailbox-retention.py"),
         "--db", str(db), "--dry-run", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    cli_dry = json.loads(result.stdout)
    assert cli_dry["dry_run"] is True
    assert cli_dry["counters"]["read_messages_deleted"] == 0
    print(f"[smoke] CLI --dry-run ok")

    print(f"\n[smoke] ALL RETENTION TESTS PASSED")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if not failures else 1


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
