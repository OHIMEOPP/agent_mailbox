"""Sliding-window rate limiter for mailbox-server endpoints.

Per-scope (per-from-name or per-token-prefix) sliding window with 1-minute
buckets. Lets the hub reject obvious abuse / runaway agents without
needing in-memory state — the DB is the source of truth, restart-safe.

Imported by:
  - mailbox-server.py (`check_and_consume()` pre-flight in /send /inbox /react)
  - mailbox-rate-limit.py (CLI: --stats / --top / --reset / --json)
  - smoke_test_rate_limit.py

Design:
  - One row per (scope_key, current_minute_bucket).
  - `check_and_consume(db, scope_key, limit_per_min)` does:
      1. compute current minute = floor(now / 60)
      2. UPSERT row for (scope_key, current_minute), incrementing count
      3. SUM counts over last 1-minute window (current + previous bucket
         pro-rated by how much of the previous still falls inside 60s)
      4. If sum >= limit, return False (reject).
  - Pure SQLite — no in-memory hot path. Trade ~ms latency for restart-safety
    and audit-trail. At mailbox-server throughput this is irrelevant.
  - Retention: buckets older than RATE_LIMIT_BUCKET_TTL_HOURS = 1 are pruned
    by `prune_old_buckets()` (cheap; runs daily-ish or on demand via CLI).

Default limit: 120 requests/minute per scope. Override via
`MAILBOX_RATE_LIMIT_PER_MIN` env. Kill-switch: `MAILBOX_RATE_LIMIT_DISABLED=1`
makes check_and_consume always return True without touching the DB.

Scope key conventions (callers pick):
  - "from:<name>"            — most common; the from_name on /send
  - "ip:<client-ip>"         — fallback for endpoints without a from field
  - "token:<hashed-prefix>"  — anonymous receiver rate limiting

Doesn't enforce a hierarchy — callers pick one scope_key per request.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_LIMIT_PER_MIN = 120
RATE_LIMIT_BUCKET_TTL_HOURS = 1


def _is_disabled() -> bool:
    return os.environ.get("MAILBOX_RATE_LIMIT_DISABLED", "").strip() in ("1", "true", "yes")


def _configured_limit() -> int:
    try:
        return int(os.environ.get("MAILBOX_RATE_LIMIT_PER_MIN", str(DEFAULT_LIMIT_PER_MIN)))
    except ValueError:
        return DEFAULT_LIMIT_PER_MIN


def init_schema(db_path: Path) -> None:
    """Idempotent DDL."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                scope_key       TEXT NOT NULL,
                minute_bucket   INTEGER NOT NULL,
                count           INTEGER NOT NULL DEFAULT 0,
                last_request_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (scope_key, minute_bucket)
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket
                ON rate_limit_buckets(minute_bucket);
        """)
        conn.commit()
    finally:
        conn.close()


def hash_token(token: str) -> str:
    """Return 16-char hex prefix of SHA256(token). Use in scope_key='token:<hash>'.

    Never log the raw token — this gives an actor identifier without leaking
    the secret if the rate_limit_buckets table is ever exfiltrated.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def check_and_consume(
    db_path: Path,
    scope_key: str,
    limit_per_min: int | None = None,
) -> tuple[bool, dict]:
    """Atomically check the sliding window and increment.

    Returns (allowed, info). `info` always has:
      - count_current_minute
      - count_previous_minute
      - effective_count (sliding-window-weighted)
      - limit
      - retry_after_seconds (0 if allowed; else seconds until window has space)

    `allowed=True` means the request is within budget; caller proceeds.
    `allowed=False` means the request should be rejected (429); the counter
    is STILL incremented (so spam attempts also count against budget) —
    this is deliberate, prevents a busy-loop attacker from getting free
    requests once they've exceeded budget.
    """
    if _is_disabled():
        return True, {
            "count_current_minute": 0,
            "count_previous_minute": 0,
            "effective_count": 0,
            "limit": -1,
            "retry_after_seconds": 0,
            "disabled": True,
        }

    if limit_per_min is None:
        limit_per_min = _configured_limit()

    now = time.time()
    current_bucket = int(now // 60)
    previous_bucket = current_bucket - 1
    # How far into the current minute are we (0..1)
    elapsed_fraction = (now % 60) / 60.0
    # Sliding window weight on previous bucket: (1 - elapsed_fraction)
    prev_weight = 1.0 - elapsed_fraction

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        # UPSERT current bucket count by 1
        conn.execute(
            "INSERT INTO rate_limit_buckets(scope_key, minute_bucket, count) "
            "VALUES(?, ?, 1) "
            "ON CONFLICT(scope_key, minute_bucket) DO UPDATE SET "
            "count = count + 1, "
            "last_request_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (scope_key, current_bucket),
        )
        # Read current + previous counts
        row = conn.execute(
            "SELECT minute_bucket, count FROM rate_limit_buckets "
            "WHERE scope_key = ? AND minute_bucket IN (?, ?)",
            (scope_key, current_bucket, previous_bucket),
        ).fetchall()
        cur_count = 0
        prev_count = 0
        for r in row:
            if r[0] == current_bucket:
                cur_count = r[1]
            elif r[0] == previous_bucket:
                prev_count = r[1]
        conn.commit()
    finally:
        conn.close()

    effective = cur_count + prev_count * prev_weight
    allowed = effective <= limit_per_min
    # Retry-after: time until the current bucket is "old enough" that adding
    # the next request would put effective < limit. Conservative estimate:
    # wait until current bucket becomes previous bucket (= 60 - now%60).
    retry_after = 0
    if not allowed:
        retry_after = int(60 - (now % 60)) + 1

    return allowed, {
        "count_current_minute": cur_count,
        "count_previous_minute": prev_count,
        "effective_count": round(effective, 2),
        "limit": limit_per_min,
        "retry_after_seconds": retry_after,
        "disabled": False,
    }


def prune_old_buckets(db_path: Path, hours: int = RATE_LIMIT_BUCKET_TTL_HOURS) -> int:
    """Delete buckets older than `hours` hours. Called by retention sweep or CLI."""
    cutoff_bucket = int(time.time() // 60) - hours * 60
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        cur = conn.execute(
            "DELETE FROM rate_limit_buckets WHERE minute_bucket < ?",
            (cutoff_bucket,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability for /health + CLI."""
    out = {
        "rate_limit_active_scopes": 0,
        "rate_limit_buckets_total": 0,
        "rate_limit_limit_per_min": _configured_limit(),
        "rate_limit_disabled": _is_disabled(),
    }
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            current_bucket = int(time.time() // 60)
            recent_window = current_bucket - 5  # 5-min lookback for "active"
            out["rate_limit_active_scopes"] = conn.execute(
                "SELECT COUNT(DISTINCT scope_key) FROM rate_limit_buckets "
                "WHERE minute_bucket >= ?",
                (recent_window,),
            ).fetchone()[0]
            out["rate_limit_buckets_total"] = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_buckets"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()


def top_scopes(db_path: Path, limit: int = 20) -> list[dict]:
    """Top scope_keys by recent-window count. For CLI --top."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            current_bucket = int(time.time() // 60)
            window = current_bucket - 5  # 5-min lookback
            rows = conn.execute(
                "SELECT scope_key, SUM(count) AS recent_count, "
                "MAX(last_request_at) AS last_seen "
                "FROM rate_limit_buckets "
                "WHERE minute_bucket >= ? "
                "GROUP BY scope_key "
                "ORDER BY recent_count DESC LIMIT ?",
                (window, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reset_scope(db_path: Path, scope_key: str) -> int:
    """Manual override — wipe a scope's recent buckets so it can resume."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        cur = conn.execute(
            "DELETE FROM rate_limit_buckets WHERE scope_key = ?",
            (scope_key,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
