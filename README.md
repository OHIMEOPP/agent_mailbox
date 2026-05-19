# Claude Code Mailbox

跨 Claude Code instance 的非同步訊息佇列 + Discord 橋接 + 對使用者推送通知，三件套統一的家。

**為什麼存在**：Claude Code 是 turn-based agent — 一個 session 不會主動知道另一個 session（或外部 Discord 使用者）想跟它說什麼。Mailbox 提供共享 SQLite + per-instance watcher，讓不同 agent / user 互通的訊息流被 event-driven 喚醒，而不是每個 session 各自輪詢。

---

## 🚀 Cold-start checklist（agent 視角：被 user 指示用 mailbox 時讀這段）

### 情境 A：「**開 watcher**」/「啟動 watcher」/「session start」

跑這 4 步：

1. **驗證 repo 在預期位置**
   ```bash
   ls C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py
   ```
   不存在 → 先 `git clone https://github.com/OHIMEOPP/agent_mailbox.git C:/Users/User/Desktop/VSCcode/claude-mailbox`

2. **驗證 DB 目錄**
   ```bash
   ls C:/Users/User/.claude/mailbox/mailbox.db
   ```
   不存在 → server.py 第一次跑時會自動 mkdir + create；通常已存在

3. **問自己是誰**：呼叫 `mcp__mailbox__whoami()` 拿 `name` 欄位（如 `wiki` / `koatag` / `koatag-frontend` / `stranger-conv`）

4. **啟動 watcher**：用 Monitor tool（preferred）— 完整模板看 [HOW-TO-START-WATCHER.md](HOW-TO-START-WATCHER.md)。一行版：
   ```yaml
   tool: Monitor
   command:     py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py" <NAME> --monitor
   persistent:  true
   timeout_ms:  3600000
   ```

5. 回 user 一句「mailbox watcher 已啟動（stream-mode）」

### 情境 B：「**DM 我**」/「回我」/「告訴 user X」/「寄 Discord」

User 在 Discord 跟你溝通 — 你要 **推送回 Discord DM**。Mailbox SQLite INSERT 對 Discord **沒效**（bridge 單向），必須走 node-red endpoint：

```python
import urllib.request, json
body = {
    "agent": "wiki",          # 你的 instance 名
    "task": "<短標題>",        # Discord 顯示第一行
    "status": "info",         # info(📋) / done(✅) / fail(❌) / warn(⚠️)
    "detail": "<本文>",        # Discord 顯示第二行起
}
req = urllib.request.Request(
    "http://localhost:1901/agent-notify",
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

外加 Docker `mailbox-bridge` container（port 1904，one-way Discord → mailbox）+ node-red `discordBot` container（port 1901，mailbox → Discord via `agent-notify` endpoint）形成 Discord ↔ agent ↔ peer-agent 三向通道。

---

## MCP 工具（5 個）

| Tool | 用途 |
|---|---|
| `send(to, body)` | 寄給某 instance |
| `inbox(unread_only=true, limit=50)` | 收信 |
| `mark_read(ids)` | 標記已讀 |
| `peers()` | 列出曾連線過的 instance |
| `whoami()` | 確認自己是誰、DB 在哪 |

## Bridge / 周邊工具

- `mailbox-discord-bridge.py` — Docker container `mailbox-bridge`（port 1904）。**單向**：Discord DM → mailbox INSERT。反向走 node-red `agent-notify`（見上面情境 B）
- `mailbox-dump.py` — 撈 mailbox 歷史；wiki session 有 slash command `/mblog` 跟 `/觀看紀錄` 包好
- `mailbox-whitelist.py` — Discord 來源信任名單 CLI（trusted / approved / pending），see [discord-stranger-chat](https://github.com/OHIMEOPP/discord-stranger-chat) 設計

---

## 安裝（新裝置首次設定）

需要 [uv](https://docs.astral.sh/uv/) — Python script 用 PEP 723 內嵌依賴宣告，`uv run` 自動裝 `mcp` 套件。

```bash
git clone https://github.com/OHIMEOPP/agent_mailbox.git C:/Users/User/Desktop/VSCcode/claude-mailbox
mkdir -p C:/Users/User/.claude/mailbox    # DB 目錄
```

不需 pip install，不需 venv。

### 註冊 MCP 到 Claude Code

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

### Bridge container（Discord 整合需要）

`discordBot` 跟 `mailbox-bridge` 兩個 container 起來，bridge 會 mount 此 repo 的 `mailbox-discord-bridge.py`。看 `discordBot/docker-compose.yml`。

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
