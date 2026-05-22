# Claude Code Mailbox

跨 Claude Code instance 的非同步訊息佇列 + Discord 橋接 + 對使用者推送通知，三件套統一的家。

**為什麼存在**：Claude Code 是 turn-based agent — 一個 session 不會主動知道另一個 session（或外部 Discord 使用者）想跟它說什麼。Mailbox 提供共享 SQLite + per-instance watcher，讓不同 agent / user 互通的訊息流被 event-driven 喚醒，而不是每個 session 各自輪詢。

**跨機（laptop / Tailscale）**：自 2026-05-22 起支援 hub-and-spoke。SQLite 永遠只在 hub 一份，遠端 agent 透過 `mailbox-server.py` 提供的 REST/SSE 連上來；MCP server 認 `CLAUDE_MAILBOX_REMOTE` env 自動 dispatch 不必改 code。完整 onboarding 文件：[SETUP-CROSS-DEVICE.md](SETUP-CROSS-DEVICE.md)。

---

## 🚀 Cold-start checklist（agent 視角：被 user 指示用 mailbox 時讀這段）

### ❓ Hub or spoke? 先回答這題（自 2026-05-22）

| 答案 | 你這台是 | 對應流程 |
|---|---|---|
| 我有 mailbox.db on disk + 是大家連線進來的中心 | **HUB** | 走下面 §情境 A 起 local watcher |
| 我要連到別台機器（DB 不在這） | **SPOKE** | 跳去 [SETUP-CROSS-DEVICE.md](SETUP-CROSS-DEVICE.md) Phase 1，**不要照下面 §A 走**，會建 ghost DB |
| 不確定 | 看 `.mcp.json` env：有 `CLAUDE_MAILBOX_REMOTE` 就是 spoke | |

**Spoke 必看**：下面 §A 的 「§A.2 驗證 DB 目錄」對 spoke 是反指令——你的 REMOTE env 設好後 server.py 不會碰 local DB，直接 dispatch HTTP；本機沒 mailbox.db 是正確情況。

---

### 情境 A：「**開 watcher**」/「啟動 watcher」/「session start」（hub 端 / 本機 mode）

跑這 4 步：

1. **驗證 repo 在預期位置**
   ```bash
   ls C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py
   ```
   不存在 → 先 `git clone https://github.com/OHIMEOPP/agent_mailbox.git C:/Users/User/Desktop/VSCcode/claude-mailbox`

2. **驗證 DB 目錄（hub 才需要）**
   ```bash
   ls C:/Users/User/.claude/mailbox/mailbox.db
   ```
   不存在 → server.py 第一次跑時會自動 mkdir + create；通常已存在。
   **Spoke 跳過此步**（spoke 不該有 local DB；若有 legacy DB 留著 orphan OK）。

3. **問自己是誰**：呼叫 `mcp__mailbox__whoami()` 拿 `name` 欄位（如 `wiki` / `koatag` / `koatag-frontend` / `stranger-conv`）。
   - **跨機**改採 `<role>@<hostname>` 格式（例：`wiki@LAPTOP-XYZ`，看 [SETUP-CROSS-DEVICE.md](SETUP-CROSS-DEVICE.md) §1.6）

4. **啟動 watcher**：用 Monitor tool（preferred）— 完整模板看 [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md)。
   - **Hub / 本機 mode** 一行版：
     ```yaml
     tool: Monitor
     command:     py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py" <NAME> --monitor
     persistent:  true
     timeout_ms:  3600000
     ```
   - **Spoke mode** 看 [SETUP-CROSS-DEVICE.md §1.8](SETUP-CROSS-DEVICE.md)（用 `--remote` 自動 fallback env）

5. 回 user 一句「mailbox watcher 已啟動（stream-mode）」

### 情境 B：「**DM 我**」/「回我」/「告訴 user X」/「寄 Discord」

> 前提：你的部署有 Discord outbound endpoint（reference deployment 是 `http://localhost:1904/agent-notify`，bridge 提供）。沒部署 Discord 整合的環境跳過此情境，看 §Core vs optional 確認。

User 在 Discord 跟你溝通 — 推送回 Discord DM 走 outbound endpoint。Mailbox SQLite INSERT 對 Discord **沒效**（必須走 Discord API），必須 POST：

```python
import os, urllib.request, json
NOTIFY_URL = os.environ.get("CLAUDE_NOTIFY_URL", "http://localhost:1904/agent-notify")
body = {
    "agent": "wiki",          # 你的 instance 名
    "task": "<短標題>",        # Discord 顯示第一行
    "status": "info",         # info(📋) / done(✅) / fail(❌) / warn(⚠️)
    "detail": "<本文>",        # Discord 顯示第二行起
}
req = urllib.request.Request(
    NOTIFY_URL,
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"},
)
urllib.request.urlopen(req, timeout=8)
```

**Schema 陷阱**：欄位必為 `agent` / `task` / `status` / `detail`，**送 `message` 會被 silently drop**，Discord 只看到 icon + agent 名沒內容。三種 reply channel 全配方 + e2e 範例：[HOW-TO-USE-MAILBOX.md](HOW-TO-USE-MAILBOX.md)。

