@echo off
REM Launcher for the ComfyUI/output mailbox folder sync daemon.
REM Registered as a Task Scheduler entry (trigger: at logon) so the daemon
REM starts in the background after the user logs in and survives independently
REM of any terminal session.
REM
REM Edit args here, not in Task Scheduler — keeps the task definition stable.

start "" /b "C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe" ^
  "C:\Users\User\Desktop\VSCcode\claude-mailbox\tools\mailbox-folder-sync.py" ^
  --folder "C:\Users\User\Documents\ComfyUI\output" ^
  --label "ComfyUI/output" ^
  --interval 3 ^
  --stable-secs 2 ^
  --log-file "C:\Users\User\.claude\mailbox\folder-sync.log"
