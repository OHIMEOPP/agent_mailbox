# Snapshot: `~/.claude/CLAUDE.md` mailbox section

> Copied here so the canonical mailbox repo records how mailbox is wired into the
> global config. Update both sides if the source changes.
>
> Last sync: 2026-06-10（plugin 化後）

---

## Mailbox 通訊

Mailbox 已 **plugin 化**（`agent-mailbox` plugin，git `OHIMEOPP/agent_mailbox`）。看到 mailbox 工具（plugin 模式下名為 `mcp__plugin_agent-mailbox_mailbox__*`）→ **這個 session 是 mailbox-enabled**。

**起 watcher 不用再手動跑 checklist**：plugin 的 SessionStart hook 會自動偵測本專案 mailbox 身分（讀 `.mailbox-name`）並注入一段提示，要你用 **Monitor tool** 起 watcher（hub 本機 mode 或 spoke `--remote` mode 自動判斷）。照那段注入的指令起即可，起完回報一句。例外：使用者明確說「不要 watcher / 這次不用 mailbox」就略過。

收信／回信／mark_read／寄 peer agent／DM user／寄 Discord 等操作的完整流程 + schema 陷阱仍見：

📘 **`C:/Users/User/Desktop/VSCcode/claude-mailbox/README.md`** 的 HOW-TO docs（`HOW-TO-USE-MAILBOX.md` / `HOW-TO-START-WATCHER.md`）。

---

## 新裝置安裝（plugin）

```
/plugin marketplace add OHIMEOPP/agent_mailbox
/plugin install agent-mailbox@agent-mailbox
```
→ 重啟 Claude Code。每個 mailbox 專案根放一個 `.mailbox-name` 檔（內容=instance 名，如 `wiki`）。監軍/supervisor 專案另放 `.mailbox-watch-args`=`--watch-all`。spoke 設 OS env `CLAUDE_MAILBOX_REMOTE`(hub URL) + `CLAUDE_MAILBOX_TOKEN`。

詳細 pattern 與工具：
- Plugin 本體 + watcher script: `claude-mailbox/`（marketplace = 本 repo）
- Dump: `claude-mailbox/mailbox-dump.py`（slash command `/mblog` 或 `/觀看紀錄`）
- Bridge (Discord↔mailbox): `claude-mailbox/bridge/`（docker container `mailbox-bridge`）
- Whitelist CLI: `claude-mailbox/tools/mailbox-whitelist.py`