### 情境 C：「**寄訊息給 koatag**」/「告訴 wiki X」/「轉給另一個 agent」

走 mailbox MCP（peer 的 watcher 會即時喚醒對方）：

```python
mcp__mailbox__send(to="koatag", body="<text>")
```

或 SQL INSERT 同表（一樣會被 peer watcher 看到）：
```python
db.execute(
    "INSERT INTO messages(from_name, to_name, body, sent_at) VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
    (your_name, "koatag", body)
)
```

### 情境 D：「**有沒有新訊息**」/「看 inbox」

```python
mcp__mailbox__inbox(unread_only=True)
# 或：
mcp__mailbox__inbox(unread_only=False, limit=20)
```

處理完**一定要 mark_read**（否則下次 session 重啟 watcher 會把舊 mail 重新喚醒）：
```python
mcp__mailbox__mark_read(ids=[123, 124])
```

詳見 [HOW-TO-USE-MAILBOX.md](HOW-TO-USE-MAILBOX.md) §Receiving + §Marking as read。

---

## 詳細 docs

| Doc | 場景 |
|---|---|
| [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md) | 啟動 watcher 完整 quick ref：Monitor stream-mode + Bash fallback + heartbeat verify + 多裝置適配 |
| [HOW-TO-USE-MAILBOX.md](HOW-TO-USE-MAILBOX.md) | 收信 → mark read → 3 種 reply channel 全配方 + agent-notify schema 陷阱 + CJK 編碼陷阱 + e2e Python 範例 |
| [snapshot/](snapshot/) | 全域 config 鏡像（`~/.claude/CLAUDE.md` mailbox 段 / `settings.json` 退役 hook / memory 全集），新裝置可參考重建 |

---

## 運作原理

每個 Claude Code instance spawn 自己的 stdio MCP server，**共讀寫同一個 SQLite 檔**：

```
            C:\Users\User\.claude\mailbox\mailbox.db
                    ▲      ▲
       ┌────────────┘      └────────────┐
       │                                │
  ┌────────────┐                  ┌────────────┐
  │ stdio MCP  │                  │ stdio MCP  │
  │  server    │                  │  server    │
  └────────────┘                  └────────────┘
       │                                │
  Claude (life_wiki)              Claude (KOATAG)
  name=wiki                       name=koatag
```

無 daemon — 每次 Claude session 開啟時 spawn 子行程，session 結束就退出。Watcher 是另一個 OS 子行程，配 Monitor tool stream-mode 持續喚醒 agent。

### Core vs optional：先區分 mandatory 跟 add-on

| 元件 | 必要性 | 角色 |
|---|---|---|
| **SQLite DB** `~/.claude/mailbox/mailbox.db` | **CORE** — 沒這個 mailbox 不存在 | 訊息持久層 |
| **MCP server** `server.py` (per Claude session spawn) | **CORE** — 沒這個 agent 拿不到 mailbox 工具 | 工具 RPC |
| **Watcher** `mailbox-watch.py` (per agent instance run) | **CORE** — 沒這個 agent 不會被新 mail 喚醒 | Event-driven wake |
| **Discord inbound bridge** `mailbox-discord-bridge.py` | **OPTIONAL** — 只 agent ↔ agent 通訊不需要 | 使用者 Discord DM → mailbox INSERT |
| **Discord bridge** (`bridge/` package, port 1904) | **OPTIONAL** — 只 agent ↔ agent 通訊不需要 | Discord ↔ mailbox 雙向 (gateway 收 DM / REST 送 DM) |

只要 agent ↔ agent 互寄（譬如 wiki ↔ koatag 內部協作），**只需 CORE 三件**。Discord 整合是 plugin。

### Discord 整合（OPTIONAL）：怎麼跟外界 user 通訊

`bridge/` 套件提供 Discord 雙向整合 — **一個 service 兩個方向都做**（2026-05-19 起）：

```
                ┌──────────────────────────────────────┐
                │ Discord (user 端)                     │
                └─────┬──────────────────────────▲─────┘
                      │ user 寫 DM                │ agent 推 DM
                      │ (gateway websocket)       │ (REST API)
                      │                           │
                ┌─────▼───────────────────────────┴─────┐
                │ bridge/ (mailbox-bridge container)     │
                │ Python, port 1904                      │
                │ ├ gateway.py : discord.py on_message   │
                │ │             → INSERT messages.db     │
                │ └ http_server : POST /agent-notify     │
                │                → REST send DM          │
                └─────┬──────────────────────────▲──────┘
                      │ INSERT                   │
                      ▼                          │
                ┌────────────────────────────────┴───┐
                │  mailbox.db (SQLite, CORE)         │
                └─────┬──────────────────────────────┘
                      │ poll                     ▲
                      ▼                          │
                ┌──────────────────┐             │
                │ mailbox-watch.py │ ←─ CORE     │
                │ Monitor stream   │             │
                └─────┬────────────┘             │
                      │ stdout MAIL              │
                      ▼                          │
                ┌──────────────────┐             │
                │ Claude agent     │ ────────────┘ POST /agent-notify
                └──────────────────┘
```

