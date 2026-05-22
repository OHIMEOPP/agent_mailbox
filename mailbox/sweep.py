"""Reusable retention sweep + stats for mailbox.

Imported by:
  - mailbox-server.py (background daemon thread, daily tick)
  - mailbox-retention.py (CLI: --once / --dry-run / --stats)

Naming: hyphenless module so Python can `from mailbox_sweep import ...`.
The hub script (mailbox-server.py) has a hyphen by historical convention and
is not importable; this module avoids that constraint.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_READ_DAYS = 7      # read messages older than this → delete
DEFAULT_UNREAD_DAYS = 14   # unread messages older than this → delete
DEFAULT_PEER_DAYS = 30     # peer heartbeat older than this → drop row


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def sweep_all(
    db_path: Path,
    attachments_dir: Path,
    read_days: int = DEFAULT_READ_DAYS,
    unread_days: int = DEFAULT_UNREAD_DAYS,
    peer_days: int = DEFAULT_PEER_DAYS,
    dry_run: bool = False,
) -> dict:
    """Run retention sweep across messages / attachments / blobs / peers.

    Order:
      1. Find message ids past their TTL — three buckets:
           a. Explicit per-message TTL: `expires_at < now` (overrides read/unread cutoffs)
           b. Default read TTL: read messages older than read_days
           c. Default unread TTL: unread messages older than unread_days
      2. Snapshot sha256 set of attachments owned by those messages
      3. Delete attachment rows → delete message rows (in one transaction)
      4. For each snapshot sha, delete blob file if no other attachment now references it
      5. Drop stale peer rows
      6. Standalone orphan blob scan — any on-disk blob whose sha is not in the
         attachments table at all (defense against past inconsistency)

    Returns a counters dict; safe to call from threads (uses its own connection).
    With dry_run=True, reports counts but writes nothing.
    """
    counters: dict[str, int] = {
        "read_messages_deleted": 0,
        "unread_messages_deleted": 0,
        "expired_messages_deleted": 0,
        "attachment_rows_deleted": 0,
        "blobs_deleted": 0,
        "blob_bytes_freed": 0,
        "peer_rows_deleted": 0,
        "standalone_orphan_blobs_deleted": 0,
    }

    conn = _connect(db_path)
    try:
        # --- Stage 1: identify messages to delete ---
        # sent_at is ISO 8601 with Z; lex-compare matches chronological order.
        cutoff_read = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{read_days} days",),
        ).fetchone()[0]
        cutoff_unread = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{unread_days} days",),
        ).fetchone()[0]
        cutoff_peer = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{peer_days} days",),
        ).fetchone()[0]
        now_ts = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]

        # 1a. Explicit per-message TTL — applies regardless of read state.
        # Wrapped in try/except so deployment against pre-TTL schemas
        # (expires_at column not yet present) degrades silently to 0 expired.
        try:
            expired_ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_ts,),
            ).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such column" in str(e).lower():
                expired_ids = []
            else:
                raise
        expired_set = set(expired_ids)

        # 1b/c. Default read/unread cutoffs, excluding rows already flagged by
        # explicit TTL and excluding pinned messages (pin = "keep indefinitely").
        # COALESCE handles pre-v006 schemas where pinned column doesn't exist.
        try:
            read_ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages "
                "WHERE read_at IS NOT NULL AND sent_at < ? "
                "AND COALESCE(pinned, 0) = 0",
                (cutoff_read,),
            ).fetchall() if r[0] not in expired_set]
            unread_ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages "
                "WHERE read_at IS NULL AND sent_at < ? "
                "AND COALESCE(pinned, 0) = 0",
                (cutoff_unread,),
            ).fetchall() if r[0] not in expired_set]
        except sqlite3.OperationalError as e:
            if "no such column" in str(e).lower():
                # Pre-v006 schema — fall back without pin filter.
                read_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM messages WHERE read_at IS NOT NULL AND sent_at < ?",
                    (cutoff_read,),
                ).fetchall() if r[0] not in expired_set]
                unread_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM messages WHERE read_at IS NULL AND sent_at < ?",
                    (cutoff_unread,),
                ).fetchall() if r[0] not in expired_set]
            else:
                raise
        all_ids = expired_ids + read_ids + unread_ids
        counters["read_messages_deleted"] = len(read_ids)
        counters["unread_messages_deleted"] = len(unread_ids)
        counters["expired_messages_deleted"] = len(expired_ids)

        # --- Stage 2: snapshot candidate-orphan shas ---
        candidate_shas: set[str] = set()
        if all_ids:
            placeholders = ",".join("?" * len(all_ids))
            for r in conn.execute(
                f"SELECT DISTINCT sha256 FROM attachments WHERE message_id IN ({placeholders})",
                all_ids,
            ):
                candidate_shas.add(r[0])

            # --- Stage 3: delete attachment + message rows ---
            if dry_run:
                # Count without deleting
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM attachments WHERE message_id IN ({placeholders})",
                    all_ids,
                ).fetchone()
                counters["attachment_rows_deleted"] = cur[0]
            else:
                cur = conn.execute(
                    f"DELETE FROM attachments WHERE message_id IN ({placeholders})",
                    all_ids,
                )
                counters["attachment_rows_deleted"] = cur.rowcount
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",
                    all_ids,
                )

        # --- Stage 4: blob orphan handling for just-deleted messages ---
        # Check each candidate sha: still referenced by any attachment NOT in
        # the to-delete set? Using NOT IN works for both modes:
        #   - real mode: rows already deleted, NOT IN is redundant but correct
        #   - dry-run: rows still present, NOT IN excludes them so the orphan
        #     prediction matches what a real run would produce.
        if candidate_shas and all_ids:
            placeholders = ",".join("?" * len(all_ids))
            for sha in candidate_shas:
                still_ref = conn.execute(
                    f"SELECT 1 FROM attachments WHERE sha256=? "
                    f"AND message_id NOT IN ({placeholders}) LIMIT 1",
                    [sha, *all_ids],
                ).fetchone()
                if still_ref:
                    continue
                blob = attachments_dir / sha[:2] / sha
                if blob.exists():
                    try:
                        size = blob.stat().st_size
                    except OSError:
                        continue
                    counters["blobs_deleted"] += 1
                    counters["blob_bytes_freed"] += size
                    if not dry_run:
                        try:
                            blob.unlink()
                        except OSError:
                            pass

        # --- Stage 5: stale peer rows ---
        stale_peers = [r[0] for r in conn.execute(
            "SELECT name FROM peers WHERE last_seen_at < ?",
            (cutoff_peer,),
        ).fetchall()]
        counters["peer_rows_deleted"] = len(stale_peers)
        if stale_peers and not dry_run:
            placeholders = ",".join("?" * len(stale_peers))
            conn.execute(
                f"DELETE FROM peers WHERE name IN ({placeholders})",
                stale_peers,
            )

        if not dry_run:
            conn.commit()

        # --- Stage 6: standalone orphan blob scan ---
        # Always run (even dry-run), independent of message deletes above.
        # Catches anything that became orphan in prior runs but wasn't cleaned.
        if attachments_dir.exists():
            for sha_dir in attachments_dir.iterdir():
                if not sha_dir.is_dir() or len(sha_dir.name) != 2:
                    continue
                for blob in sha_dir.iterdir():
                    if not blob.is_file():
                        continue
                    sha = blob.name
                    if len(sha) != 64:  # not a sha256 file (probably .tmp)
                        continue
                    if sha in candidate_shas:
                        # Already counted in Stage 4 if orphan, or kept if still ref'd
                        continue
                    still_ref = conn.execute(
                        "SELECT 1 FROM attachments WHERE sha256=? LIMIT 1",
                        (sha,),
                    ).fetchone()
                    if still_ref:
                        continue
                    try:
                        size = blob.stat().st_size
                    except OSError:
                        continue
                    counters["standalone_orphan_blobs_deleted"] += 1
                    counters["blob_bytes_freed"] += size
                    if not dry_run:
                        try:
                            blob.unlink()
                        except OSError:
                            pass
    finally:
        conn.close()

    return counters


def format_summary(counters: dict) -> str:
    """One-line stderr summary used by both daemon and CLI."""
    bytes_freed = counters.get("blob_bytes_freed", 0)
    mb = bytes_freed / 1024 / 1024
    expired = counters.get("expired_messages_deleted", 0)
    expired_clause = f"{expired} expired / " if expired else ""
    return (
        f"deleted {expired_clause}"
        f"{counters['read_messages_deleted']} read / "
        f"{counters['unread_messages_deleted']} unread messages, "
        f"{counters['attachment_rows_deleted']} attach rows, "
        f"{counters['blobs_deleted'] + counters['standalone_orphan_blobs_deleted']} blobs "
        f"({mb:.2f} MB freed), "
        f"{counters['peer_rows_deleted']} stale peers"
    )


def stats(db_path: Path, attachments_dir: Path) -> dict:
    """Observability stats. Used by /health and CLI --stats.

    Wraps any DB error in ok=False; disk scan is best-effort.
    """
    out: dict = {
        "ok": True,
        "unread_count": 0,
        "message_count": 0,
        "attachment_count": 0,
        "blob_count": 0,
        "blob_total_bytes": 0,
        "oldest_message_age_days": None,
        "peer_count": 0,
        "ttl_expiring_24h": 0,
        "ttl_expired_pending_sweep": 0,
    }
    try:
        conn = _connect(db_path)
        try:
            out["unread_count"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE read_at IS NULL"
            ).fetchone()[0]
            out["message_count"] = conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            out["attachment_count"] = conn.execute(
                "SELECT COUNT(*) FROM attachments"
            ).fetchone()[0]
            out["peer_count"] = conn.execute(
                "SELECT COUNT(*) FROM peers"
            ).fetchone()[0]
            oldest_row = conn.execute(
                "SELECT MIN(sent_at) FROM messages"
            ).fetchone()
            oldest = oldest_row[0] if oldest_row else None
            if oldest:
                age_row = conn.execute(
                    "SELECT julianday('now') - julianday(?)",
                    (oldest,),
                ).fetchone()
                out["oldest_message_age_days"] = round(age_row[0], 2)
            # TTL stats — degrade silently if expires_at column not yet present.
            try:
                out["ttl_expiring_24h"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE expires_at IS NOT NULL "
                    "AND expires_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','+24 hours')"
                ).fetchone()[0]
                out["ttl_expired_pending_sweep"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE expires_at IS NOT NULL "
                    "AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                ).fetchone()[0]
            except sqlite3.OperationalError as e:
                if "no such column" not in str(e).lower():
                    raise
        finally:
            conn.close()
    except sqlite3.Error as e:
        out["ok"] = False
        out["error"] = f"db error: {e}"

    # Disk scan — best effort
    try:
        if attachments_dir.exists():
            for sha_dir in attachments_dir.iterdir():
                if not sha_dir.is_dir() or len(sha_dir.name) != 2:
                    continue
                for blob in sha_dir.iterdir():
                    if not blob.is_file() or len(blob.name) != 64:
                        continue
                    out["blob_count"] += 1
                    try:
                        out["blob_total_bytes"] += blob.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass

    return out
