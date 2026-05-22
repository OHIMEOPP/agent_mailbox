# Repository structure — current state + future proposal

User said「整理專案結構」on 2026-05-23 ~07:48 local. This file
documents the current layout, what's grown into it during the overnight
build, and a proposed (non-executed) reorganization. Do **not** move
anything until you've read the "Risks" section and decided what to keep
stable.

---

## Current layout (post-overnight, 21 commits)

```
claude-mailbox/                        25+ Python files at repo root
├── server.py                          # stdio MCP server (per-instance subprocess)
├── mailbox-server.py                  # hub REST/SSE server (:1905)
├── mailbox-watch.py                   # watcher subprocess (local OR --remote SSE)
│
├── mailbox_audit.py                   # shared module — audit_log table + log_event
├── mailbox_backup.py                  # shared module — backup_once, restore, stats
├── mailbox_migrations.py              # shared module — versioned schema runner
├── mailbox_priority.py                # shared module — priority lane labels
├── mailbox_rate_limit.py              # shared module — sliding-window
├── mailbox_reactions.py               # shared module — reactions table
├── mailbox_scheduled.py               # shared module — scheduled-send queue
├── mailbox_sweep.py                   # shared module — retention sweep
├── mailbox_webhooks.py                # shared module — outbound HTTP webhooks
│
├── mailbox-attach.py                  # CLI — cross-device file send (peer-to-peer)
├── mailbox-audit.py                   # CLI — audit log inspector
├── mailbox-backup.py                  # CLI — manual backup + restore
├── mailbox-discord-file.py            # CLI — push file to user Discord DM (port 1904)
├── mailbox-dump.py                    # CLI — chat history dumper (--tree --include-scheduled --audit-trail)
├── mailbox-rate-limit.py              # CLI — rate-limit inspector
├── mailbox-retention.py               # CLI — manual sweep / dry-run / stats
├── mailbox-scheduled.py               # CLI — scheduled-send queue inspector
├── mailbox-stats.py                   # CLI — read-only activity report
├── mailbox-webhooks.py                # CLI — webhook subscription manager
├── bootstrap-spoke.py                 # CLI — onboarding wizard for new spoke
│
├── smoke_test_aliases.py              # 14 smoke test files
├── smoke_test_attach.py
├── smoke_test_audit.py
├── smoke_test_backup.py
├── smoke_test_bootstrap.py
├── smoke_test_claim.py
├── smoke_test_dump_tree.py
├── smoke_test_integration.py
├── smoke_test_migrations.py
├── smoke_test_priority.py
├── smoke_test_rate_limit.py
├── smoke_test_reactions.py
├── smoke_test_retention.py
├── smoke_test_scheduled.py
├── smoke_test_search.py
├── smoke_test_stats.py
├── smoke_test_threading.py
├── smoke_test_webhooks.py
│
├── mailbox-dump.py                    # (duplicate listed for clarity)
├── server.py                          # (top of file)
│
├── bridge/                            # already organized: docker-compose.yml + Python package
├── examples/                          # mcp.json templates
├── snapshot/                          # global config mirror
│
├── README.md / HOW-TO-* / SETUP-CROSS-DEVICE.md / etc.
```

**Counts** (as of head `b1b2cdc`):
- 3 entry-point scripts (server.py / mailbox-server.py / mailbox-watch.py)
- 9 shared modules (`mailbox_*.py` — importable, underscore-named)
- 11 admin / CLI tools (`mailbox-*.py` + `tools/bootstrap-spoke.py`)
- 14 smoke test files (`smoke_test_*.py`)
- = **37 Python files at root**

---

## Proposed target layout

```
claude-mailbox/
├── server.py                          ← stays at root (entry point referenced
├── mailbox-server.py                  ←  by docker-compose.yml mount paths +
├── mailbox-watch.py                   ←  by SETUP-CROSS-DEVICE.md docs)
│
├── mailbox/                           ← shared modules go here as a package
│   ├── __init__.py
│   ├── audit.py                       (renamed from mailbox_audit.py)
│   ├── backup.py
│   ├── migrations.py
│   ├── priority.py
│   ├── rate_limit.py
│   ├── reactions.py
│   ├── scheduled.py
│   ├── sweep.py
│   └── webhooks.py
│
├── tools/                             ← all admin CLI scripts
│   ├── attach.py                      (renamed mailbox-attach.py → tools/attach.py)
│   ├── audit.py
│   ├── backup.py
│   ├── discord-file.py
│   ├── dump.py
│   ├── rate-limit.py
│   ├── retention.py
│   ├── scheduled.py
│   ├── stats.py
│   ├── webhooks.py
│   └── bootstrap-spoke.py
│
├── tests/                             ← all smoke tests
│   ├── conftest.py                    (optional; pytest fixtures shared)
│   └── smoke_test_*.py
│
├── bridge/                            ← unchanged
├── examples/                          ← unchanged
├── snapshot/                          ← unchanged
└── *.md                               ← docs at root
```

