"""Outbound webhook fan-out for mailbox messages.

When new messages land in the mailbox.db, registered webhooks receive a
POST with the message payload + HMAC signature header. Lets external systems
(Slack, dashboards, custom bots, …) react to mailbox activity without
polling the SQLite DB themselves.

Imported by:
  - mailbox-server.py (daemon thread polls and dispatches every few seconds)
  - mailbox-webhooks.py (CLI: --list / --add / --delete / --tail-deliveries / --test)

Tables (idempotent DDL):
  webhooks(id, name, url, secret_hmac, filter_to_glob, filter_from_glob,
           active, created_at, last_fired_at, total_fires, last_error)
  webhook_deliveries(id, webhook_id, message_id, status, attempts,
                     last_attempt_at, response_code, response_body)

`status` ∈ {'pending', 'success', 'failed', 'skipped'}.

Wire format (POST body):
  {
    "event": "mail",
    "message": {
      "id": 123,
      "from": "wiki",
      "to": "koatag",
      "body": "...",
      "sent_at": "2026-05-23T01:30:00.000Z",
      "in_reply_to": null,
      "expires_at": null,
      "has_attachments": 0
    },
    "delivered_at": "2026-05-23T01:30:01.234Z"
  }

Headers:
  X-Mailbox-Sig: sha256=<hmac hex of body using webhook secret>
  X-Mailbox-Webhook-Id: <int>
  X-Mailbox-Delivery-Id: <int>
  Content-Type: application/json; charset=utf-8

Retry: each delivery is attempted up to MAX_ATTEMPTS times across daemon
ticks (no exponential backoff — keep simple, daemon ticks every 5s). After
MAX_ATTEMPTS failures the row is marked status='failed' and stays there for
forensics; admin can re-test via the CLI's `--test` flag.

Design notes:
  - Polling not /send-hook — avoids contention with mailing-list aliases
    fanout (wiki/#5) and any future write-path features.
  - Secret stored verbatim in the DB; treat the DB file as private. Future
    work: encryption-at-rest via env-fed key.
  - HMAC-SHA256 only; no per-event auth scheme. LAN/VPN trust model assumed
    for inbound endpoints.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import secrets as _secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAX_ATTEMPTS = 5         # give up after this many tries per delivery
DAEMON_TICK_SECONDS = 5  # how often the server polls for new messages
HTTP_TIMEOUT = 10        # per-request socket timeout, seconds


def _is_disabled() -> bool:
    """Kill-switch via env. Daemon respects this on every tick; CLI ignores."""
    return os.environ.get("MAILBOX_WEBHOOKS_DISABLED", "").strip() in ("1", "true", "yes")


def init_schema(db_path: Path) -> None:
    """Idempotent DDL — webhooks + webhook_deliveries tables + indexes.

    Mirrors mailbox_audit / mailbox_backup pattern: separate DDL block,
    safe under repeated calls, no ALTER traps.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS webhooks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL UNIQUE,
                url              TEXT NOT NULL,
                secret_hmac      TEXT NOT NULL,
                filter_to_glob   TEXT,
                filter_from_glob TEXT,
                active           INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                last_fired_at    TEXT,
                total_fires      INTEGER NOT NULL DEFAULT 0,
                last_error       TEXT
            );

            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                webhook_id      INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
                message_id      INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                response_code   INTEGER,
                response_body   TEXT,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
                ON webhook_deliveries(status, webhook_id);
            CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_message
                ON webhook_deliveries(message_id);
        """)
        conn.commit()
    finally:
        conn.close()


def _generate_secret() -> str:
    return _secrets.token_urlsafe(32)


def register(
    db_path: Path,
    name: str,
    url: str,
    filter_to_glob: str | None = None,
    filter_from_glob: str | None = None,
    secret: str | None = None,
) -> dict:
    """Add a new webhook. Returns the row dict including the secret.

    `secret` is generated if not provided; capture it from the return value —
    it's stored in DB but the CLI doesn't re-expose it on `--list`.
    """
    sec = secret or _generate_secret()
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "INSERT INTO webhooks(name, url, secret_hmac, filter_to_glob, filter_from_glob) "
            "VALUES(?, ?, ?, ?, ?) RETURNING *",
            (name, url, sec, filter_to_glob, filter_from_glob),
        ).fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def list_webhooks(db_path: Path, include_secret: bool = False) -> list[dict]:
    """Return all webhooks. Default omits secret_hmac to avoid leaking."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM webhooks ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            if not include_secret:
                d["secret_hmac"] = "***"
            out.append(d)
        return out
    finally:
        conn.close()


