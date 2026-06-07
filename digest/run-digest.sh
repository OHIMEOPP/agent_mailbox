#!/usr/bin/env bash
# 每日 AI/LLM digest — koatag VM 版（Linux / cron）
# headless claude 研究 -> digest-out.md -> post-to-discord.py 直送 Discord DM
#
# cron（VM 是 UTC；台北 09:20 早場 = 01:20 UTC、19:00 晚場 = 11:00 UTC）：
#   20 1  * * *  /home/user/claude-mailbox/digest/run-digest.sh
#    0 11 * * *  /home/user/claude-mailbox/digest/run-digest.sh
# 手動指定時段： run-digest.sh 早場   /   run-digest.sh 晚場

set -uo pipefail

# cron 的 PATH 很精簡，claude / node / python3 可能不在 → 明確補上常見位置
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

LOG="$DIR/digest.log"
OUT="$DIR/digest-out.md"
PROMPT_FILE="$DIR/digest-prompt.md"
SETTINGS="$DIR/digest-settings.json"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"

# 時段：未帶參數時依「台北時間」判斷（VM 是 UTC，用 TZ 換算）
SLOT="${1:-}"
if [ "$SLOT" != "早場" ] && [ "$SLOT" != "晚場" ]; then
  TPE_HOUR="$(TZ=Asia/Taipei date '+%H')"
  if [ "$TPE_HOUR" -lt 12 ]; then SLOT="早場"; else SLOT="晚場"; fi
fi

echo "[$STAMP] === digest run start (slot=$SLOT) ===" >> "$LOG"
rm -f "$OUT"

if [ "$SLOT" = "早場" ]; then
  MODE='【時段指示】現在是「早場」。
1. 主要內容：聚焦「昨天」發生的事（過去約 24 小時）的 AI/LLM/agent 新聞。
2. 本場必含固定區塊「📌 Anthropic / Claude 官方動態」，放在 digest 最前面：彙整 Anthropic / Claude 最新官方發布、部落格、文件/changelog、官方社群（X @AnthropicAI 等）——即使不算「新聞」也要列。來源指引見 sources.json 的 official_by_slot.早場。
digest 標題時段請寫「早場」。'
else
  MODE='【時段指示】現在是「晚場」。
1. 主要內容：聚焦「今天最新」發生的事（過去約 10–12 小時）的 AI/LLM/agent 新聞，避免重複今天早場可能已報過的昨日舊聞。
2. 本場必含固定區塊「📌 OpenAI 官方動態」，放在 digest 最前面：彙整 OpenAI 最新官方發布、部落格、release notes / platform changelog、官方社群（X @OpenAI / @sama 等）——即使不算「新聞」也要列。來源指引見 sources.json 的 official_by_slot.晚場。
digest 標題時段請寫「晚場」。'
fi

PROMPT="$MODE

$(cat "$PROMPT_FILE")"

# headless claude（OAuth 訂閱額度）。--settings 用檔案（disableAllHooks）；只放行需要的工具，
# print 模式下白名單外的工具自動拒絕，無人值守、不關整體審批、不用 bypassPermissions。
printf '%s' "$PROMPT" | claude -p \
  --model sonnet \
  --settings "$SETTINGS" \
  --allowedTools WebSearch WebFetch Read Write \
  >> "$LOG" 2>&1

if [ -s "$OUT" ]; then
  python3 "$DIR/post-to-discord.py" "$OUT" >> "$LOG" 2>&1
  echo "[$STAMP] delivered (slot=$SLOT)" >> "$LOG"
else
  python3 "$DIR/post-to-discord.py" --error "claude 沒有產生 digest-out.md（slot=$SLOT，見 digest.log）" >> "$LOG" 2>&1
  echo "[$STAMP] FAILED - no digest-out.md (slot=$SLOT)" >> "$LOG"
fi