**Benefits:**
- Root drops 37 → 3 Python files (just entry points)
- Clear separation: modules (importable, library) vs tools (executable, admin) vs tests
- `from mailbox.audit import log_event` reads more naturally than `import mailbox_audit`
- Python convention: package directory > flat namespace prefix
- Easier onboarding for contributors — folder names self-document

**What stays at root:**
- 3 entry-point scripts (docker mounts + SETUP doc cite these by name)
- `*.md` docs
- `.mcp.example.json` / `.gitignore` / etc.

---

## Risks (why this wasn't auto-executed)

### 1. Docker compose mount paths
`bridge/docker-compose.yml` mounts specific Python files into the
container:

```yaml
- ../mailbox-server.py:/app/mailbox-server.py:ro
- ../mailbox_sweep.py:/app/mailbox_sweep.py:ro
- ../mailbox_audit.py:/app/mailbox_audit.py:ro
... (one mount per shared module)
```

Moving modules to `mailbox/` requires either:
- Updating every mount line, OR
- Mounting the whole `mailbox/` package (`../mailbox:/app/mailbox:ro`)

Plus the `import mailbox_audit` → `from mailbox import audit` (or
`from mailbox.audit import ...`) churn in `mailbox-server.py` /
`server.py` / `mailbox-watch.py`.

### 2. CLI invocation paths in user docs + memory
README / SETUP-CROSS-DEVICE / morning briefing / catalogue page all cite
CLI paths like `py tools/mailbox-retention.py --once`. Renaming to
`py tools/retention.py --once` requires updating ~20+ doc mentions and
the user's memory of "the file is called X". Less critical for the
short-form CLI tools because relative paths don't break, but the user's
muscle memory does.

### 3. Smoke test sibling-import pattern
Several smokes do `sys.path.insert(0, str(Path(__file__).parent))` to
import sibling modules. If smokes move into `tests/`, the path math
becomes `parent.parent` — mechanical but easy to miss one file.

### 4. Git blame continuity
Mass renames lose blame. Important for a 2-day-old codebase actively
evolving; less so for stable code. Mitigate with `git mv` + the
`--follow` flag.

### 5. Open work in flight (DO NOT reorganize mid-PR)
Mailbox-dev had `mailbox_priority.py` + `smoke_test_priority.py` in
working tree when this proposal was written. Reorganization MUST wait
until all in-flight commits are landed.

---

## Suggested execution order (after user approves)

1. **Phase 1 — Smoke tests only** (lowest risk; nothing else imports them):
   - Create `tests/` dir
   - `git mv smoke_test_*.py tests/`
   - Update `here = Path(__file__).parent` references inside each smoke
     to `Path(__file__).parent.parent` for sibling-module imports
   - Run all smokes from new location → verify still pass
   - Single commit "chore(tests): move smokes into tests/ dir"

2. **Phase 2 — Tools** (medium risk; CLI invocation paths in docs change):
   - Create `tools/` dir
   - `git mv mailbox-*.py tools/` (rename to drop `mailbox-` prefix)
   - `git mv bootstrap-spoke.py tools/bootstrap-spoke.py`
   - Update `tools/mailbox-discord-file.py` import paths if any (none expected)
   - Update SETUP-CROSS-DEVICE.md / README.md to cite new paths
   - Update memory references in life_wiki where applicable

3. **Phase 3 — Modules** (highest risk; affects docker + import statements):
   - Create `mailbox/` package with `__init__.py`
   - `git mv mailbox_*.py mailbox/` and rename (drop `mailbox_` prefix
     in the new file inside package)
   - Update every `import mailbox_<name>` → `from mailbox import <name>`
     across server.py / mailbox-server.py / mailbox-watch.py / tools/*
   - Update `bridge/docker-compose.yml` mounts
   - Docker restart + run integration smoke
   - Commit "refactor(modules): consolidate mailbox_* modules into
     mailbox/ package"

4. **Phase 4 — Validate**:
   - Run full smoke suite from new locations
   - Docker re-deploy + /health check
   - Test cross-device with spoke (mock or real)

Each phase is independently committable + revertable. **Don't do all
three in one commit.**

---

## Decision needed from user

- (a) **Reorganize now** — wiki executes Phase 1+2 this overnight session
      (smoke + tools), leaves Phase 3 (modules + docker) for daytime when
      operator can baby-sit docker restart
- (b) **Reorganize later** — keep current flat layout; revisit when adding
      more features makes the pile unwieldy
- (c) **Partial: smokes only** — just Phase 1 tonight; defer 2+3

Default if no decision: **(c) partial** in next iteration if cutoff allows,
otherwise leave flat and revisit.

---

## Inventory at proposal time (head `b1b2cdc`)

37 Python files at repo root + bridge/ examples/ snapshot/ subdirs.
- Total LOC across `mailbox*.py` files: large but not the metric — the
  symptom is *cognitive load when ls-ing the repo*, not file size.

Recommend running this command on morning to see the actual count
before deciding:

```bash
ls *.py | wc -l
```
