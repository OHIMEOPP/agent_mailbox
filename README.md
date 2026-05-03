# Claude Code Mailbox

跨 Claude Code instance 的非同步訊息佇列，透過 MCP 實作。

## 運作原理

每個 Claude Code instance 各自 spawn 自己的 stdio MCP server，**但共讀寫同一個 SQLite 檔**：

```
            ~/.claude-mailbox.db (SQLite, WAL mode)
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

無 daemon，無 server process 要顧。每次 Claude 開 session 時 spawn 子行程，session 結束就退出。

## 工具（5 個）

| Tool | 用途 |
|---|---|
| `send(to, body)` | 寄給某 instance |
| `inbox(unread_only=true, limit=50)` | 收信 |
| `mark_read(ids)` | 標記已讀 |
| `peers()` | 列出曾連線過的 instance |
| `whoami()` | 確認自己是誰、DB 在哪 |

## 安裝

需要 [uv](https://docs.astral.sh/uv/) — Python script 用 PEP 723 內嵌依賴宣告，`uv run` 自動裝 `mcp` 套件。

不需要 pip install，不需要 venv。

## 註冊到 Claude Code

每個專案各自設定，給自己一個獨特名稱。

### 方法 A：專案 `.mcp.json`

複製 `examples/mcp.json.life_wiki`（或 `mcp.json.koatag`）到專案根目錄並改名為 `.mcp.json`，調整 `CLAUDE_MAILBOX_NAME`：

```json
{
  "mcpServers": {
    "mailbox": {
      "command": "uv",
      "args": [
        "run",
        "C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py"
      ],
      "env": {
        "CLAUDE_MAILBOX_NAME": "wiki"
      }
    }
  }
}
```

### 方法 B：`claude mcp add` CLI

```bash
claude mcp add mailbox \
  --scope project \
  -e CLAUDE_MAILBOX_NAME=wiki \
  -- uv run "C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py"
```

### 方法 C：全域註冊（所有專案共用，但 NAME 會固定）

`~/.claude.json` 或 `claude mcp add --scope user`。**不建議**，因為 NAME 固定就無法區分專案。

## 使用方式

啟動 Claude Code 後，會看到工具 `mcp__mailbox__send`、`mcp__mailbox__inbox` 等。

### 發信
> 「請呼叫 mailbox.send 給 koatag，內容是『請看 docker-compose.yml 並摘要服務清單』」

### 收信
> 「檢查 mailbox 是否有未讀訊息」

→ Claude 會 call `inbox()`，看到訊息後處理，可能再 call `send()` 回覆。

## 自動檢查信箱（hook）

因為 Claude Code 是 turn-based，**不會主動 poll**。在每個專案 `.claude/settings.json` 加 `SessionStart` hook 自動提醒：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '[mailbox] 開 session 前先呼叫 mcp__mailbox__inbox 看有無未讀'"
          }
        ]
      }
    ]
  }
}
```

或更積極的，把這條規則寫進 CLAUDE.md：

```markdown
## Mailbox 規則
- Session 開始時：先 call `mcp__mailbox__inbox()` 查未讀
- 任務需要委派給其他專案時：call `mcp__mailbox__send(to=..., body=...)`
- 看完訊息後 call `mark_read(ids=[...])`
```

## 限制

- ❌ **不是即時 push** — 收信端要主動觸發 `inbox()` 才看得到
- ❌ 不能中斷對方正在執行的任務
- ✅ 跨專案、跨 session 可以非同步傳資料
- ✅ 可跨機器（DB 放共享磁碟即可）

## DB 位置

預設 `~/.claude-mailbox.db`（Windows 上 = `C:\Users\User\.claude-mailbox.db`）。

要改：在 `.mcp.json` 的 `env` 加 `CLAUDE_MAILBOX_DB`：

```json
"env": {
  "CLAUDE_MAILBOX_NAME": "wiki",
  "CLAUDE_MAILBOX_DB": "D:/shared/team-mailbox.db"
}
```

## DB schema

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

要直接看訊息：

```bash
sqlite3 ~/.claude-mailbox.db "SELECT * FROM messages ORDER BY id DESC LIMIT 10"
```

## 除錯

### server 起不來
- 看 Claude Code 啟動 log（`claude --debug` 或 IDE extension 的 output panel）
- 手動測：`uv run C:/Users/User/Desktop/VSCcode/claude-mailbox/server.py`，會等 stdio 輸入；按 Ctrl+C 退出代表 server 啟動成功
- 環境變數 `CLAUDE_MAILBOX_NAME` 必設，沒設會 RuntimeError

### 訊息送不到
- `whoami()` 看自己 NAME 對不對
- `peers()` 看對方有沒有連過（連過才會出現在表內）
- `sqlite3 ~/.claude-mailbox.db ".tables"` 確認 DB 建好了

### 兩台機器共用 DB
- DB 放網路共享磁碟（SMB/NFS）
- ⚠️ SQLite WAL 模式跨網路檔案系統行為有限制；偶爾鎖死。建議單機多 instance。
