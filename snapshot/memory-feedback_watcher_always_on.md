---
name: Mailbox watcher 永遠開著，每 turn 必確認 / restart
description: 用 Monitor stream-mode watcher (--monitor + persistent=true)，收信不死、不需 restart cycle。每 turn 確認 watcher alive (hook reminder 是訊號)，死了才 restart。2026-05-19 從 Bash exit-mode 改 Monitor stream-mode 解決 wake-die-restart gap
type: feedback
originSessionId: f21eb892-937b-4c6f-8533-631d612899ff
---
## 規則 (2026-05-19+)

**Watcher 用 Monitor + stream-mode**，收信不死。每 turn 結束前確認 watcher alive，死了才 restart（不無腦疊加）。

判斷 watcher 死了的訊號：
- UserPromptSubmit hook reminder「Mailbox watcher for '...' is NOT running」
- Monitor task 列表中沒有 watcher entry（TaskList 查）
- 不確定 → 默認 dead，restart

Restart：用 **Monitor tool**（`persistent: true`）跑：
```
py "C:/Users/User/.claude/tools/mailbox-watch.py" wiki --monitor
```

例外：使用者明確說「不用 watcher / 這次不用 mailbox」。

## Why

**Watcher 沒開 = 不接 mailbox = 監軍角色失能**。peer agent (koatag/koatag-frontend) 寄 task review request 過來不會即時被 wiki 看到，要等下個 user prompt 才 inbox poll → 拖三方 round trip 進度。

歷史 trace（KOATAG Drive session 2026-05-10）：
- 使用者早期明說「**每個都要先 watcher**」(msg in session)
- 中途連兩 turn 沒 restart watcher：
  - turn N（回 koatag B11 ack + 存 memory）→ 漏
  - turn N+1（回使用者「現在要幹嘛」）→ 再漏
  - turn N+2 使用者直接問「watcher 有開?」抓到
- 使用者 turn N+3 直接糾正：「我沒說要關，你關了或沒開怎麼接 mailbox」

紀律失誤代價：**那段空窗期 peer 訊息不會自動喚醒 wiki**。當下沒漏訊息純粹是運氣（兩端 standby）。

跟既有 feedback memory `feedback_mailbox_polling.md`（mailbox pull-based 要主動 poll inbox）共同形成 mailbox 紀律 — 但這條更嚴格：watcher 不只「主動 poll」是「**永遠在背景跑**」。

## How to apply

**Trigger**：每個 turn 寫 final response 前最後一個 tool call 段落。

**Check list**（按順序）：
1. **UserPromptSubmit hook 已要求 restart？**（reminder「NOT running」）→ 已在 turn 開頭 restart 過，這 turn 不再開
2. 沒看到 hook reminder → watcher 仍在跑，**跳過 restart**（hook 偵測到 process 存在會靜默）
3. 不確定 → 開（多一個 process 浪費但無害）

**指令**：
```
Monitor tool, persistent=true:
  py "C:/Users/User/.claude/tools/mailbox-watch.py" wiki --monitor
```

## 歷史演化

- **2026-05-18 修補 1**: watcher TTL `--max=17280` (24hr) → `0` infinite，解過夜 user DM queued 問題（user 過夜離開 → watcher 自殺 → 早上 DM 進 mailbox 但 watcher 已死）
- **2026-05-19 修補 2 (現役)**: 從 Bash exit-mode 改 Monitor stream-mode，解 wake-die-restart gap (watcher 收信 exit → harness wake → agent 處理 → restart 之間，新 mail 進來會 queued)。Monitor + stdout-per-mail 後 watcher 不再死，gap 消除

## 例外與邊界

- **使用者明說暫停監軍** → watcher 可關（但要明確記下，下次 resume signal 立刻開）
- **wiki session 進入 standby 等使用者 explicit signal** → watcher 仍開（避免 peer 訊息漏）
- **per-feature alignment 流程下** → watcher 必開（peer 隨時可能寄 review request）

## 出處

- 使用者 KOATAG Drive session msg「每個都要先 watcher」(early instruction)
- 連續兩 turn 漏（2026-05-10 turn 後段）後 user 直接糾正
- 跟 `feedback_mailbox_polling.md` 同源但更嚴格
