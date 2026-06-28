# run-stock-digest.ps1 -- daily Taiwan stock-market digest (pre-market)
# headless Claude researches US + TW market-moving news, traces supply-chain
# beneficiary stocks, writes stock-digest-out.md -> post-to-bridge.py -> Discord DM (:1904).
# Task Scheduler trigger: weekdays 08:00. Manual test:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run-stock-digest.ps1
# NOTE: keep this file ASCII-only (no Chinese) so PS5.1 parses it regardless of BOM.

$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

$claude       = 'C:\Users\User\.local\bin\claude.exe'
$promptFile   = Join-Path $dir 'stock-digest-prompt.md'
$settingsFile = Join-Path $dir 'digest-settings.json'
$outFile      = Join-Path $dir 'stock-digest-out.md'
$logFile      = Join-Path $dir 'stock-digest.log'
$postScript   = Join-Path $dir 'post-to-bridge.py'
$stamp        = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

"[$stamp] === stock digest run start ===" | Out-File $logFile -Append -Encoding utf8

# Clear last output so we never deliver stale content.
if (Test-Path $outFile) { Remove-Item $outFile -Force }

$prompt = Get-Content $promptFile -Raw -Encoding UTF8

# headless Claude: research + write stock-digest-out.md (delivery handled below).
# --settings is a FILE path (PS5.1 mangles inline JSON quotes); --bare would kill OAuth.
$claudeArgs = @(
    '-p',
    '--model', 'opus',
    '--settings', $settingsFile,
    '--allowedTools', 'WebSearch', 'WebFetch', 'Read', 'Write'
)
# PS5.1 wraps native stderr as ErrorRecord; under 'Stop' that aborts the whole script
# (delivery skipped). Relax to 'Continue'; delivery keys on the output file existing.
$ErrorActionPreference = 'Continue'
$prompt | & $claude @claudeArgs *>> $logFile
$ErrorActionPreference = 'Stop'

# Deliver: send the digest if produced, else a failure notice. --stock sets the Discord title.
if ((Test-Path $outFile) -and ((Get-Item $outFile).Length -gt 0)) {
    py $postScript $outFile --stock *>> $logFile
    "[$stamp] delivered" | Out-File $logFile -Append -Encoding utf8
} else {
    py $postScript --error "Claude did not produce stock-digest-out.md (see stock-digest.log)" --stock *>> $logFile
    "[$stamp] FAILED - no stock-digest-out.md" | Out-File $logFile -Append -Encoding utf8
}
