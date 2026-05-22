"""Per-actor mute list: hide messages from specific peers in inbox view.

Schema (independent table, own DDL — like reactions / webhooks pattern):
  mutes(id, actor, muted_peer, created_at)
  UNIQUE(actor, muted_peer)

Behavior contract:
  - inbox() default: hide rows where from_name ∈ (actor's mute list)
  - inbox(include_muted=True): bypass filter
  - Retention sweep does NOT touch muted messages — mute is a read-side
    filter, not a write-side decision. The messages still exist.
  - Mute is per-actor — wiki muting koatag has zero effect on koatag's view.

Doesn't touch messages schema → independent module-level init_schema (no
migration version needed). DDL is idempotent CREATE IF NOT EXISTS.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def init_schema(db_path: Path) -> None:
    """Idempotent DDL — create mutes table if missing."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mutes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                actor       TEXT NOT NULL,
                muted_peer  TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(actor, muted_peer)
            );
            CREATE INDEX IF NOT EXISTS idx_mutes_actor ON mutes(actor);
        """)
        conn.commit()
    finally:
        conn.close()


def mute(db_path: Path, actor: str, peer: str) -> dict:
    """Add peer to actor's mute list. Idempotent.

    Returns {muted: True, was_already_muted: bool, actor, peer}.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        existing = conn.execute(
            "SELECT 1 FROM mutes WHERE actor = ? AND muted_peer = ? LIMIT 1",
            (actor, peer),
        ).fetchone()
        if existing:
            return {"muted": True, "was_already_muted": True,
                    "actor": actor, "peer": peer}
        conn.execute(
            "INSERT INTO mutes(actor, muted_peer) VALUES(?, ?)",
            (actor, peer),
        )
        conn.commit()
        return {"muted": True, "was_already_muted": False,
                "actor": actor, "peer": peer}
    finally:
        conn.close()


def unmute(db_path: Path, actor: str, peer: str) -> dict:
    """Remove peer from actor's mute list. Returns {muted: False, was_muted: bool}."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        cur = conn.execute(
            "DELETE FROM mutes WHERE actor = ? AND muted_peer = ?",
            (actor, peer),
        )
        conn.commit()
        return {"muted": False, "was_muted": cur.rowcount > 0,
                "actor": actor, "peer": peer}
    finally:
        conn.close()


def list_mutes(db_path: Path, actor: str) -> list[str]:
    """Return the list of peers `actor` has muted, sorted alphabetically."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            rows = conn.execute(
                "SELECT muted_peer FROM mutes WHERE actor = ? "
                "ORDER BY muted_peer ASC",
                (actor,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    """Observability: total mute relationships + distinct actors muting."""
    out = {"mute_count": 0, "muting_actors": 0}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        try:
            out["mute_count"] = conn.execute("SELECT COUNT(*) FROM mutes").fetchone()[0]
            out["muting_actors"] = conn.execute(
                "SELECT COUNT(DISTINCT actor) FROM mutes"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return out
    finally:
        conn.close()
