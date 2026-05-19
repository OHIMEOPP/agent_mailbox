# Snapshot: `~/.claude/settings.json` hook registration

> Was used to auto-emit a "watcher not running" reminder on SessionStart /
> UserPromptSubmit. Removed 2026-05-19 because Monitor stream-mode watcher
> rarely dies, making the reminder near-permanent noise.
>
> Kept here as record of the historical wiring in case it needs to come back.

The two hook entries that were removed (both pointed at the same script):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:/Users/User/.claude/hooks/ensure-mailbox-watcher.ps1"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:/Users/User/.claude/hooks/ensure-mailbox-watcher.ps1"
          }
        ]
      }
    ]
  }
}
```

Original script also kept under `snapshot/ensure-mailbox-watcher.ps1` (no longer
wired in by `settings.json`).