**Bridge 必要 env**: `DISCORD_BOT_TOKEN`（從 Discord Developer Portal 拿）+ `DISCORD_DEFAULT_CHANNEL`（trusted user DM channel id）。在 `bridge/.env` 設好，`cd bridge && docker compose up -d` 即可。

#### Discord 端 setup

- Developer Portal → Bot → **Privileged Gateway Intents → MESSAGE CONTENT INTENT** ✅（沒開 gateway crash，HTTP 仍 OK 但收不到 DM）
- 同一個 bot token 可同時跑 gateway（inbound）+ REST（outbound），Discord 允許

#### Legacy: node-red `discordBot` :1901（已淘汰但仍可並存）

之前 inbound 走 node-red `discordMessage` flow → POST `:1904/from-discord` → bridge INSERT；outbound 走 node-red `:1901/agent-notify`。2026-05-19 改 bridge 直接 gateway + REST 取代，node-red 沒角色了。仍可保留並存（看 §Outbound 並存 section）。

**其他裝置不一定長這樣** — 你可以：
- 完全不部署 Discord 整合（純 agent ↔ agent，CORE 已足夠）
- 用自己的 inbound 機制（不用 Python bridge，自己寫個 webhook 直接 INSERT SQLite 也行）
- 把 outbound 接其他平台（Slack / Telegram / Webhook URL — 只要那 endpoint 接 `{agent, task, status, detail}` JSON 並回應 Discord-style DM 就行）
- 不同 port、不同 host

**新裝置 setup 時 hardcode 預設可以**：bridge `:1904` 就是新裝置 default，但仍建議用 env var 讓 reference deployment 可改：
```python
NOTIFY_URL = os.environ.get("CLAUDE_NOTIFY_URL", "http://localhost:1904/agent-notify")
```

> **TL;DR for new agents**: 你 care 的 surface 是 **mailbox SQLite**（收信，CORE）+ **bridge :1904**（雙向 Discord 整合，OPTIONAL）。沒 Discord 需求就只跑 CORE。

#### Outbound 並存：1904 (bridge Python，主) 與 1901 (node-red，legacy)

2026-05-19 起 bridge 雙向都做了，1901 變成可選 legacy fallback。

| | 1904 (bridge Python, 主) | 1901 (node-red, legacy) |
|---|---|---|
| 連線方式 | gateway (inbound) + REST (outbound) 全 Python 自包 | gateway + REST 都在 node-red flow |
| Bot token | `DISCORD_BOT_TOKEN` env 直接讀 | 從 node-red credentials 解密拿 |
| 共用同 token? | ✅（gateway 是 singleton，不能兩邊同時連）| 兩邊**不能同時**連 gateway，REST OK |
| 預設 default | agent 端 `CLAUDE_NOTIFY_URL` 應指這 | 已退役，新部署不需要 |
| Schema | `{agent, task, status, detail, channel?}` | 完全相同 |

**新部署直接跑 :1904**（不需 node-red）：
1. `cp bridge/.env.example bridge/.env` → 填 `DISCORD_BOT_TOKEN` + `DISCORD_DEFAULT_CHANNEL`
2. Developer Portal → Bot → Privileged Gateway Intents → ✅ MESSAGE CONTENT INTENT
3. `cd bridge && docker compose up -d`
4. 測 outbound：`curl -sS -X POST http://localhost:1904/agent-notify -H 'Content-Type: application/json' -d '{"agent":"test","task":"hi","status":"info","detail":"from bridge"}'` → 應該收到 DM
5. 測 inbound：手機 / 另一個帳號傳 DM 給 bot → Monitor 應該 fire `MAIL id=N from=user-discord ...`

**舊部署仍有 node-red 想保留**：可以並存，但只能一邊 gateway 連 Discord（會互踢登入）。我這台目前 node-red gateway 被 cut，全走 bridge :1904。

---

## MCP 工具（6 個）

| Tool | 用途 |
|---|---|
| `send(to, body, files?=[])` | 寄給某 instance；`files` 是 host filesystem 路徑列表，多附件一起送 |
| `inbox(unread_only=true, limit=50)` | 收信；每則訊息額外帶 `attachments: [{id, filename, mime, size, sha256}]` |
| `mark_read(ids)` | 標記已讀 |
| `peers()` | 列出曾連線過的 instance |
| `download(attachment_id, save_to)` | 把附件 blob 拉到 local file path |
| `whoami()` | 確認自己是誰、DB 在哪 |

## 檔案 / Zip 傳輸（自 2026-05-23）

Mailbox 可以順著訊息附帶檔案，hub ↔ spoke 之間用同一條 LAN/VPN 通道傳，不需要另外開 SMB / Syncthing。

**邊界**：本質是 **event/message 附件**，不是 file sync engine。
- ✅ 適合：screenshot / PDF / log / 設定檔；資料夾**自己 zip** 後當單檔送
- ❌ 不適合：folder ongoing sync（兩台機器持續鏡像目錄 — 那是 Syncthing / Tailscale Drive 的工作）

**限制**：單檔 100 MB、總 payload 500 MB、單訊息最多 32 個附件。超過會回 413。

