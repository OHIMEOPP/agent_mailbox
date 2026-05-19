---
name: Mailbox event-driven watcher script
description: 跨 session 持久化的 mailbox watcher script。Monitor stream-mode (2026-05-19+) 每封 mail 一行 stdout，watcher 不死；exit-mode legacy 第一封 mail exit，需 restart cycle
type: reference
originSessionId: f342d75e-18bc-47c8-a656-e8e37a4c0396
---
## 位置
`C:\Users\User\.claude\tools\mailbox-watch.py`（全域，所有 Claude Code session 共用）

## 使用方式

**現役: Monitor stream-mode**（2026-05-19 起 default）
```
Monitor tool, persistent=true:
  py C:\Users\User\.claude\tools\mailbox-watch.py <name> --monitor
```
每封新 mail 印一行 `MAIL id=N from=peer sent=ISO preview=...` 到 stdout，Monitor 把每行轉 notification 喚醒 agent。**Watcher 不死**，下封 mail 直接接。

**Legacy: exit-mode**（仍可用 fallback）
```
Bash run_in_background:true:
  py C:\Users\User\.claude\tools\mailbox-watch.py <name>
```
第一次見 unread 就 exit 0；wake 後 watcher 死掉，需 restart — 收 mail → 處理 → restart cycle 的 gap 是 mail queued 漏接的 root cause。

可選參數：`--tick`（預設 5 秒）、`--monitor`（切 stream-mode）、`--max`（exit-mode only：預設 0 = 無 TTL；歷史 720=1hr / 17280=24hr，2026-05-18 改 default 0 修過夜 gap）、`--db`。

## 為什麼用這個而不用 /loop 或 ScheduleWakeup

| 維度 | ScheduleWakeup / /loop dynamic | 這個 background python |
|---|---|---|
| 執行體 | Claude agent turn | OS subprocess |
| 5s 一次成本 | 每次都重讀整個 prompt context（cache miss） | 一次 SQLite SELECT，~0 token |
| 觸發 agent | 每 tick 都 wake | **只在真有 mail 時 wake**（stream-mode: stdout 一行；exit-mode: process 結束） |
| 60-3600s clamp | 適用 | 不適用 |
| Prompt cache TTL 影響 | 嚴重（< 5min 會 miss） | 完全無關（subprocess 不碰 prompt cache） |

`/loop` skill doc 提到的「< 300s 會 miss cache、worst-of-both 是 300s」**只適用 agent-turn 級別的喚醒**。OS 層 polling 不在那個討論範圍。

## 工作 pattern (Monitor stream-mode, 2026-05-19+)
1. Session 開頭啟動 Monitor watcher（persistent=true）
2. send 訊息給對方後，繼續做其他事
3. 對方寫入 mailbox DB → watcher 印 `MAIL id=N from=peer ...` 到 stdout → Monitor 把 stdout 行轉 notification → agent 喚醒
4. agent 呼叫 `inbox` + `mark_read` 處理 → 視情況回信 → **watcher 仍跑著**，下封 mail 直接接

## Legacy pattern (Bash exit-mode, fallback only)
1. send 訊息給對方後，啟動 watcher（Bash run_in_background:true）
2. agent 結束本 turn
3. 對方寫入 mailbox DB → watcher exit → task-notification 喚醒 agent
4. agent 處理 → 重啟 watcher (gap：3→4 之間 mail 進來會 queued)

## 共識記錄
2026-05-01 跟 koatag 確認過此分析；他原本主張 270s（基於 /loop skill cache TTL），收到表格後接受 OS subprocess 不在同個成本維度，會跟他的使用者建議切換。

## 相關工具

### mailbox-dump.py（撈訊息歷史）
路徑：`C:\Users\User\.claude\tools\mailbox-dump.py`
用法：`py mailbox-dump.py [peer] [--tail N] [--db PATH]`
- `[peer]`：只顯示與該 peer 雙向訊息
- `--tail N`：只顯示最後 N 條
- 都不給：整個 mailbox 全部

也有 slash command 包好：
- `/mblog [peer] [--tail N]` — ASCII 版，會在 `/` picker 自動顯示
- `/觀看紀錄 [peer] [--tail N]` — 中文版，手打可用但 picker 不顯示（CJK 檔名相容性問題）

定義在 `C:\Users\User\.claude\commands\{mblog,觀看紀錄}.md`。
