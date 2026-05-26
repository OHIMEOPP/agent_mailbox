"""Smoke test for mailbox-watch.py honoring the mutes table.

Wiki #1568: watcher kept firing MAIL events for peers muted via
mcp__mailbox__mute_peer because watcher SQL didn't join the mutes table.

Verifies:
  1. fetch_unread excludes rows where (actor=name, muted_peer=from_name)
  2. fetch_unread_all (--watch-all) excludes rows where the row's to_name
     has muted the row's from_name
  3. Unmute resurfaces those rows on next call
  4. Legacy DB without mutes table still works (graceful fallback)
"""
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent.parent


def load_watcher():
    """Import mailbox-watch.py as a module (filename has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "mailbox_watch", HERE / "mailbox-watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _init_db(db_path: str, with_mutes: bool = True) -> None:
    with sqlite3.connect(db_path) as c:
        c.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                read_at TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE peers (
                name TEXT PRIMARY KEY,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                filename TEXT NOT NULL,
                mime TEXT,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        """)
        if with_mutes:
            c.executescript("""
                CREATE TABLE mutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    muted_peer TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(actor, muted_peer)
                );
                CREATE INDEX idx_mutes_actor ON mutes(actor);
            """)


def _insert_msg(db_path: str, from_name: str, to_name: str, body: str) -> int:
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO messages (from_name, to_name, body) VALUES (?, ?, ?)",
            (from_name, to_name, body),
        )
        return cur.lastrowid


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="watcher-mute-smoke-"))
    db = workdir / "mailbox.db"
    watcher = load_watcher()

    try:
        # --- Test 1: per-name mode honors mute ---
        _init_db(str(db), with_mutes=True)
        _insert_msg(str(db), "koatag", "wiki", "normal mail")
        _insert_msg(str(db), "folder-sync@DESKTOP", "wiki", "noisy 1")
        _insert_msg(str(db), "folder-sync@DESKTOP", "wiki", "noisy 2")
        _insert_msg(str(db), "mailbox-dev", "wiki", "another normal mail")

        with sqlite3.connect(str(db)) as c:
            rows = watcher.fetch_unread(c, "wiki", 0)
        assert len(rows) == 4, f"baseline expected 4 rows, got {len(rows)}: {rows}"
        senders = sorted(r[1] for r in rows)
        assert senders == ["folder-sync@DESKTOP", "folder-sync@DESKTOP",
                           "koatag", "mailbox-dev"]
        print("[smoke] baseline (no mute) ok — 4 rows visible")

        # Mute folder-sync
        with sqlite3.connect(str(db)) as c:
            c.execute("INSERT INTO mutes(actor, muted_peer) VALUES (?, ?)",
                      ("wiki", "folder-sync@DESKTOP"))
            c.commit()

        with sqlite3.connect(str(db)) as c:
            rows = watcher.fetch_unread(c, "wiki", 0)
        assert len(rows) == 2, f"after mute expected 2 rows, got {len(rows)}"
        senders = sorted(r[1] for r in rows)
        assert senders == ["koatag", "mailbox-dev"], senders
        print("[smoke] per-name mute filter ok — folder-sync hidden")

        # Confirm mute is per-actor: koatag muting folder-sync != wiki muting
        with sqlite3.connect(str(db)) as c:
            c.execute("INSERT INTO mutes(actor, muted_peer) VALUES (?, ?)",
                      ("koatag", "anyone"))
            c.commit()
            # wiki still sees the same filtered set
            rows = watcher.fetch_unread(c, "wiki", 0)
        assert len(rows) == 2, "koatag's mute leaked into wiki's view"
        print("[smoke] mute is per-actor ok")

        # --- Test 2: --watch-all mode honors per-recipient mute ---
        _insert_msg(str(db), "folder-sync@DESKTOP", "koatag", "for koatag")
        _insert_msg(str(db), "folder-sync@DESKTOP", "wiki", "noisy 3")

        with sqlite3.connect(str(db)) as c:
            rows = watcher.fetch_unread_all(c, 0)
        # wiki has muted folder-sync, koatag has not — koatag's row visible,
        # wiki's folder-sync rows hidden.
        wiki_rows = [r for r in rows if r[2] == "wiki"]
        koatag_rows = [r for r in rows if r[2] == "koatag"]
        wiki_senders = sorted(r[1] for r in wiki_rows)
        assert wiki_senders == ["koatag", "mailbox-dev"], \
            f"wiki rows should hide folder-sync, got {wiki_senders}"
        assert len(koatag_rows) == 1 and koatag_rows[0][1] == "folder-sync@DESKTOP", \
            f"koatag row should be visible: {koatag_rows}"
        print("[smoke] watch-all per-recipient mute ok")

        # --- Test 3: unmute resurfaces ---
        with sqlite3.connect(str(db)) as c:
            c.execute("DELETE FROM mutes WHERE actor=? AND muted_peer=?",
                      ("wiki", "folder-sync@DESKTOP"))
            c.commit()
            rows = watcher.fetch_unread(c, "wiki", 0)
        senders = sorted(r[1] for r in rows)
        assert senders.count("folder-sync@DESKTOP") == 3, \
            f"after unmute expected 3 folder-sync rows, got {senders}"
        print("[smoke] unmute resurfaces ok")

        # --- Test 4: legacy DB without mutes table ---
        legacy_db = workdir / "legacy.db"
        _init_db(str(legacy_db), with_mutes=False)
        _insert_msg(str(legacy_db), "koatag", "wiki", "legacy mail 1")
        _insert_msg(str(legacy_db), "folder-sync@DESKTOP", "wiki", "legacy mail 2")
        with sqlite3.connect(str(legacy_db)) as c:
            rows = watcher.fetch_unread(c, "wiki", 0)
            rows_all = watcher.fetch_unread_all(c, 0)
        assert len(rows) == 2, f"legacy per-name expected 2, got {len(rows)}"
        assert len(rows_all) == 2, f"legacy watch-all expected 2, got {len(rows_all)}"
        print("[smoke] legacy DB (no mutes table) graceful fallback ok")

        print("\n[smoke] ALL TESTS PASSED")
        return 0
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2
    except Exception as e:
        print(f"\n[smoke] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
