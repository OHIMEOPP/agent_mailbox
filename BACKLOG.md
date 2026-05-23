# Mailbox 未來功能推薦清單

跨 Claude session 加新功能時看這份，挑高 ROI 的先做。**這份只列上層 application
feature**（messaging semantic / inbox 工具），不重複底層三層 channel（本機 / 跨裝置 /
Discord bridge）的維護工作。

寫於 2026-05-23 overnight ship 完 Round 1+2 ~40+ commits 後，user 詢問「還可以加什麼」
列出來的初版。後續每完成一項就劃掉並 commit log。

---

## 🥇 高 ROI（30 min 內可加）

### 1. Inbox 過濾規則（Gmail filter 風格）

新 table `inbox_rules(id, actor, condition_json, action_json, enabled)`，daemon thread
在 message INSERT 後 evaluate 規則 → 自動 pin / priority / forward / mute / mark_read。

範例：
```
{from_name LIKE 'user-discord%' AND body LIKE '%緊急%'}
  → action: pin=1, priority=9, react with 🚨
```

**Why**：user 已多次反映 user-discord stale 訊息散在 inbox（64 條 dead-letter 那次），
規則自動分類治本。

**How to apply**：新 module `mailbox/rules.py` + REST `/rules add/list/delete` + CLI
`tools/mailbox-rules.py`。Daemon thread 加 INSERT 後 trigger evaluate。

### 2. Mention / cc 系統

`send(to="koatag", body="@wiki 你也看一下", mentions=["wiki"])` — 即使收件人是
koatag，wiki 的 watcher 也會 fire（用 fanout 寄 metadata-only echo message 給
mentions list；或加 mentions 欄到 messages，watcher SELECT 加 OR mentions LIKE）。

**Why**：多 agent 協作常需要「主處理人 + cc 旁觀者」語意。目前 fanout (`to="*"`) 太
粗暴；mention 是輕量提醒。

**How to apply**：schema migration 加 `messages.mentions_json TEXT`（JSON array）；
watcher SQL 加 OR clause；inbox SELECT 加 mentions filter。

### 3. Read receipts

寄件人標 `request_receipt=True`，當收件人 mark_read 時，hub 自動寄一封 small ack 訊息
回原寄件人（body="receipt: msg #X read at <ts>"）。

**Why**：緊急 ping 想確認對方真看到了。

**How to apply**：schema `messages.request_receipt INTEGER DEFAULT 0`；mark_read 邏輯
加 hook：if request_receipt then send ack。Audit 加 action=read_receipt_sent。

---

## 🥈 中 ROI（30-60 min）

### 4. Conversation bundle

`bundle(thread_id)` → 把整 in_reply_to chain （含 root + 所有 replies + reactions
+ attachments metadata）打包成一塊 JSON 或 Markdown，給新接手 agent 直接吞。

**Why**：LLM context 友善。新 agent 加入長對話不用 walk chain 手動 fetch。

**How to apply**：純 read-only SQL + render；新 MCP tool `bundle(message_id, format='md'|'json')`。
REST `GET /bundle/<id>`。

### 5. 訊息草稿

新 table `drafts(id, actor, to, body, in_reply_to, created_at, last_edited_at)`。
MCP `draft(...)` 存草稿，`send_draft(id)` 真 send，`list_drafts()` / `delete_draft(id)`。

**Why**：agent 邊算邊寫長訊息，避免中途誤 send；或 multi-step plan 寫到一半暫停。

**How to apply**：新 module `mailbox/drafts.py` + 4 MCP tools + CLI `tools/mailbox-drafts.py`。

### 6. Smart routing — `best_available`

`send(to="*koatag&load_balance")` 解析成「所有 active koatag*」中找 unread+claim count
最低那個，只寄給他。減少 agent fanout 處理同份工作。

**Why**：load balance multi-instance（譬如同 role 兩台 spoke 跑同 agent）。

**How to apply**：解析 `&load_balance` suffix → 計算每 candidate 的 backlog → pick min。
跟 existing alias fanout 邏輯共用 80%。

### 7. 多 mailbox / channel 區隔

新 table `channels(id, name)` + `messages.channel_id` 欄。Agent 可定義 "work" / "personal" /
"alerts" 等獨立 inbox，不互相干擾。

**Why**：mailbox 累積越多後混在一個 inbox 變雜。

**How to apply**：schema + filter + inbox CLI / MCP 加 `channel=` param。**注意**：複雜度
不小，可能 over-engineer，user 沒實際痛點先別做。

---

## 🥉 大工程但長期高價值（半天～1 天）

### 8. End-to-end 加密 per-message

agent 間 body 加密（X3DH 或更輕量 ECDH），hub 變純 relay 看不到內容。

**Why**：未來開放第三方 agent 加入 hub 時必要；目前自己用反正 hub 自己控所以低優先。

**How to apply**：key exchange protocol design + per-peer keypair store + encrypt/decrypt
在 server.py 包 send/inbox。**Notes**：跟 KOATAG E2EE round 2 重疊度高，可借鏡。

### 9. Federation — 多 hub 互通

家裡 hub + 公司 hub → agent 可跨 hub 寄信。Tailscale 已鋪 LAN/VPN 底層。

**How to apply**：新 `federation` table；hub 互相 register；`to="koatag@hub.work"`
語法。Trust + token exchange 是 hard part。

### 10. Web UI inbox 瀏覽器

純 HTML/JS frontend 看歷史 / search / dump tree / digest，不用每次跑 CLI。

**How to apply**：mailbox-server.py 加 GET `/ui/*` 靜態檔；單頁 vanilla JS 接 REST。
**Notes**：scope 容易爆，先做 minimal read-only。

---

## ⚠ 不推薦做

| 為什麼不做 |
|---|
| **訊息 edit / unsend** — mailbox 本質「事件流」(append-only)，可變性破壞 audit trail |
| **Slack / Email 整合** — Discord 已夠；多 channel 增加 ops 負擔 |
| **AI auto-summarize 訊息** — 丟 LLM 處理 → 成本不可預測；等真有 use case 再加 |
| **Token auto-pipeline**（spoke 自己跟 hub 要 token） — 雞蛋問題 + trust model 沒設計清楚（見 `project_mailbox_token_pipeline_backlog` memory） |
| **#10 Structured logging**（stderr → JSON line） — overnight 已 pivot away，cost / value 比不上 feature |

---

## 加新項目的格式

寫進這檔之前先評估：
1. **解了什麼真實痛點**（user 抱怨過 / 寫 incident report 出現）
2. **跟既有 feature 有沒重複**（譬如 conversation bundle 跟 dump --tree 重疊 80%，多做的價值是什麼）
3. **多大 scope**（&lt; 30 min / 30-60 / 半天+ 三檔分類）
4. **怎麼 smoke 證明它沒退化**

格式：
```markdown
### N. 標題

簡述 + 範例 invocation

**Why**: 痛點來源

**How to apply**: 實作 entry points + 影響面 + 注意點
```

---

## Connections

- [[STRUCTURE.md]] — repo 結構 (~3 levels: entry / mailbox/ / tools/ / tests/)
- [[wiki/output/mailbox-overnight-2026-05-22]] — 已 ship 17+ Round 1 / 14+ Round 2 features
- [[wiki/output/mailbox-overnight-2026-05-22-morning-briefing]] — TL;DR + 部署
- [[wiki/concepts/AI/claude-mailbox/subtopics/feature-catalogue-2026-05-23]] — 完整功能目錄
- `project_mailbox_token_pipeline_backlog` memory — token auto-pipeline 設計雞蛋問題
