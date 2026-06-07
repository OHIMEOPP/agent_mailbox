# digest — 每日 AI/LLM 新聞 digest（mailbox 附屬模組）

Windows Task Scheduler 每天觸發兩場，headless Claude 上網研究 AI / LLM / AI-agent 動態，
用自然繁體中文摘要，透過 mailbox bridge（`:1904`）DM 到使用者的 Discord：

| 時段 | 時間 | 主新聞時間窗 | 固定官方區塊 |
|------|------|------|------|
| 早場 | 09:20 | 昨天（過去約 24h） | 📌 Anthropic / Claude 官方 |
| 晚場 | 19:00 | 今天最新（過去約 10–12h） | 📌 OpenAI 官方 |

兩場用不同時間窗 + 不同官方區塊**自然錯開內容**（目前無去重狀態，靠這個避免整碗重複）。
時段不帶參數時依執行時間自動判斷（中午前=早場）；官方來源見 `sources.json` 的 `official_by_slot`。

> Discord 單則上限 2000 字，digest 太長會自動切成多則並標 `(1/3)(2/3)…` 讓你確認沒漏段；
> 短到一則裝得下時就不標。

> 為什麼用 Claude 而非「爬蟲 + 翻譯套件」：翻譯套件會有機翻腔、術語易錯、且不會濃縮重點。
> Claude 直接讀英文原文、用繁中摘要解讀，沒有 MT 中間層。

## 架構（職責分離）

```
Task Scheduler (09:20)
  └─ run-digest.ps1
       ├─ claude.exe -p  ──讀── sources.json / digest-prompt.md
       │     └─ WebSearch / WebFetch 研究 → 寫 digest-out.md（只負責產內容）
       └─ post-to-bridge.py digest-out.md
             └─ POST :1904/agent-notify → Discord DM（只負責投遞，自動分段）
```

投遞與內容生成分離：就算 Claude 當天表現不穩，投遞邏輯仍由 ps1/python 掌控；
Claude 沒產出檔案時會改送一則失敗通知。

## 檔案

| 檔 | 作用 |
|----|------|
| `sources.json` | 主題焦點 + 來源清單（**改這裡就能調整追蹤範圍**） |
| `digest-prompt.md` | 給 headless Claude 的指令（格式、規則） |
| `digest-settings.json` | 傳給 `claude --settings` 的設定（`disableAllHooks` 關掉桌寵/記憶萃取 hook） |
| `run-digest.ps1` | Task Scheduler 進入點 |
| `post-to-bridge.py` | UTF-8 安全、自動分段投遞到 bridge |
| `digest-out.md` | 每次跑產生的當日 digest（會被下次覆蓋） |
| `digest.log` | 執行記錄 |

## Windows / PowerShell 5.1 已踩過的雷（改檔前必讀）

1. **`run-digest.ps1` 必須存成 UTF-8 with BOM** — 否則 PS5.1 用 OEM 編碼解析中文註解失敗，exit 1、完全沒 log。
2. **不要把 JSON 字串 inline 傳給 `claude --settings`** — PS5.1 把含雙引號的字串傳原生 exe 時引號會被吃掉變壞 JSON；所以改用 `digest-settings.json` 檔案路徑。
3. **claude 那行的 `$ErrorActionPreference` 已放寬成 `Continue`** — PS5.1 會把原生程式 stderr 包成 ErrorRecord，在 `Stop` 下會中斷整個腳本（連投遞都跳過）。投遞以 `digest-out.md` 是否存在為準，不依賴 claude 的 exit code。
4. **關桌寵/記憶 hook 用 `disableAllHooks`，不要用 `--bare`** — `--bare` 會連 OAuth 一起關掉（只剩 ANTHROPIC_API_KEY），訂閱制會無法登入。

## 手動跑一次

```powershell
# 依目前時間自動判斷早/晚場：
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\User\Desktop\VSCcode\claude-mailbox\digest\run-digest.ps1
# 指定時段測試（不管現在幾點）：
powershell -NoProfile -ExecutionPolicy Bypass -File C:\...\run-digest.ps1 -Slot 早場
powershell -NoProfile -ExecutionPolicy Bypass -File C:\...\run-digest.ps1 -Slot 晚場
```

## 排程管理

```powershell
Get-ScheduledTask -TaskName 'AI-LLM-Daily-Digest'           # 看狀態
Start-ScheduledTask -TaskName 'AI-LLM-Daily-Digest'         # 立刻跑一次
Disable-ScheduledTask -TaskName 'AI-LLM-Daily-Digest'       # 暫停
Unregister-ScheduledTask -TaskName 'AI-LLM-Daily-Digest'    # 移除
```

## 注意

- 桌機要開機 + 已登入，且 `mailbox-bridge` 容器在跑（Discord 出口在本機）。
- 用的是 Claude 訂閱額度（每天一次、`sonnet` 模型，量很小）。
- 調主題：改 `sources.json`；調格式/語氣：改 `digest-prompt.md`；調時間：改排程的 trigger。
