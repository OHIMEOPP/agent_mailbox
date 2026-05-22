"""Smoke test for mailbox_rate_limit.

Test plan:
  1. init_schema idempotent + table+index shape
  2. check_and_consume under limit → all allowed
  3. check_and_consume exceeds limit → returns False with retry_after > 0
  4. Different scope_keys are independent
  5. MAILBOX_RATE_LIMIT_DISABLED=1 always allows
  6. prune_old_buckets cleans rows older than N hours
  7. top_scopes returns most-active scopes sorted desc
  8. reset_scope wipes a specific scope's buckets
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from mailbox import rate_limit as mailbox_rate_limit  # noqa: E402


def test_1_ddl_idempotent(db: Path) -> None:
    print("\n[test 1] init_schema idempotent + table+index shape")
    mailbox_rate_limit.init_schema(db)
    mailbox_rate_limit.init_schema(db)  # no-op

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "rate_limit_buckets" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rate_limit_buckets)")}
        for c in ("scope_key", "minute_bucket", "count", "last_request_at"):
            assert c in cols, f"missing column {c}"
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='rate_limit_buckets'"
        )}
        assert "idx_rate_limit_bucket" in idx
    finally:
        conn.close()
    print("  table + 4 cols + bucket index ok")


def test_2_under_limit_allowed(db: Path) -> None:
    print("\n[test 2] check_and_consume under limit → all allowed")
    mailbox_rate_limit.init_schema(db)
    limit = 10
    for i in range(limit):
        allowed, info = mailbox_rate_limit.check_and_consume(
            db, "test:under", limit_per_min=limit,
        )
        assert allowed is True, \
            f"request {i+1}/{limit} rejected: {info}"
        assert info["limit"] == limit
    print(f"  {limit} requests allowed; effective_count={info['effective_count']}")


def test_3_exceeds_limit_rejected(db: Path) -> None:
    print("\n[test 3] check_and_consume exceeds limit → rejected + retry_after")
    mailbox_rate_limit.init_schema(db)
    limit = 5
    last_info = None
    for _ in range(limit + 3):
        allowed, info = mailbox_rate_limit.check_and_consume(
            db, "test:exceed", limit_per_min=limit,
        )
        last_info = info
    assert allowed is False, \
        f"expected rejection after {limit + 3} requests, got allowed: {last_info}"
    assert last_info["retry_after_seconds"] > 0
    assert last_info["effective_count"] > limit
    print(f"  rejected; effective_count={last_info['effective_count']} "
          f"retry_after={last_info['retry_after_seconds']}s")


def test_4_scope_independence(db: Path) -> None:
    print("\n[test 4] different scope_keys are independent")
    mailbox_rate_limit.init_schema(db)
    limit = 3
    # Exhaust scope A
    for _ in range(limit + 1):
        mailbox_rate_limit.check_and_consume(db, "test:A", limit_per_min=limit)
    allowed_a, _ = mailbox_rate_limit.check_and_consume(db, "test:A", limit_per_min=limit)
    assert allowed_a is False

    # Scope B still has full budget
    allowed_b, info_b = mailbox_rate_limit.check_and_consume(db, "test:B", limit_per_min=limit)
    assert allowed_b is True, f"scope B should not be affected by A: {info_b}"
    print("  A exhausted, B unaffected ok")


def test_5_disabled_env_always_allows(db: Path) -> None:
    print("\n[test 5] MAILBOX_RATE_LIMIT_DISABLED=1 always allows")
    mailbox_rate_limit.init_schema(db)
    os.environ["MAILBOX_RATE_LIMIT_DISABLED"] = "1"
    try:
        # Spam well past any sane limit
        for _ in range(50):
            allowed, info = mailbox_rate_limit.check_and_consume(
                db, "test:disabled", limit_per_min=1,
            )
            assert allowed is True
            assert info["disabled"] is True
    finally:
        del os.environ["MAILBOX_RATE_LIMIT_DISABLED"]
    print("  50 requests with limit=1 all allowed due to disabled flag")


def test_6_prune_old_buckets(db: Path) -> None:
    print("\n[test 6] prune_old_buckets deletes buckets older than N hours")
    mailbox_rate_limit.init_schema(db)

    # Seed buckets at various ages
    now = int(time.time() // 60)
    conn = sqlite3.connect(str(db))
    try:
        # Old bucket (3 hours ago)
        conn.execute(
            "INSERT INTO rate_limit_buckets(scope_key, minute_bucket, count) "
            "VALUES('test:old', ?, 5)",
            (now - 180,),
        )
        # Recent bucket (just now)
        conn.execute(
            "INSERT INTO rate_limit_buckets(scope_key, minute_bucket, count) "
            "VALUES('test:recent', ?, 5)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    pruned = mailbox_rate_limit.prune_old_buckets(db, hours=1)
    assert pruned == 1, f"expected 1 pruned (the 3hr-old one), got {pruned}"

    # Verify
    conn = sqlite3.connect(str(db))
    try:
        remaining = {r[0] for r in conn.execute(
            "SELECT scope_key FROM rate_limit_buckets"
        ).fetchall()}
        assert remaining == {"test:recent"}, f"remaining: {remaining}"
    finally:
        conn.close()
    print(f"  pruned {pruned} old bucket, kept the recent one")


def test_7_top_scopes(db: Path) -> None:
    print("\n[test 7] top_scopes returns most-active scopes desc")
    mailbox_rate_limit.init_schema(db)
    for scope, n in (("test:noisy", 10), ("test:quiet", 2), ("test:medium", 5)):
        for _ in range(n):
            mailbox_rate_limit.check_and_consume(db, scope, limit_per_min=999)

    top = mailbox_rate_limit.top_scopes(db, limit=10)
    assert len(top) >= 3, f"expected ≥3 scopes, got {len(top)}"
    # Filter to our test scopes (other tests may leave traces — they wouldn't
    # here since each test gets a fresh db, but defensively)
    our = [t for t in top if t["scope_key"].startswith("test:")]
    by_scope = {t["scope_key"]: t["recent_count"] for t in our}
    assert by_scope.get("test:noisy") == 10
    assert by_scope.get("test:medium") == 5
    assert by_scope.get("test:quiet") == 2
    # Order: noisy first
    assert our[0]["scope_key"] == "test:noisy"
    print(f"  3 scopes ranked correctly: noisy(10) > medium(5) > quiet(2)")


def test_8_reset_scope(db: Path) -> None:
    print("\n[test 8] reset_scope wipes a scope's buckets")
    mailbox_rate_limit.init_schema(db)
    for _ in range(7):
        mailbox_rate_limit.check_and_consume(db, "test:to-reset",
                                              limit_per_min=999)
    for _ in range(3):
        mailbox_rate_limit.check_and_consume(db, "test:to-keep",
                                              limit_per_min=999)

    deleted = mailbox_rate_limit.reset_scope(db, "test:to-reset")
    assert deleted >= 1

    # to-keep unaffected
    top = mailbox_rate_limit.top_scopes(db, limit=10)
    by_scope = {t["scope_key"]: t["recent_count"] for t in top}
    assert "test:to-reset" not in by_scope
    assert by_scope.get("test:to-keep") == 3
    print(f"  reset deleted {deleted} bucket(s); other scope preserved")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-rl-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_ddl_idempotent,
                                 test_2_under_limit_allowed,
                                 test_3_exceeds_limit_rejected,
                                 test_4_scope_independence,
                                 test_5_disabled_env_always_allows,
                                 test_6_prune_old_buckets,
                                 test_7_top_scopes,
                                 test_8_reset_scope), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL RATE LIMIT TESTS PASSED")
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
