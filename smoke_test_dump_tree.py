"""Smoke test for mailbox-dump.py --tree rendering.

Seeds a temp DB with mixed root + nested + orphan threads, runs the CLI
as subprocess, asserts the box-drawing structure + flags appear correctly.
"""
import io
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Force UTF-8 — tree output contains ⚠ (U+26A0) which cp950 can't encode.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-dump-tree-smoke-"))
    db = workdir / "mailbox.db"
    here = Path(__file__).parent
    print(f"[smoke] workdir={workdir}")

    try:
        # --- Schema (matches current mailbox-server.py db_init shape) ---
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                read_at TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                in_reply_to INTEGER
            );
            CREATE TABLE peers (name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL);
            CREATE TABLE reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                emoji TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(message_id, actor, emoji)
            );
        """)

        def insert(from_name, to_name, body, in_reply_to=None):
            cur = conn.execute(
                "INSERT INTO messages(from_name, to_name, body, in_reply_to) "
                "VALUES(?, ?, ?, ?) RETURNING id",
                (from_name, to_name, body, in_reply_to),
            )
            return cur.fetchone()[0]

        # Tree structure:
        #   [1] alice → bob   "root A"
        #   ├─[2] bob → alice  "reply to A"   in_reply_to=1
        #   │  └─[4] alice → bob "nested under 2" in_reply_to=2
        #   └─[3] bob → alice  "another reply to A" in_reply_to=1
        #   [5] alice → bob   "standalone root B"
        #   [6] alice → bob   "orphan reply to ghost" in_reply_to=999 (parent absent)
        m1 = insert("alice", "bob", "root A")
        m2 = insert("bob", "alice", "reply to A", in_reply_to=m1)
        m3 = insert("bob", "alice", "another reply to A", in_reply_to=m1)
        m4 = insert("alice", "bob", "nested under 2", in_reply_to=m2)
        m5 = insert("alice", "bob", "standalone root B")
        m6 = insert("alice", "bob", "orphan reply to ghost", in_reply_to=999)

        # Reactions: m1 gets ❤ from bob + dave, 👍 from carol;  m2 gets 🚀 from alice
        def react(mid, actor, emoji):
            conn.execute(
                "INSERT INTO reactions(message_id, actor, emoji) VALUES(?, ?, ?)",
                (mid, actor, emoji))
        react(m1, "bob", "❤")
        react(m1, "dave", "❤")
        react(m1, "carol", "👍")
        react(m2, "alice", "🚀")
        # m5 gets no reactions — should render without 💬 line

        conn.commit()
        conn.close()

        # --- Run mailbox-dump.py --tree ---
        result = subprocess.run(
            [sys.executable, str(here / "mailbox-dump.py"),
             "--db", str(db), "--tree"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=10,
        )
        if result.returncode != 0:
            print(f"[smoke] CLI exit {result.returncode}: {result.stderr}", file=sys.stderr)
            return 1
        out = result.stdout
        print("[smoke] --- CLI output ---")
        print(out)
        print("[smoke] --- end ---")

        # --- Assertions ---
        # Each message should appear with its id
        for mid in [m1, m2, m3, m4, m5, m6]:
            assert f"[{mid}]" in out, f"missing message [{mid}] in output"

        # Reply marker for child nodes
        assert f"(re: #{m1})" in out, "msg #2's parent ref missing"
        assert f"(re: #{m2})" in out, "msg #4's parent ref missing"

        # Tree connectors should appear for siblings
        assert "├─" in out, "missing tee connector for sibling"
        assert "└─" in out, "missing corner connector for last child"

        # Orphan flag for msg #6 (parent #999 absent)
        assert "broken-chain" in out, "orphan flag missing for msg #6"
        assert "parent #999" in out, "orphan flag should cite missing parent id"

        # Standalone root B has no parent ref or orphan flag in header line
        # Look for that header line specifically: "[5]" followed by sender, no (re:
        # We can grep more strictly:
        lines = out.splitlines()
        m5_header = next(l for l in lines if f"[{m5}]" in l)
        assert "re:" not in m5_header, f"standalone msg [{m5}] shouldn't have re: marker"
        assert "broken-chain" not in m5_header, f"[{m5}] shouldn't be flagged orphan"

        # Roots: messages 1, 5, 6 should be at indent level 0 (no leading │ / ├─ / └─)
        # Children: messages 2, 3, 4 should have indent
        m1_header = next(l for l in lines if f"[{m1}]" in l)
        m2_header = next(l for l in lines if f"[{m2}]" in l)
        assert not m1_header.startswith(("│", "├", "└", " ")), \
            f"[{m1}] should be root at col 0: {m1_header!r}"
        assert m2_header.startswith(("├", "└")), \
            f"[{m2}] should be a child with tree connector: {m2_header!r}"

        # Reactions render verification
        # m1: ❤ bob,dave  👍 carol  (grouped by emoji)
        assert "💬" in out, "missing reactions sigil"
        assert "❤" in out and "bob,dave" in out, \
            f"missing m1 ❤ bob,dave: {out!r}"
        assert "👍" in out and "carol" in out, "missing m1 👍 carol"
        assert "🚀" in out and "alice" in out, "missing m2 🚀 alice"
        # m5 has no reactions — its 💬 line should not exist between m5 and the
        # next separator. Find m5 block:
        m5_idx = next(i for i, l in enumerate(lines) if f"[{m5}]" in l)
        m5_body_lines = lines[m5_idx:m5_idx + 4]
        assert not any("💬" in l for l in m5_body_lines), \
            f"m5 has no reactions but rendered 💬 line: {m5_body_lines!r}"

        print(f"\n[smoke] ALL DUMP TREE TESTS PASSED ({6} messages, tree + 4 reactions verified)")
        return 0
    except AssertionError as e:
        print(f"\n[smoke] ASSERT FAIL: {e}", file=sys.stderr)
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
