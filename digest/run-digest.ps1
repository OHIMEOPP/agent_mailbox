# run-digest.ps1 — 每日 AI/LLM digest
# headless Claude 研究並寫 digest-out.md -> post-to-bridge.py 投遞到 Discord DM (:1904 bridge)
# Task Scheduler 觸發：09:20 早場 / 19:00 晚場（不帶參數時依時間自動判斷）。
# 手動指定時段測試： powershell -File run-digest.ps1 -Slot 早場
param([string]$Slot = '')

$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

$claude       = 'C:\Users\User\.local\bin\claude.exe'
$promptFile   = Join-Path $dir 'digest-prompt.md'
$settingsFile = Join-Path $dir 'digest-settings.json'
$outFile      = Join-Path $dir 'digest-out.md'
$logFile      = Join-Path $dir 'digest.log'
$postScript   = Join-Path $dir 'post-to-bridge.py'
$stamp        = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# 時段：未指定就依執行時間自動判斷（中午前=早場 / 其餘=晚場）
if ($Slot -ne '早場' -and $Slot -ne '晚場') {
    $Slot = if ((Get-Date).Hour -lt 12) { '早場' } else { '晚場' }
}

"[$stamp] === digest run start (slot=$Slot) ===" | Out-File $logFile -Append -Encoding utf8

# 清掉上次輸出，避免投遞到舊內容
if (Test-Path $outFile) { Remove-Item $outFile -Force }

# 兩場用不同時間窗 + 不同固定官方來源錯開內容（無去重狀態也不會整碗重複）
if ($Slot -eq '早場') {
    $modeNote = @'
【時段指示】現在是「早場」。
1. 主要內容：聚焦「昨天」發生的事（過去約 24 小時）的 AI/LLM/agent 新聞。
2. 本場必含固定區塊「📌 Anthropic / Claude 官方動態」，放在 digest 最前面：彙整 Anthropic / Claude 最新官方發布、部落格、文件/changelog 更新、官方社群（X @AnthropicAI 等）訊息——即使不算「新聞」也要列。來源指引見 sources.json 的 official_by_slot.早場。
digest 標題時段請寫「早場」。
'@
} else {
    $modeNote = @'
【時段指示】現在是「晚場」。
1. 主要內容：聚焦「今天最新」發生的事（過去約 10–12 小時）的 AI/LLM/agent 新聞，避免重複今天早場可能已報過的昨日舊聞。
2. 本場必含固定區塊「📌 OpenAI 官方動態」，放在 digest 最前面：彙整 OpenAI 最新官方發布、部落格、release notes / platform changelog、官方社群（X @OpenAI / @sama 等）訊息——即使不算「新聞」也要列。來源指引見 sources.json 的 official_by_slot.晚場。
digest 標題時段請寫「晚場」。
'@
}

$prompt = $modeNote + "`r`n`r`n" + (Get-Content $promptFile -Raw -Encoding UTF8)

# headless Claude：研究 + 寫 digest-out.md（不負責投遞）
# 不用 bypassPermissions，只用 --allowedTools 白名單；--settings 用「檔案」而非 inline JSON
# （PS5.1 把含雙引號的 JSON 字串傳原生 exe 時引號會被吃掉）；--bare 會連 OAuth 一起關，不用。
$claudeArgs = @(
    '-p',
    '--model', 'sonnet',
    '--settings', $settingsFile,
    '--allowedTools', 'WebSearch', 'WebFetch', 'Read', 'Write'
)
# PS5.1 會把原生程式 stderr 包成 ErrorRecord，在 'Stop' 下會中斷整個腳本（連投遞都跳過）。
# 放寬成 'Continue'，確保不論 claude 結果如何都會走到下面的投遞判斷（投遞以檔案存在為準）。
$ErrorActionPreference = 'Continue'
$prompt | & $claude @claudeArgs *>> $logFile
$ErrorActionPreference = 'Stop'

# 投遞：有輸出就送 digest，沒有就送失敗通知
if ((Test-Path $outFile) -and ((Get-Item $outFile).Length -gt 0)) {
    py $postScript $outFile *>> $logFile
    "[$stamp] delivered (slot=$Slot)" | Out-File $logFile -Append -Encoding utf8
} else {
    py $postScript --error "Claude 沒有產生 digest-out.md（slot=$Slot，見 digest.log）" *>> $logFile
    "[$stamp] FAILED - no digest-out.md (slot=$Slot)" | Out-File $logFile -Append -Encoding utf8
}
