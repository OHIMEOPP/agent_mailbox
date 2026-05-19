# Hook for Claude Code SessionStart / UserPromptSubmit.
# If no mailbox-watch.py process is running for this project's mailbox name,
# emit a system-reminder telling the assistant to start one as a tracked
# background Bash. Stays silent when watcher is OK.

$ErrorActionPreference = 'SilentlyContinue'

# --- 1. Determine mailbox name ---------------------------------------------
$projectDir = $env:CLAUDE_PROJECT_DIR
if (-not $projectDir) { $projectDir = (Get-Location).Path }

$name = $null
$mcpJson = Join-Path $projectDir '.mcp.json'
if (Test-Path $mcpJson) {
    try {
        $cfg = Get-Content $mcpJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $name = $cfg.mcpServers.mailbox.env.CLAUDE_MAILBOX_NAME
    } catch {}
}

# Fallback: project dir leaf -> mailbox name (for projects without .mcp.json,
# e.g. life_wiki which uses a globally-configured mailbox)
if (-not $name) {
    $leaf = Split-Path $projectDir -Leaf
    switch -Regex ($leaf) {
        '^life_wiki$'       { $name = 'wiki' }
        '^KOATAG$'          { $name = 'koatag' }
        '^koatag-frontend$' { $name = 'koatag-frontend' }
    }
}

if (-not $name) { exit 0 }  # not a mailbox-enabled project; silent

# --- 2. Is a watcher already running for this name? ------------------------
$pat = "mailbox-watch\.py.*\b$([regex]::Escape($name))\b"
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match $pat }

if ($running) { exit 0 }  # OK; silent

# --- 3. Emit directive to assistant ----------------------------------------
@"
<system-reminder>
Mailbox watcher for '$name' is NOT running in this session. Your next tool call MUST be the Monitor tool with persistent: true to start it:

  command:     py "C:/Users/User/.claude/tools/mailbox-watch.py" $name --monitor
  description: mailbox watcher for $name
  persistent:  true

Stream-mode watcher prints one stdout line per new mail and keeps polling -- the watcher survives mail events instead of dying. Each MAIL line is a Monitor notification, waking you to handle the inbox. Without this tracked binding, incoming Discord DMs / agent messages go unnoticed until you manually poll the mailbox.
</system-reminder>
"@

exit 0