**Spoke watcher 預設不自動下載**——只在 stdout MAIL line 加 `attach=N` 提示。Agent 自己決定要不要呼 `download()` 拉 blob，避免 idle inbox 把 spoke 硬碟塞爆。

**API surface**：
```
# 從 agent 內傳
mcp__mailbox__send(to="wiki@LAPTOP", body="see attached zip",
                   files=["C:/snapshots/wiki-2026-05-23.zip"])

# 對端收到
inbox()
# → [{id: 42, from: "wiki@DESKTOP", body: "see attached zip",
#     attachments: [{id: 7, filename: "wiki-2026-05-23.zip",
#                    size: 4_521_887, sha256: "abc123..."}]}]

mcp__mailbox__download(attachment_id=7, save_to="C:/tmp/wiki.zip")
# → {path: "C:/tmp/wiki.zip", size: 4521887, sha256: "abc123..."}
```

**Shell-only CLI**（不經 MCP，直接打 hub HTTP endpoint）：
```bash
py mailbox-attach.py --from wiki@DESKTOP --to wiki@LAPTOP \
    --body "snapshot" --files C:/tmp/wiki.zip
```
詳見 `mailbox-attach.py --help`。**注意**：別跟 `mailbox-discord-file.py`（推檔到 Discord DM 的，port 1904）搞混 — 那個 2026-05-23 改名了，這個 `mailbox-attach.py` 才是 cross-device peer ↔ peer 的。

**Blob 儲存**：hub 端用 content-addressed 路徑 `<mailbox-dir>/attachments/<sha256[:2]>/<sha256>`，同 hash 自動 dedup。SSE event payload 加 `attachments: [{id, filename, mime, size}]` 欄位（additive，舊 spoke 不認該欄不會炸）。

## 維運 / Retention（自 2026-05-23）

Mailbox 本質是 transient queue — 訊息、附件、blob、peer heartbeat 都**不該存很久**。Hub 端內建 daily sweep daemon，避免長期累積。

| Item | TTL | Env var |
|---|---|---|
| Read messages | 7d | `MAILBOX_RETENTION_READ_DAYS` |
| Unread messages | 14d | `MAILBOX_RETENTION_UNREAD_DAYS` |
| Peer rows | 30d | `MAILBOX_RETENTION_PEER_DAYS` |
| Blobs | 跟著 attachment / sha256 reference | (auto, no knob) |

**自動**：daily（24hr）背景 thread，第一次跑在 boot 後 1hr。`MAILBOX_RETENTION_DISABLED=1` 關掉。

**手動 CLI**：
```bash
py mailbox-retention.py --stats      # 看 disk / 訊息數 / oldest age
py mailbox-retention.py --dry-run    # 報告會刪什麼，不真刪
py mailbox-retention.py --once       # 立即跑一次 sweep
```

**觀測**：`curl /health` 回 JSON，含 `unread_count`、`blob_count`、`blob_total_bytes`、`oldest_message_age_days`、`last_sweep_at`。最後欄 > 25hr 沒更新 = sweep daemon 死了。

