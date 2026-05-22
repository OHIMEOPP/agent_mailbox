"""Smoke test for mailbox audit log.

Tests run directly against mailbox_audit functions + the CLI as a subprocess.

Test plan:
  1. init_schema is idempotent + creates expected indexes
  2. log_event writes a row for each action vocabulary; query_audit retrieves
  3. query_audit filters (actor / action / since / limit / asc-order)
  4. CLI mailbox-audit.py --tail / --stats / --json subprocess works
  5. MAILBOX_AUDIT_DISABLED=1 env var skips log_event writes (CLI reads still work)
  6. log_event swallows DB failure (e.g. bad path) instead of raising
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Local import via path manipulation since this file is alongside the module
sys.path.insert(0, str(Path(__file__).parent.parent))
import mailbox_audit  # noqa: E402


def test_1_ddl_idempotent(db: Path) -> None:
    print("\n[test 1] init_schema idempotent + index shape")
    mailbox_audit.init_schema(db)
    # Run twice — should be no-op
    mailbox_audit.init_schema(db)

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "audit_log" in tables, f"audit_log table missing: {tables}"

        cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        expected_cols = {"id", "ts", "actor", "action", "target", "payload_json", "ok"}
        assert expected_cols.issubset(cols), \
            f"audit_log missing columns: expected={expected_cols} got={cols}"

        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'"
        ).fetchall()}
        for expected_idx in ("idx_audit_ts", "idx_audit_actor_ts", "idx_audit_action_ts"):
            assert expected_idx in idx, \
                f"missing index {expected_idx} (got {idx})"
    finally:
        conn.close()
    print(f"  table+cols+3 indexes present; rerun no-op ok")


def test_2_log_each_action(db: Path) -> None:
    print("\n[test 2] log_event for each ACTIONS member; query_audit retrieves")
    mailbox_audit.init_schema(db)

    for action in mailbox_audit.ACTIONS:
        mailbox_audit.log_event(
            db, actor="smoke-actor", action=action,
            target=f"target-for-{action}",
            payload={"action_kind": action, "n": 7},
        )

    rows = mailbox_audit.query_audit(db, limit=100)
    actions_seen = {r["action"] for r in rows}
    assert actions_seen == mailbox_audit.ACTIONS, \
        f"missing actions: expected={mailbox_audit.ACTIONS} got={actions_seen}"
    for r in rows:
        assert r["actor"] == "smoke-actor"
        assert r["target"] == f"target-for-{r['action']}"
        assert isinstance(r["payload"], dict)
        assert r["payload"]["n"] == 7
        assert r["ok"] is True
    print(f"  wrote {len(rows)} rows covering {sorted(actions_seen)}")

    s = mailbox_audit.stats(db)
    assert s["audit_count"] == len(mailbox_audit.ACTIONS), \
        f"stats count {s['audit_count']} != {len(mailbox_audit.ACTIONS)}"
    assert s["by_action"] == {a: 1 for a in mailbox_audit.ACTIONS}, \
        f"by_action mismatch: {s['by_action']}"
    print(f"  stats: count={s['audit_count']} by_action={s['by_action']}")


def test_3_filters(db: Path) -> None:
    print("\n[test 3] query_audit filters (actor / action / since / asc)")
    mailbox_audit.init_schema(db)

    mailbox_audit.log_event(db, "alice", "send", target="bob",
                             payload={"msg_id": 1})
    time.sleep(0.01)
    mailbox_audit.log_event(db, "alice", "inbox", payload={"returned": 0})
    time.sleep(0.01)
    mailbox_audit.log_event(db, "bob", "send", target="alice",
                             payload={"msg_id": 2})
    time.sleep(0.01)
    mid_ts = sqlite3.connect(str(db)).execute(
        "SELECT ts FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    time.sleep(0.05)
    mailbox_audit.log_event(db, "bob", "mark_read", payload={"ids": [1]})

    # actor filter
    alice_rows = mailbox_audit.query_audit(db, actor="alice", limit=100)
    assert len(alice_rows) == 2, f"alice filter got {len(alice_rows)}"
    assert {r["action"] for r in alice_rows} == {"send", "inbox"}

    # action filter
    send_rows = mailbox_audit.query_audit(db, action="send", limit=100)
    assert len(send_rows) == 2, f"send filter got {len(send_rows)}"
    assert {r["actor"] for r in send_rows} == {"alice", "bob"}

    # since filter — rows AFTER mid_ts (strictly greater)
    after = mailbox_audit.query_audit(db, since=mid_ts, limit=100)
    assert len(after) == 1, f"since filter got {len(after)}: {after}"
    assert after[0]["action"] == "mark_read"

    # asc order
    asc = mailbox_audit.query_audit(db, order_desc=False, limit=100)
    desc = mailbox_audit.query_audit(db, order_desc=True, limit=100)
    assert asc[0]["id"] < asc[-1]["id"]
    assert desc[0]["id"] > desc[-1]["id"]
    assert list(reversed(asc)) == desc

    # limit truncates
    short = mailbox_audit.query_audit(db, limit=2)
    assert len(short) == 2

    print(f"  filters ok: actor=alice→2, action=send→2, since→1, asc/desc/limit ok")


def test_4_cli_subprocess(db: Path) -> None:
    print("\n[test 4] CLI --tail / --stats / --json subprocess")
    mailbox_audit.init_schema(db)
    mailbox_audit.log_event(db, "cli-actor", "send", target="peer",
                             payload={"msg_id": 42})
    mailbox_audit.log_event(db, "cli-actor", "inbox",
                             payload={"returned": 5})

    here = Path(__file__).parent.parent
    cli = str(here / "mailbox-audit.py")

    # --stats --json
    r = subprocess.run(
        [sys.executable, cli, "--db", str(db), "--stats", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, \
        f"CLI --stats failed (rc={r.returncode}): stderr={r.stderr}"
    s = json.loads(r.stdout)
    assert s["audit_count"] == 2
    assert s["by_action"] == {"send": 1, "inbox": 1}

    # --tail --json
    r = subprocess.run(
        [sys.executable, cli, "--db", str(db), "--tail", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"CLI --tail failed: {r.stderr}"
    rows = json.loads(r.stdout)
    assert isinstance(rows, list)
    assert len(rows) == 2
    actions = {row["action"] for row in rows}
    assert actions == {"send", "inbox"}

    # --tail --actor cli-actor --action send (combined filter)
    r = subprocess.run(
        [sys.executable, cli, "--db", str(db), "--tail",
         "--actor", "cli-actor", "--action", "send", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    rows = json.loads(r.stdout)
    assert len(rows) == 1 and rows[0]["action"] == "send"

    # --action with unknown value rejected with exit code
    r = subprocess.run(
        [sys.executable, cli, "--db", str(db), "--action", "nosuch"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 2, f"expected rc=2 for bad --action, got {r.returncode}"
    assert "unknown --action" in r.stderr

    print(f"  CLI --stats / --tail / filters / unknown-action ok")


def test_5_disabled_env_skips_writes(db: Path) -> None:
    print("\n[test 5] MAILBOX_AUDIT_DISABLED=1 skips writes")
    mailbox_audit.init_schema(db)
    mailbox_audit.log_event(db, "pre", "send", payload={"x": 1})
    pre_count = mailbox_audit.stats(db)["audit_count"]
    assert pre_count == 1

    os.environ["MAILBOX_AUDIT_DISABLED"] = "1"
    try:
        for _ in range(5):
            mailbox_audit.log_event(db, "skipped", "send", payload={"y": 2})
    finally:
        del os.environ["MAILBOX_AUDIT_DISABLED"]

    post_count = mailbox_audit.stats(db)["audit_count"]
    assert post_count == pre_count, \
        f"writes should have been skipped: pre={pre_count} post={post_count}"

    # Sanity — after un-setting env, writes work again
    mailbox_audit.log_event(db, "after", "send", payload={"z": 3})
    assert mailbox_audit.stats(db)["audit_count"] == pre_count + 1
    print(f"  5 disabled writes skipped; post-unset write recorded ok")


def test_6_log_event_swallows_failures(workdir: Path) -> None:
    print("\n[test 6] log_event swallows DB failure (never raises)")
    bogus_db = workdir / "does-not-exist-dir" / "no-write" / "x.db"
    # No init_schema call; sqlite3 will fail to open + write
    # Should not raise — best-effort logging.
    try:
        mailbox_audit.log_event(bogus_db, "x", "send", payload={"x": 1})
    except Exception as e:
        raise AssertionError(f"log_event raised on bad path: {type(e).__name__}: {e}")
    print("  log_event survived unreachable db path ok")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-audit-smoke-"))
    print(f"[smoke] workdir={workdir}")
    try:
        for i, fn in enumerate((test_1_ddl_idempotent,
                                 test_2_log_each_action,
                                 test_3_filters,
                                 test_4_cli_subprocess,
                                 test_5_disabled_env_skips_writes), start=1):
            db = workdir / f"t{i}.db"
            fn(db)
        test_6_log_event_swallows_failures(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n[smoke] ALL AUDIT TESTS PASSED")
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
