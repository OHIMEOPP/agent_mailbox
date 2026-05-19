---
name: dm
description: "User 下指令後預估 >30s 的任務, 過程要 DM 進度報告 (開始 / 階段轉換 / 卡住 / 完成), 不能一頭悶到尾. 沉默 = user 不知道有沒在跑"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: e264a90a-f10a-4b3a-ab10-4c99987d97c2
---

## 規則

User 下指令後預估執行 >30s 的任務，**過程中要 DM 進度回報**，不能一頭悶到結尾再吐結果。

### 觸發報告的時機

- **任務開始** (>30s 預估) → 一句 `starting X, 預計 ~Y 分`
- **階段轉換** → `Phase 1 done, Phase 2 中`
- **卡住 / 預估時間超 2x** → `blocked / 還在跑` + 原因
- **完成** → 最終結果 + 數據

### 不該報的（避免 spam）

- 每個 sub-step（micro 細節）
- 單一 LLM call 結束（除非 benchmark 本身就是任務）
- 自解的小 error / retry
- < 30s 的快任務（沒 race condition 風險）

## Why

**User 看不到 CLI**，只看 Discord DM。一頭悶到尾 = user 不知道 agent 是 hung / 跑著 / 死了。

具體痛點 trace（2026-05-19）：
- user 「拉個 9b 測測」
- wiki 默默跑 pull (2 min) + benchmark (30s) — 全程沒回
- user 等 ~3 min 後問「邊的時候可以順便報告進度？」抓到
- 之前 mailbox bridge 改的時候也類似，user 多次問「好了沒」「跑過了？」

紀律失誤代價：user **以為我 hung 或漏掉指令**, 反覆問同件事浪費 round-trip。

## How to apply

DM 走 `POST http://localhost:1904/agent-notify`（bridge primary，2026-05-19 後），schema `{agent, task, status, detail}`. Status 用：
- `info` 📋 — 起始 / 階段轉換
- `done` ✅ — 完成
- `fail` ❌ — blocker / error
- `warn` ⚠️ — 跑著但超 ETA

例子（user: 「動 Phase 1 RAG」）：
1. `📋 [wiki] Phase 1 開始 - 5 步預計 ~30 min`
2. `📋 [wiki] embedding model 拉完 (2/5)`
3. `📋 [wiki] schema 建好 + glossary 灌完 (4/5)`
4. `✅ [wiki] Phase 1 完成 — top-5 命中率 X, 譯文對照如附`

## 例外

- User 明說「不用報進度」/「沉默到完成」→ 略過
- Background task fire-and-forget（譬如 watcher 開機）→ 啟動時提一次, 後續事件驅動
