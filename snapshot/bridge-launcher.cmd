@echo off
REM Launcher for mailbox-discord-bridge.
REM Redirects stdout/stderr to log so Task Scheduler can run it window-less.
REM Log location: C:\Users\User\.claude\tools\bridge.log (overwritten each start;
REM the bridge runs forever per invocation so this is intentional).
py "C:\Users\User\.claude\tools\mailbox-discord-bridge.py" > "C:\Users\User\.claude\tools\bridge.log" 2>&1
