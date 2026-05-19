---
name: agent-notify-schema-task-detail-message
description: "agent-notify POST 用 `agent` + `task` + `status` + `detail`，不是 `message`。送錯欄位 Discord 只看到 icon + agent 名 (body drop)，user 不知道內容"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e264a90a-f10a-4b3a-ab10-4c99987d97c2
---

## 規則

`POST http://localhost:1901/agent-notify` JSON 必填四欄：

```json
{
  "agent": "wiki | koatag | koatag-frontend",
  "task": "<短標題, 顯示在第一行>",
  "status": "done | fail | warn | info",
  "detail": "<body, 顯示在第二行>"
}
```

**Why**: 2026-05-19 連續錯送 `{"message": "..."}` 4 條 (#745, #747, #749, #754) — node-red flow drop `message` 欄位，user Discord 只看到 `📋 **[wiki]**` 空殼。User #750 直接抓「你傳個 '✅ [wiki]'，我怎麼知道好了沒」當下 wiki 沒 verify response body，繼續錯送整天。

**How to apply**:
- 每次 notify 前 mental check 欄位名 = `task` + `detail`，不是 `message`
- 看 response — 完整應是 `<icon> **[wiki]** <task>\n<detail>`，只看到 `<icon> **[wiki]**` = body drop 了
- memory [[reference_agent_discord_notify]] schema 在那檔，遇 doubt 先 read

## 旁支發現

**Mailbox bridge 是單向**（Discord → mailbox 有，mailbox → Discord 沒）。所以 `mcp__mailbox__send` / SQL INSERT `wiki → user-discord` 只進 SQLite，**不會到 Discord**。User Discord 端的所有訊息都靠 agent-notify。

要寄訊息給 user 一定走 agent-notify，不是 mailbox INSERT。Mailbox INSERT 純做 audit log。
