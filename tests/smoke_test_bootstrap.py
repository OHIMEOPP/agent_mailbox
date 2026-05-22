"""Smoke test for bootstrap-spoke.py.

Tests CLI flags + .mcp.json generation. Uses --skip-probe to avoid network.

  1. --hub --token --name --skip-probe → writes valid .mcp.json
  2. Generated config has correct mcpServers.mailbox structure
  3. Defaults to <role>@<hostname> when --name omitted
  4. Existing .mcp.json without --force → exit 3 with helpful message
  5. --force overwrites
  6. Missing --token → exit 2
  7. Probe path: bogus hub URL → exit 1 (with --skip-probe absent)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)


def run(args, cwd=None, input_text=None, timeout=15):
    here = Path(__file__).parent.parent
    proc = subprocess.run(
        [sys.executable, str(here / "bootstrap-spoke.py")] + args,
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        input=input_text, timeout=timeout,
    )
    return proc


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mailbox-bootstrap-smoke-"))
    print(f"[smoke] workdir={workdir}")

    try:
        # ---- Test 1: happy path with all flags ----
        proj1 = workdir / "proj1"
        proj1.mkdir()
        r1 = run(["--hub", "http://192.168.1.10:1905",
                  "--token", "test-token-1234567890",
                  "--name", "wiki@TESTHOST",
                  "--project", str(proj1),
                  "--skip-probe"])
        assert r1.returncode == 0, \
            f"exit {r1.returncode}: stdout={r1.stdout!r} stderr={r1.stderr!r}"
        mcp_path = proj1 / ".mcp.json"
        assert mcp_path.exists(), "did not create .mcp.json"
        cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "mailbox" in cfg["mcpServers"]
        env = cfg["mcpServers"]["mailbox"]["env"]
        assert env["CLAUDE_MAILBOX_NAME"] == "wiki@TESTHOST"
        assert env["CLAUDE_MAILBOX_REMOTE"] == "http://192.168.1.10:1905"
        assert env["CLAUDE_MAILBOX_TOKEN"] == "test-token-1234567890"
        assert cfg["mcpServers"]["mailbox"]["command"] == "python"
        print(f"[smoke] happy path ok — {mcp_path}")

        # ---- Test 2: <role>@<hostname> default ----
        proj2 = workdir / "proj2"
        proj2.mkdir()
        r2 = run(["--hub", "http://hub:1905",
                  "--token", "abc",
                  "--role", "koatag",
                  "--project", str(proj2),
                  "--skip-probe"])
        assert r2.returncode == 0
        cfg2 = json.loads((proj2 / ".mcp.json").read_text(encoding="utf-8"))
        name2 = cfg2["mcpServers"]["mailbox"]["env"]["CLAUDE_MAILBOX_NAME"]
        # Should look like "koatag@<hostname>"
        assert name2.startswith("koatag@"), f"expected koatag@<host>, got {name2}"
        print(f"[smoke] <role>@<hostname> default ok — {name2}")

        # ---- Test 3: existing file → fail (exit 3) ----
        # proj1 already has .mcp.json from test 1
        r3 = run(["--hub", "http://hub:1905",
                  "--token", "abc",
                  "--name", "wiki",
                  "--project", str(proj1),
                  "--skip-probe"])
        assert r3.returncode == 3, f"expected 3 (FileExistsError), got {r3.returncode}"
        assert "exists" in r3.stderr or "exists" in r3.stdout, \
            "should print 'exists' helper"
        print("[smoke] refuses to overwrite existing .mcp.json ok")

        # ---- Test 4: --force overwrites ----
        r4 = run(["--hub", "http://hub2:1905",
                  "--token", "newtoken",
                  "--name", "wiki",
                  "--project", str(proj1),
                  "--skip-probe",
                  "--force"])
        assert r4.returncode == 0
        cfg4 = json.loads((proj1 / ".mcp.json").read_text(encoding="utf-8"))
        assert cfg4["mcpServers"]["mailbox"]["env"]["CLAUDE_MAILBOX_REMOTE"] == "http://hub2:1905", \
            "--force did not overwrite REMOTE field"
        assert cfg4["mcpServers"]["mailbox"]["env"]["CLAUDE_MAILBOX_TOKEN"] == "newtoken"
        print("[smoke] --force overwrite ok")

        # ---- Test 5: missing --token → exit 2 (after interactive prompt with empty input) ----
        proj5 = workdir / "proj5"
        proj5.mkdir()
        # Provide empty token via stdin
        r5 = run(["--hub", "http://hub:1905",
                  "--project", str(proj5),
                  "--skip-probe"],
                 input_text="\n")  # empty token
        assert r5.returncode == 2, f"expected 2 (missing token), got {r5.returncode}"
        assert "token is required" in r5.stderr, f"stderr: {r5.stderr!r}"
        print("[smoke] missing token → exit 2 ok")

        # ---- Test 6: probe failure (bogus hub) → exit 1 ----
        proj6 = workdir / "proj6"
        proj6.mkdir()
        r6 = run(["--hub", "http://nonexistent-host-12345.invalid:1905",
                  "--token", "abc",
                  "--name", "wiki",
                  "--project", str(proj6)],
                 timeout=20)
        assert r6.returncode == 1, f"expected 1 (probe failed), got {r6.returncode}"
        assert "unreachable" in r6.stderr or "Errno" in r6.stderr, \
            f"expected unreachable err in stderr: {r6.stderr!r}"
        # No .mcp.json should have been written
        assert not (proj6 / ".mcp.json").exists(), \
            "probe-failure path should NOT write .mcp.json"
        print("[smoke] probe failure → exit 1 + no .mcp.json ok")

        # ---- Test 7: --skip-probe with non-existent hub still works (offline mode) ----
        proj7 = workdir / "proj7"
        proj7.mkdir()
        r7 = run(["--hub", "http://nonexistent-host-12345.invalid:1905",
                  "--token", "abc",
                  "--name", "wiki",
                  "--project", str(proj7),
                  "--skip-probe"])
        assert r7.returncode == 0, \
            f"--skip-probe should bypass network: exit={r7.returncode} stderr={r7.stderr!r}"
        assert (proj7 / ".mcp.json").exists()
        print("[smoke] --skip-probe bypasses network ok")

        # ---- Test 8: gitignore warning when .mcp.json not in .gitignore ----
        proj8 = workdir / "proj8"
        proj8.mkdir()
        (proj8 / ".gitignore").write_text("node_modules/\n*.pyc\n", encoding="utf-8")
        r8 = run(["--hub", "http://hub:1905",
                  "--token", "abc",
                  "--name", "wiki",
                  "--project", str(proj8),
                  "--skip-probe"])
        assert r8.returncode == 0
        # Warning may go to stderr
        combined = r8.stdout + r8.stderr
        assert ".mcp.json" in combined and "gitignore" in combined.lower(), \
            f"expected gitignore warning: {combined[:300]}"
        print("[smoke] gitignore warning ok")

        print(f"\n[smoke] ALL BOOTSTRAP SPOKE TESTS PASSED")
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
