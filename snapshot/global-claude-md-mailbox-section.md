# Snapshot: `~/.claude/CLAUDE.md` mailbox section

> Copied here so the canonical mailbox repo records how mailbox is wired into the
> global config. Update both sides if the source changes.
>
> Last sync: 2026-05-19

---

## Mailbox 通訊 — Session 開始時自動啟動 watcher

如果這個 session 有 mailbox MCP（工具列表裡看得到 `mcp__mailbox__*`），**第一個 turn** 自動跑：

1. 呼叫 `mcp__mailbox__whoami` 拿到本 instance 的名字（例如 `wiki`、`koatag`）
2. 用 **Monitor tool** 起 stream-mode watcher（`persistent: true`）：
   ```
   command:     py "C:/Users/User/Desktop/VSCcode/claude-mailbox/mailbox-watch.py" <name> --monitor
   description: mailbox watcher for <name>
   persistent:  true
   timeout_ms:  3600000
   ```
3. 跟使用者一句話交代：「mailbox watcher 已啟動（Monitor stream-mode，每封 mail 一條 notification，watcher 不死）」

之後別的 agent（例如 wiki ↔ koatag 互寄）任何訊息進 mailbox，watcher 印一行 `MAIL id=... from=...` 到 stdout、harness 用 Monitor 機制喚醒 agent 自動 `inbox` + `mark_read` + 視情況回信。**Watcher 收完信繼續活著**，下封 mail 直接接，不需要 restart cycle。

**例外**：使用者明確說「不要 watcher」/「這次不用 mailbox」就略過。

**Legacy exit-mode** (`py mailbox-watch.py <name>` 不帶 `--monitor`)：watcher 第一次見 unread 就 exit code 0；走 Bash `run_in_background:true` 也能 wake，但 wake 後 watcher 死掉，需 restart。新 session 一律用 monitor mode。

---

詳細 pattern 與工具：
- Script: `claude-mailbox/mailbox-watch.py`（5s tick，per-instance filter）
- Dump: `claude-mailbox/mailbox-dump.py`（slash command `/mblog` 或 `/觀看紀錄`）
- Bridge (Discord↔mailbox): `claude-mailbox/mailbox-discord-bridge.py`（docker container `mailbox-bridge`）
- Whitelist CLI: `claude-mailbox/mailbox-whitelist.py`