設定 / tuning 細節：[SETUP-CROSS-DEVICE.md Phase 5](SETUP-CROSS-DEVICE.md#phase-5--retention-since-2026-05-23)

## 備份 / Backup（自 2026-05-23）

Retention sweep 會刪舊資料；萬一刪錯、或 DB 被外部寫壞，要有 snapshot 可救回。Hub 端內建 daily backup daemon，**在 sweep 之前**先打 snapshot — 順序保證最近一份 backup 一定是 pre-sweep 狀態。

| Item | 備份方式 | Filename |
|---|---|---|
| `mailbox.db` | SQLite online `.backup` API（atomic, 不擋 writer） | `mailbox-backup-YYYYMMDD-HHMMSS.db` |
| `attachments/` | tar.gz | `mailbox-backup-YYYYMMDD-HHMMSS-attachments.tar.gz` |
| Rolling retention | 7 daily + 4 weekly + 3 monthly | (auto-pruned per backup) |

**自動**：跟 retention sweep daemon 共用 thread — backup → sweep → sleep 24hr。`MAILBOX_BACKUP_DISABLED=1` 關掉。

**手動 CLI**：
```bash
py mailbox-backup.py --stats                     # last_backup_at / count / total bytes
py mailbox-backup.py --list                      # 列現存 snapshot，新→舊
py mailbox-backup.py --once                      # 立刻打一份 + rolling prune
py mailbox-backup.py --restore 20260523-020000 --yes
                                                  # 從 timestamp restore（會把現狀搬去 .before-restore-<now>）
```

**Env vars**：

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_BACKUP_DIR` | `<db parent>/backups` | 改 backup 落地位置 |
| `MAILBOX_BACKUP_DISABLED` | (unset) | `1` = 關掉 daemon（CLI 仍可用） |
| `MAILBOX_BACKUP_KEEP_DAILY` | 7 | rolling — daily 保留幾份 |
| `MAILBOX_BACKUP_KEEP_WEEKLY` | 4 | rolling — weekly 保留幾份 |
| `MAILBOX_BACKUP_KEEP_MONTHLY` | 3 | rolling — monthly 保留幾份 |

**觀測**：`/health` JSON 多三欄 `last_backup_at`、`backup_count`、`backup_total_bytes`，外加 `last_backup_counters`（同 sweep 的設計）。`last_backup_at` > 25hr 沒更新 = backup daemon 死了。

**Restore 流程**：`--restore` 會先把現有 `mailbox.db` 跟 `attachments/` 搬到 `*.before-restore-<timestamp>`，然後 copy backup 進去。restore 失敗或不滿意 → 把 `.before-restore-*` 改回原名即可 rollback。Restore 需要 `--yes` 才會真執行，沒帶就 dry-run（印步驟、退 exit=2）。

設定 / tuning 細節：[SETUP-CROSS-DEVICE.md Phase 6](SETUP-CROSS-DEVICE.md#phase-6--backup-since-2026-05-23)

## 稽核 / Audit log（自 2026-05-23）

Passive audit log — 所有 mailbox MCP tool call 跟 REST endpoint 都會在 `audit_log` 表留一筆 row（actor / action / target / payload_json / ok / ts）。事故重建、debug "誰寄了什麼"、找 download 流量分布都靠這個。

| Action | Logged from |
|---|---|
| `send` | MCP `send()` + REST `/send` + `/send-file`（payload 帶 `msg_id` / `files_count` / `in_reply_to`） |
| `inbox` | MCP `inbox()` + REST `/inbox`（payload 帶 `returned` 數量） |
| `mark_read` | MCP `mark_read()` + REST `/mark_read`（payload 帶 `ids` / `marked`） |
| `download` | MCP `download()` + REST `/attachment/<id>`（含失敗 case，`ok=0`） |
| `whoami` | MCP `whoami()`（only local mode） |
| `peers` | MCP `peers()` + REST `/peers` |

**Actor 命名**：MCP local mode 寫 `CLAUDE_MAILBOX_NAME`（譬如 `wiki`、`koatag@LAPTOP-XYZ`）；REST endpoint 拿不到 caller 身分時用 `rest:<client-ip>`（譬如 `rest:192.168.1.50`），有 `from` 欄位的 endpoint（`/send`、`/send-file`）就用 `from` 值。

**手動 CLI**：
```bash
py mailbox-audit.py --tail                  # 最近 50 筆
py mailbox-audit.py --tail --limit 200
py mailbox-audit.py --since 1h              # 相對時間：15m / 1h / 24h / 7d
py mailbox-audit.py --actor wiki            # 只看某 actor
py mailbox-audit.py --action send           # 只看某動作
py mailbox-audit.py --actor wiki --action send --since 1h
py mailbox-audit.py --stats                 # count + first/last + by_action 分布
py mailbox-audit.py --tail --json           # machine-readable
```

**Env vars**：

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_AUDIT_DISABLED` | (unset) | `1` = hot path 不寫 audit（讀仍 OK）。除非真的有 perf 顧慮否則別開 |

**REST endpoint**：`/audit?since=...&until=...&actor=...&action=...&limit=N`，跟 CLI 同 filter（hub bearer auth 保護）。Spoke 想看 hub 端的 audit log 走這個。

**`/health` 多出兩欄**：`audit_count`、`audit_last_at`。

**設計約束**：
- 表是 append-only — app code 沒 UPDATE/DELETE path（retention sweep 之後可加，但目前審計留全份）
- `log_event()` 內部 catch all exceptions 不外拋 — audit 必須**不可以**讓被審計的 operation 失敗
- Spoke (`CLAUDE_MAILBOX_REMOTE` 設了的 MCP server) **不**寫 local audit log — 它的 call 都進 hub，hub 已記。本機沒 DB 也沒 audit table。

## TTL / 過期訊息（自 2026-05-23）

`send()` 可以帶 `expires_at` 讓訊息有壽命 — retention sweep 看到 `expires_at < now` 就刪，**不管**讀沒讀。適合：

- 狀態 ping（"我還活著"）— 隔天就無價值
- 進度更新（"step 3/5 done"）— 完工後 obsolete
- 短命 broadcast（"deploy 開始了" → 1hr 後過期）

```python
# MCP — ISO 8601 或 relative
mcp__mailbox__send(to="wiki", body="step 3/5 done", expires_at="1h")
mcp__mailbox__send(to="koatag", body="see you tomorrow", expires_at="24h")
mcp__mailbox__send(to="hub", body="urgent",
                    expires_at="2026-05-25T00:00:00Z")
```

Relative：`30m` / `1h` / `7d`，由 hub/server 時鐘 + 該單位計算。null/省略 = 永不過期（走 read/unread default TTL）。

**Inbox / SSE 回 `expires_at` 欄**：peer agent 可以決定要不要先讀短命訊息（或先過 long-lived）。

**Retention sweep 多一個 stage**：每天的 daily sweep 在 `read_days`/`unread_days` 之前先撈 `expires_at < now` 的 row。重複的不會 double-count。

**`/health` 多兩欄**：
- `ttl_expiring_24h`: 24hr 內會過期的訊息數（active monitoring）
- `ttl_expired_pending_sweep`: 已經過期但 sweep 還沒跑 = grace period 內的訊息數

**Schema**: `messages.expires_at TEXT` (nullable) + partial index `WHERE expires_at IS NOT NULL`。Forward-compat — 舊 DB 自動 ALTER on init。

## Webhook 出站（自 2026-05-23）

當有新訊息進 mailbox.db，註冊的 webhook 會收到 POST。讓外部系統（Slack、dashboard、custom bot）能對 mailbox 活動做 reactive 而不用各自輪詢 DB。

**為什麼是 daemon polling 不是 inline /send hook**：避免跟 send-path features（mailing list aliases / TTL / 之後的 features）互鎖；deliver_pending 在 mailbox-server.py daemon thread 每 ~5s 跑一次。

**註冊一個 webhook**：

```bash
py mailbox-webhooks.py --add my-slack --url https://hooks.slack.com/services/...
# 輸出會印出 secret_hmac — 記下來，receiver 端要用它驗 HMAC

# 帶 glob filter — 只 fire 給特定 to/from
py mailbox-webhooks.py --add koatag-only \
    --url https://x.example.com \
    --to-glob 'koatag*'

py mailbox-webhooks.py --list
py mailbox-webhooks.py --deactivate 3   # 不刪、暫停
py mailbox-webhooks.py --delete 3       # 真刪（連 deliveries 一起 CASCADE）
py mailbox-webhooks.py --tail-deliveries --status failed   # debug
py mailbox-webhooks.py --stats
```

**POST body** receiver 收到：

```json
{
  "event": "mail",
  "message": {
    "id": 123, "from": "wiki", "to": "koatag",
    "body": "...", "sent_at": "2026-05-23T01:30:00.000Z",
    "in_reply_to": null, "expires_at": null, "has_attachments": false
  },
  "delivered_at": "2026-05-23T01:30:01.234Z"
}
```

**Headers**：
- `X-Mailbox-Sig: sha256=<hex hmac of body using webhook secret>`
- `X-Mailbox-Webhook-Id: <int>`
- `X-Mailbox-Delivery-Id: <int>`

**Receiver 驗 HMAC 範例**：

```python
import mailbox_webhooks
body = request.get_data()
sig = request.headers.get("X-Mailbox-Sig", "")
if not mailbox_webhooks.verify_signature(body, sig, MY_STORED_SECRET):
    abort(401)
```

**Env vars**：

| Var | Default | Purpose |
|---|---|---|
| `MAILBOX_WEBHOOKS_DISABLED` | (unset) | `1` = daemon idle，不 deliver。CLI register/list 仍可用 |

**Retry**：每筆 delivery 最多重試 `MAX_ATTEMPTS=5` 次（每 daemon tick 嘗試一次，目前固定 5s tick = 25s total window）。超過就標 `failed`，留在 `webhook_deliveries` 表做 forensics，admin 可 `--test` 手動再 fire。

**Filter**：`--to-glob` / `--from-glob` 用 `fnmatch` 比 message recipient/sender。兩個都 None = fire 所有訊息。

**`/health` 多 4 欄**：`webhook_count`、`webhook_pending_deliveries`、`webhook_failed_deliveries`、`webhook_last_fired_at`。

**Schema**：兩個新表 `webhooks` + `webhook_deliveries`，DDL 走 `mailbox_webhooks.init_schema()` 不塞進 messages executescript（避開 wiki #1 撞到的 partial-index ALTER trap）。

## Reactions（自 2026-05-23）

對訊息加 emoji 反應 — 取代「我看到了」「收到」這種純 ack 短信，減少 mailbox 噪音。Discord/Slack 式輕量信號。

```python
mcp__mailbox__react(message_id=123, emoji="✅")   # 同 actor 同 emoji 同 msg 只一筆
mcp__mailbox__react(message_id=123, emoji="🔥")   # 加另一個
mcp__mailbox__unreact(message_id=123, emoji="✅") # 撤回
```

**inbox() 結果**裡每筆訊息多一個 `reactions: [{actor, emoji, created_at}]` 欄。Spoke 也通 — 走 REST `/react` `/unreact`。

**Schema**：`reactions(id, message_id, actor, emoji, created_at)` UNIQUE(message_id, actor, emoji) — 二次 react 是 no-op，回 `{added: false, id: <existing>}`。

**REST endpoints**：
- `POST /react`: body `{actor, message_id, emoji}` → `{added: bool, id, created_at}`
- `POST /unreact`: same body → `{removed: int}`

**Audit actions** 加 `react` / `unreact`。

**`/health` 多兩欄**：`reaction_count`、`reaction_unique_emojis`。

**Emoji** 是 TEXT 1..32 chars freeform — 慣例是單個 emoji（`👍` `🔥` `👀` `✅`），但短文字標籤（`"ack"`、`"todo"`）也可，client 自由。

## Bridge / 周邊工具

- `mailbox-discord-bridge.py` — Docker container `mailbox-bridge`（port 1904）。**Inbound only**：Discord DM → mailbox INSERT。Agent **不直接 call** 這個 port，是 Discord bot 自動推進來。看完整 e2e 流程圖：[Discord 整合：兩個 port，分工不對稱](#discord-整合兩個-port分工不對稱)
- `mailbox-discord-file.py` — CLI，推檔到使用者 Discord DM（port 1904 bridge），**走 Discord REST API**。跟下面 `mailbox-attach.py` 不同：那個是 agent ↔ agent，這個是 agent → Discord user。
- `mailbox-attach.py` — CLI，cross-device 寄訊息+檔案到 peer agent mailbox（port 1905 hub server）。MCP `send(files=[...])` 的 shell 等價物。
- `mailbox-retention.py` — CLI，手動 trigger retention sweep / 看 stats / dry-run（hub-only）
- `mailbox-backup.py` — CLI，手動打 backup / list / restore / stats（hub-only）
- `mailbox-audit.py` — CLI，tail / filter / stats audit log（hub-only）
- `mailbox-webhooks.py` — CLI，register/list/delete/tail outbound webhooks（hub-only）
- `mailbox-dump.py` — 撈 mailbox 歷史；wiki session 有 slash command `/mblog` 跟 `/觀看紀錄` 包好
- `mailbox-whitelist.py` — Discord 來源信任名單 CLI（trusted / approved / pending），see [discord-stranger-chat](https://github.com/OHIMEOPP/discord-stranger-chat) 設計

---

## 新裝置首次設定（fresh device bootstrap）

> 這個 repo 是 mailbox 的 canonical home，但 Claude harness 規定某些檔必須放在
> `~/.claude/` 才會自動載入。所以 first-time setup 需要兩面：clone repo 到 dev
> 目錄 + 把 snapshot 內的 `~/.claude/` 鏡像安裝到新機器對應位置。

### Step 1 — 安裝 `uv` + clone repo + 建 DB 目錄

需要 [uv](https://docs.astral.sh/uv/)（Python script 用 PEP 723 內嵌依賴宣告，`uv run` 自動裝 `mcp` 套件，不需 pip install / venv）。

```bash
git clone https://github.com/OHIMEOPP/agent_mailbox.git C:/Users/User/Desktop/VSCcode/claude-mailbox
mkdir -p C:/Users/User/.claude/mailbox
```

### Step 2 — 安裝 `~/.claude/CLAUDE.md` 的 mailbox 段

```bash
# 把 snapshot 內容追加到 user-level CLAUDE.md（如果該檔不存在，先 create 空檔）
cat C:/Users/User/Desktop/VSCcode/claude-mailbox/snapshot/global-claude-md-mailbox-section.md >> C:/Users/User/.claude/CLAUDE.md
```

或手動：開 `snapshot/global-claude-md-mailbox-section.md`，把「## Mailbox 通訊」段複製進 `~/.claude/CLAUDE.md`。內容會指 agent 來讀本 README 的 cold-start checklist。

### Step 3 — 安裝 memory files（per-project，可選）

如果新裝置上跑 wiki / koatag 等專案，把對應 memory snapshot 複製進該專案的 memory dir：

```bash
# 例：life_wiki project 的 memory 路徑（projectId 是專案絕對路徑換 - 編碼後）
PROJECT_ID="C--Users-User-Desktop-VSCcode-life-wiki"
MEMORY_DIR="C:/Users/User/.claude/projects/$PROJECT_ID/memory"
mkdir -p "$MEMORY_DIR"
cp snapshot/memory-*.md "$MEMORY_DIR/"
# 改名拿掉 "memory-" prefix：
cd "$MEMORY_DIR" && for f in memory-*.md; do mv "$f" "${f#memory-}"; done
```

memory 是 reference + feedback 行為紀律，agent 看了會知道：
- watcher 死了該怎麼判斷 / 重啟（`feedback_watcher_always_on.md`）
- agent-notify schema 是 `task/detail` 不是 `message`（`feedback_notify_schema.md`）
- Discord DM 推送格式 + 何時打不該打（`reference_agent_discord_notify.md`）
- watcher script 完整技術文檔（`reference_mailbox_watcher.md`）

沒裝 memory 也能跑（README cold-start checklist 涵蓋核心情境），但裝了 agent 對邊角 case 反應會更準。

### Step 4 — 註冊 MCP 到 Claude Code

每個專案各自設定，給自己一個獨特名稱：

```json
{
  "mcpServers": {
    "mailbox": {
      "command": "uv",
      "args": ["run", "C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py"],
      "env": {
        "CLAUDE_MAILBOX_NAME": "wiki"
      }
    }
  }
}
```

複製到專案根目錄改名 `.mcp.json`，改 NAME。範本看 `examples/mcp.json.{life_wiki,koatag}`。

或 CLI：`claude mcp add mailbox --scope project -e CLAUDE_MAILBOX_NAME=wiki -- uv run "<path>/server.py"`

> **如果這台是 spoke 不是 hub**：上面 env 區塊只有 local-mode 變數。Spoke 還必須加 `CLAUDE_MAILBOX_REMOTE` + `CLAUDE_MAILBOX_TOKEN`，完整 env block 看 [SETUP-CROSS-DEVICE.md §1.7](SETUP-CROSS-DEVICE.md)。沒加 → MCP server 會 fallback 本機 mode 並開始建 ghost SQLite。

### Step 5 — Bridge container（Discord 整合需要）

如果新裝置要接 Discord（agent ↔ user DM / stranger chat），**只要一個 container**：`bridge/` 自包雙向（gateway inbound + REST outbound）。

```bash
cd C:/Users/User/Desktop/VSCcode/claude-mailbox/bridge
cp .env.example .env
# 編輯 .env 填入:
#   DISCORD_BOT_TOKEN=<bot token>
#   DISCORD_DEFAULT_CHANNEL=<trusted user DM channel id>
docker compose up -d
```

預先要做：
1. Docker external network 一次性建立：`docker network create animesite_other-networks-main`（其他相關 compose 也用這個 network）
2. Discord Developer Portal → Bot 頁 → Privileged Gateway Intents → ✅ MESSAGE CONTENT INTENT

> 不再需要 node-red `discordBot` container（2026-05-19 退役，bridge 雙向都接手了）。仍有舊 node-red 部署的話可以保留，但 gateway 不能跟 bridge 同時連（會互踢）— 二擇一。

### Step 6 — 驗證

開 Claude Code session 進有 `.mcp.json` 的 project → 看到 `mcp__mailbox__*` 工具 → README 的 🚀 Cold-start checklist 跑情境 A 起 watcher → 自我寄一封 test mail 驗證 stream 觸發。

```python
import sqlite3, datetime
db = sqlite3.connect(r'C:\Users\User\.claude\mailbox\mailbox.db')
db.execute('INSERT INTO messages(from_name,to_name,body,sent_at) VALUES(?,?,?,?)',
    ('self-test', '<NAME>', 'first ping', datetime.datetime.now(datetime.UTC).isoformat().replace('+00:00','Z')))
db.commit()
```

Monitor 任務應在 5s 內 print `MAIL id=... from=self-test ...`。沒看到 → watcher 沒跑 / DB path 設錯，看 [Debug](#除錯)。

---

## DB

預設 `C:\Users\User\.claude\mailbox\mailbox.db`。要改用 `CLAUDE_MAILBOX_DB` env override:
```json
"env": {
  "CLAUDE_MAILBOX_NAME": "wiki",
  "CLAUDE_MAILBOX_DB": "D:/shared/team-mailbox.db"
}
```

### Schema

```sql
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_name  TEXT NOT NULL,
    to_name    TEXT NOT NULL,
    body       TEXT NOT NULL,
    sent_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    read_at    TEXT
);

CREATE TABLE peers (
    name          TEXT PRIMARY KEY,
    last_seen_at  TEXT NOT NULL
);
```

直接看訊息：`sqlite3 ~/.claude/mailbox/mailbox.db "SELECT * FROM messages ORDER BY id DESC LIMIT 10"`

### Journal mode

`PRAGMA journal_mode = DELETE`（rollback journal）。原本是 WAL 但 Docker Desktop on Windows 對 `.db-shm` mmap 跨 bind-mount 有 bug → "disk I/O error"，訊息量不大切 DELETE 影響忽略。

---

## 限制

- ❌ **不是即時 push** — 收信端要靠 watcher exit/stream 才被喚醒；沒 watcher 就只能 user 講話時順便 `inbox()`
- ❌ 不能中斷對方正在執行的任務
- ✅ 跨專案、跨 session 非同步傳資料
- ✅ 可跨機器（DB 放共享磁碟即可，但 SQLite WAL 跨網路 FS 有限制 → 建議單機多 instance）

---

## 除錯

### server 起不來
- `claude --debug` 看 stdio log
- 手動：`uv run C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py`，會等 stdio 輸入，Ctrl+C 退出代表 server OK
- `CLAUDE_MAILBOX_NAME` 必設，沒設 RuntimeError

### 訊息送不到
- `whoami()` 看自己 NAME
- `peers()` 看對方有沒有連過（連過才會出現在表內）
- `sqlite3 ~/.claude/mailbox/mailbox.db ".tables"` 確認 DB 建好

### Watcher 沒喚醒
- 確認 watcher process 活著：`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` Cmdlet match `mailbox-watch.py`
- 確認 heartbeat：`SELECT last_seen_at FROM peers WHERE name='<NAME>'` 應 5 秒內
- 死了重啟看 [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md)

### Discord DM 沒收到
- Bridge container 健康：`docker ps | grep mailbox-bridge`，`Up`
- 確認用的是 `agent-notify` 不是 mailbox INSERT（後者**不會**到 Discord）
- agent-notify response 看到 `<icon> **[wiki]**` **沒下文** → schema 用錯了（應是 `task` + `detail` 不是 `message`）

---

## Hooks（已退役）

2026-05-19 移除 `~/.claude/hooks/ensure-mailbox-watcher.ps1` + `~/.claude/settings.json` 兩個 hook entry。Monitor stream-mode watcher 持續活著，hook 提示變常駐 noise。

歷史 wiring 看 [snapshot/global-settings-json-hooks.md](snapshot/global-settings-json-hooks.md)，要復原自行加回去。