def delete(db_path: Path, webhook_id: int) -> int:
    """Delete by id. Returns rowcount (0 if no such webhook).

    Deliveries are CASCADE-deleted by the FK definition.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")  # enable CASCADE
        cur = conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_active(db_path: Path, webhook_id: int, active: bool) -> int:
    """Toggle active. Returns rowcount."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        cur = conn.execute(
            "UPDATE webhooks SET active = ? WHERE id = ?",
            (1 if active else 0, webhook_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_deliveries(
    db_path: Path,
    webhook_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Recent deliveries, newest first."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        clauses: list[str] = []
        params: list = []
        if webhook_id is not None:
            clauses.append("webhook_id = ?")
            params.append(webhook_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM webhook_deliveries{where} ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _match_filters(msg: dict, w: dict) -> bool:
    """fnmatch globs on to/from. Both None = pass-through (fire for all)."""
    if w.get("filter_to_glob"):
        if not fnmatch.fnmatch(msg["to_name"], w["filter_to_glob"]):
            return False
    if w.get("filter_from_glob"):
        if not fnmatch.fnmatch(msg["from_name"], w["filter_from_glob"]):
            return False
    return True


def _build_payload(msg: dict) -> bytes:
    """Serialize the wire-format JSON for POST body."""
    return json.dumps({
        "event": "mail",
        "message": {
            "id": msg["id"],
            "from": msg["from_name"],
            "to": msg["to_name"],
            "body": msg["body"],
            "sent_at": msg["sent_at"],
            "in_reply_to": msg.get("in_reply_to"),
            "expires_at": msg.get("expires_at"),
            "has_attachments": bool(msg.get("has_attachments")),
        },
        "delivered_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _sign(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """Returns (status_code, response_text). Raises on connection errors."""
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json; charset=utf-8",
                                           "Content-Length": str(len(body)),
                                           **headers})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read(8192).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read(8192).decode("utf-8", errors="replace")
        return e.code, body


def deliver_pending(db_path: Path, since_id: int) -> dict:
    """Scan messages with id > since_id; for each active webhook, enqueue a
    delivery if filters match, then attempt POSTs for pending+retryable rows.

    Daemon contract:
      - Caller passes the last message id it dispatched on the prior tick
        (or 0 for first run / cold start).
      - Returns counters and `new_since_id` — caller passes that back next tick.
      - Idempotent within a tick: re-running with the same since_id won't
        double-enqueue (the queue table is keyed on (webhook_id, message_id)
        via INSERT...WHERE NOT EXISTS).

    Counters: {messages_scanned, deliveries_enqueued, deliveries_succeeded,
               deliveries_failed, deliveries_skipped, new_since_id}
    """
    counters = {
        "messages_scanned": 0,
        "deliveries_enqueued": 0,
        "deliveries_succeeded": 0,
        "deliveries_failed": 0,
        "deliveries_skipped": 0,
        "new_since_id": since_id,
    }
    if _is_disabled():
        return counters

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # Pull active webhooks once per tick.
        try:
            webhooks = [dict(r) for r in conn.execute(
                "SELECT * FROM webhooks WHERE active = 1"
            ).fetchall()]
        except sqlite3.OperationalError:
            return counters
        if not webhooks:
            # Still advance since_id so we don't re-scan endlessly when later
            # webhooks register; first scan after registration will see prior
            # messages_id and naturally skip them.
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
            counters["new_since_id"] = row[0]
            return counters

        # New messages since last tick.
        msgs = [dict(r) for r in conn.execute(
            "SELECT id, from_name, to_name, body, sent_at, has_attachments, "
            "in_reply_to, expires_at "
            "FROM messages WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()]
        counters["messages_scanned"] = len(msgs)

        # Stage 1: enqueue deliveries (INSERT-only; safe against re-runs because
        # we'll dedupe via (webhook_id, message_id) check before insert).
        for msg in msgs:
            for w in webhooks:
                if not _match_filters(msg, w):
                    continue
                # Dedupe — if a row exists for this (webhook_id, message_id),
                # skip enqueue. Cheap (indexed lookup).
                existing = conn.execute(
                    "SELECT 1 FROM webhook_deliveries "
                    "WHERE webhook_id = ? AND message_id = ? LIMIT 1",
                    (w["id"], msg["id"]),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO webhook_deliveries(webhook_id, message_id) "
                    "VALUES(?, ?)",
                    (w["id"], msg["id"]),
                )
                counters["deliveries_enqueued"] += 1
            # Advance high-water mark regardless of whether any delivery enqueued.
            counters["new_since_id"] = max(counters["new_since_id"], msg["id"])
        conn.commit()

        # Stage 2: pick pending deliveries (limit to a reasonable batch per tick
        # to keep daemon turnaround predictable). Order by id so retries get
        # fair share with new deliveries.
        pending = conn.execute(
            "SELECT d.*, w.url AS webhook_url, w.secret_hmac AS webhook_secret, "
            "       w.name AS webhook_name "
            "FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
            "WHERE d.status = 'pending' AND d.attempts < ? "
            "ORDER BY d.id ASC LIMIT 100",
            (MAX_ATTEMPTS,),
        ).fetchall()

        for d in pending:
            d = dict(d)
            # Lazy-fetch message row (could batch but msgs are small).
            msg_row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (d["message_id"],),
            ).fetchone()
            if msg_row is None:
                # Message gone (retention swept it before we dispatched).
                # Mark skipped + advance state.
                conn.execute(
                    "UPDATE webhook_deliveries SET status='skipped', "
                    "last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "response_body='message no longer exists' "
                    "WHERE id = ?", (d["id"],),
                )
                counters["deliveries_skipped"] += 1
                continue
            msg = dict(msg_row)

            body = _build_payload(msg)
            sig = _sign(body, d["webhook_secret"])
            headers = {
                "X-Mailbox-Sig": sig,
                "X-Mailbox-Webhook-Id": str(d["webhook_id"]),
                "X-Mailbox-Delivery-Id": str(d["id"]),
            }
            try:
                code, resp = _http_post(d["webhook_url"], body, headers)
                ok = 200 <= code < 300
                new_status = "success" if ok else "pending"
                # If we just exhausted attempts, mark failed.
                new_attempts = d["attempts"] + 1
                if not ok and new_attempts >= MAX_ATTEMPTS:
                    new_status = "failed"
                conn.execute(
                    "UPDATE webhook_deliveries SET status=?, attempts=?, "
                    "last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "response_code=?, response_body=? WHERE id=?",
                    (new_status, new_attempts, code, resp[:2048], d["id"]),
                )
                if ok:
                    conn.execute(
                        "UPDATE webhooks SET total_fires = total_fires + 1, "
                        "last_fired_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                        "last_error = NULL WHERE id = ?",
                        (d["webhook_id"],),
                    )
                    counters["deliveries_succeeded"] += 1
                else:
                    if new_status == "failed":
                        conn.execute(
                            "UPDATE webhooks SET last_error = ? WHERE id = ?",
                            (f"HTTP {code}", d["webhook_id"]),
                        )
                        counters["deliveries_failed"] += 1
            except (urllib.error.URLError, OSError) as e:
                new_attempts = d["attempts"] + 1
                err_text = f"{type(e).__name__}: {e}"
                new_status = "pending"
                if new_attempts >= MAX_ATTEMPTS:
                    new_status = "failed"
                    counters["deliveries_failed"] += 1
                conn.execute(
                    "UPDATE webhook_deliveries SET status=?, attempts=?, "
                    "last_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "response_body=? WHERE id=?",
                    (new_status, new_attempts, err_text[:2048], d["id"]),
                )
                if new_status == "failed":
                    conn.execute(
                        "UPDATE webhooks SET last_error = ? WHERE id = ?",
                        (err_text[:255], d["webhook_id"]),
                    )
        conn.commit()
    finally:
        conn.close()

    return counters


def stats(db_path: Path) -> dict:
    """Observability for /health + CLI --stats."""
    if not db_path.exists():
        return {
            "webhook_count": 0,
            "webhook_pending_deliveries": 0,
            "webhook_last_fired_at": None,
            "webhook_failed_deliveries": 0,
        }
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        out = {
            "webhook_count": 0,
            "webhook_pending_deliveries": 0,
            "webhook_last_fired_at": None,
            "webhook_failed_deliveries": 0,
        }
        try:
            out["webhook_count"] = conn.execute(
                "SELECT COUNT(*) FROM webhooks WHERE active = 1"
            ).fetchone()[0]
            out["webhook_pending_deliveries"] = conn.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'pending'"
            ).fetchone()[0]
            out["webhook_failed_deliveries"] = conn.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'failed'"
            ).fetchone()[0]
            last_at = conn.execute(
                "SELECT MAX(last_fired_at) FROM webhooks"
            ).fetchone()[0]
            out["webhook_last_fired_at"] = last_at
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()


def verify_signature(body: bytes, header_sig: str, secret: str) -> bool:
    """For receivers: verify incoming POST signature matches expected HMAC.

    Server-side example — pasted into a Flask handler:
        body = request.get_data()
        sig = request.headers.get('X-Mailbox-Sig', '')
        if not mailbox_webhooks.verify_signature(body, sig, MY_SECRET):
            abort(401)
    """
    expected = _sign(body, secret)
    return hmac.compare_digest(expected, header_sig)
