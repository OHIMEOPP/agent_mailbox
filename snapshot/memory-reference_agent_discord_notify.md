---
name: reference-agent-discord-notify
description: Agent 透過 bridge :1904 (primary) 或 node-red :1901 (legacy) endpoint 即時推送 Discord DM 給 user 的 pattern + UTF-8 編碼陷阱 + 檔案附件 multipart
metadata: 
  node_type: memory
  type: reference
  originSessionId: 95503142-1415-4a46-bb0a-250d2760c1d9
---

User 設了 Discord outbound endpoint 接 agent → user Discord DM (channel ID `1284054699594485814`，直接對話非群組)。重要 round close / error 可即時推送 user 不必等 mailbox round-trip。

## Endpoints

### 主：bridge `:1904/agent-notify` (Python, 2026-05-19+)
本 repo `claude-mailbox/bridge/` 提供，走 Discord REST API。新 deployment default 用這條。

### Legacy：node-red `:1901/agent-notify`
過去主要 endpoint，2026-05-19 起 bridge 接管 outbound。仍可並存但 gateway 不能同時連 Discord。

## 文字 DM endpoint

```
POST http://localhost:1904/agent-notify
Content-Type: application/json; charset=utf-8

Body:
{
  "agent": "wiki | koatag | koatag-frontend",
  "task": "<title>",
  "status": "done | fail | warn | info",  // 預設 info
  "detail": "<text，可空>",
  "channel": "<discord channel id，可省略走預設 trusted DM>"
}
```

## 附件 endpoint (2026-05-19+)

```
POST http://localhost:1904/agent-notify-file
Content-Type: multipart/form-data

Parts:
  payload_json   = JSON body 同上 (agent/task/status/detail/channel?)
  files[0]       = file binary (filename header 保留)
  files[1]       = ... (多檔可選)
```

CLI 包裝 (host 端讀檔，container 不需 mount 路徑):
```
py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-send-file.py" \
   --task "..." --detail "..." \
   --files A.png B.pdf \
   [--channel <id>]
```

Discord 限制 25 MB / 檔 (Nitro 50/500), 一封多檔總 size 累加.

## Status → icon

- `done` → ✅
- `fail` → ❌
- `warn` → ⚠️
- `info` (預設) → 📋

## 訊息格式

```
{icon} **[{agent}]** {task}
{detail}
```

## 推薦 client (中文 UTF-8 safe)

### Python（最 portable）

```python
import urllib.request, json
body = {'agent': 'wiki', 'task': '中文標題', 'status': 'done', 'detail': '中文 detail'}
req = urllib.request.Request(
    'http://localhost:1904/agent-notify',
    data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
    method='POST',
    headers={'Content-Type': 'application/json; charset=utf-8'},
)
urllib.request.urlopen(req, timeout=8)
```

### PowerShell

```powershell
$body = @{ agent='wiki'; task='中文'; status='done'; detail='中文' } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:1904/agent-notify `
    -Method POST -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

### Bash curl（中文用 file 不用 inline）

```bash
echo '{"agent":"wiki","task":"中文","status":"done"}' > /tmp/n.json
curl -X POST http://localhost:1904/agent-notify -H "Content-Type: application/json; charset=utf-8" -d @/tmp/n.json
```

## ❌ Anti-pattern：curl inline 中文

Git Bash 在 Windows 上 `curl -d '{"task":"中文"}'` 會把中文 UTF-8 轉成 cp950/cp1252 → Discord 收到 mojibake。**避免**。

## ⚠️ 不該打 notify 的時機

- 每 mailbox round 都打 → spam user
- 三層 check 中間步驟（除非 critical fail）→ noisy
- 純技術細節 ack → low signal
- 重複「相同 task 不同階段」訊息 → user 看到三次同個 task fatigue

## ✅ 該打 notify 的時機

- **大 round close**（D.x 完整 close 含 deploy）
- **prod-touching action fail blocker** — user 該知道
- **classifier 攔停需 user direct GO** — user 不知不會 unblock
- **發現 critical security gap** — D.18 image upload 那種發現
- **autonomous overnight 開始 / 結束** — 大時間段 phase transition

## 對應 [[reference-autonomous-overnight-pattern]]

autonomous mode 啟動 + 結束時打 notify 給 user 知道 phase transition 是好 pattern。
