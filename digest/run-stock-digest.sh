#!/usr/bin/env bash
# 每日股市 digest — koatag VM 版（Linux / cron）
# headless claude 研究美股+台股新聞 → 台股供應鏈受益股 -> stock-digest-out.md
#   -> post-to-discord.py 直送 Discord DM（REST，與 Node-RED bot gateway 互不衝突）
#
# cron（VM 是 UTC；台北盤前 08:00 = 00:00 UTC，平日 Mon-Fri）：
#   0 0 * * 1-5  /home/user/digest/run-stock-digest.sh
# 手動測試： ./run-stock-digest.sh

set -uo pipefail

# cron 的 PATH 很精簡，claude / node / python3 可能不在 → 明確補上常見位置
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

LOG="$DIR/stock-digest.log"
OUT="$DIR/stock-digest-out.md"
PROMPT_FILE="$DIR/stock-digest-prompt.md"
SETTINGS="$DIR/digest-settings.json"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"

echo "[$STAMP] === stock digest run start ===" >> "$LOG"
rm -f "$OUT"

# headless claude（OAuth 訂閱額度）。--settings 用檔案（disableAllHooks）；只放行需要的工具。
printf '%s' "$(cat "$PROMPT_FILE")" | claude -p \
  --model opus \
  --settings "$SETTINGS" \
  --allowedTools WebSearch WebFetch Read Write \
  >> "$LOG" 2>&1

if [ -s "$OUT" ]; then
  # 後處理：回填台股昨收價到第③部（TWSE/TPEx 官方 API，零捏造；抓不到原檔不動）
  python3 "$DIR/inject-stockprice.py" "$OUT" >> "$LOG" 2>&1
  python3 "$DIR/post-to-discord.py" "$OUT" --stock >> "$LOG" 2>&1
  echo "[$STAMP] delivered" >> "$LOG"
else
  python3 "$DIR/post-to-discord.py" --error "claude 沒有產生 stock-digest-out.md（見 stock-digest.log）" --stock >> "$LOG" 2>&1
  echo "[$STAMP] FAILED - no stock-digest-out.md" >> "$LOG"
fi
