---
name: wiki-mailbox-supervisor-inbound-peer
description: "wiki watcher --watch-all 看任意 to_name, 對非 wiki 的 inbound 等待 X 分鐘看 peer 接沒, 沒接 wiki 代理回 user. 監軍角色 (2026-05-19)"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e264a90a-f10a-4b3a-ab10-4c99987d97c2
---

## 規則

Wiki 跑 `mailbox-watch.py wiki --monitor --watch-all`，stdout `MAIL id=N from=A to=B preview=...` 對任意 `to_name` 都會 fire。對 mail to ≠ wiki：

1. **不立刻插手** — 給 peer agent 一點時間自己處理
2. **等 ~3-5 min**（看 task urgency 調），SQL check `read_at` 還是 NULL?
3. **還沒接** → DM user `⚠ <peer> 沒接 #N (X 分鐘)，我代理：<回應>` + 處理該需求
4. **接了** → 不動，stand by

## 哪些 mail 該代理（白名單）

只代理 **user 發起的 inbound**（user-discord routed via @prefix 到 peer）：

| `from_name` 模式 | 代理? |
|---|---|
| `user-discord (ohimeopp)` 路由到 koatag / koatag-frontend / stranger-conv | ✅ 代理 |
| `user-discord (X)` (X != ohimeopp) | ⚠ 看 whitelist；通常 stranger-conv 處理，我不插手 |
| `koatag` / `koatag-frontend` / 其他 agent 之間互寄 | ❌ 不代理（agent ↔ agent 內部協作，wiki 不該介入）|
| `test-*` / `bridge` / 自己 INSERT 的 admin mail | ❌ 不代理（內部 ops 流量）|

## 檢查 peer 收沒收

```python
import sqlite3
db = sqlite3.connect(r'C:\Users\User\.claude\mailbox\mailbox.db')
# Mail 收沒收
row = db.execute("SELECT read_at FROM messages WHERE id=?", (msg_id,)).fetchone()
unread = row[0] is None
# Peer watcher 活著嗎
hb = db.execute("SELECT last_seen_at FROM peers WHERE name=?", (peer_name,)).fetchone()
```

| 訊號 | 意義 |
|---|---|
| `read_at IS NULL` + `peers.last_seen_at` < 5s ago | watcher 活，agent 可能在處理，再等等 |
| `read_at IS NULL` + `peers.last_seen_at` > 30s ago | watcher 死了 / agent 不在 — 該代理 |
| `read_at IS NOT NULL` | 已處理，stand by |

## 代理時的 DM 格式

```
⚠ koatag-frontend 沒接 #N (5 min)
原文: ...
我代理回應: ...
```

User 看到知道發生什麼。Peer agent 後續若 wake 起來，看到 mail 還在 unread + DM history 有 wiki 已回 → 不會二度回應（agent 共識：read_at IS NULL 不代表「沒人處理」，要看 DM 上下文）。

## 不該做的

- **不要 mark_read 別人的 inbox 來代替處理** — 那會讓 peer agent 永遠看不到該訊息. Mail 該維持 unread, peer 後續仍有機會 catch up
- **不要改 from_name / to_name** — 別動 DB schema, 純 read + DM 回 user
- **不要無視長等候訊號** — 超過 30 min 還 unread 且 peer heartbeat 死 → 直接代理 + 提醒 user「peer X 看起來掛了，建議重啟 session」

## Why

`watch-all` 上線後 wiki 看得到全網流量，是天然的 supervisor。User 寄到 peer 時若 peer 不在 (Claude session 沒開 / agent 在睡)，沒人接 = mail queue 卡。Wiki 代理避免 user 等到 timeout 才發現訊息丟了。

## 出處

2026-05-19 user request:「我要無論 DM 給誰你都可以被喚醒，其他 agent 沒收到時，你要代理，你得知到其他 agent 有沒有收到?」

對應實作:
- `mailbox-watch.py --watch-all` flag (commit a208987)
- 本 memory rule
