# AI 伺服器供應鏈 乖離警示機器人（Telegram，Android 可用）

用 GitHub Actions 在台股交易日自動執行，推播到手機上的 Telegram。

| 時間（台灣） | 動作 |
|---|---|
| 09:15 | 開盤報價：各檔開盤價與距購買區間 |
| 09:00–13:35，每 10 分鐘 | 盤中檢查：現價跌入購買區間或衝入超漲區間就推播（同檔同天只推一次） |
| 14:40 | 收盤整理：更新每日開高低收量、重算隔日購買區間價、推播重點 |

乖離定義：z = (收盤 − 均線) ÷ 同期標準差，週期 20／60／240 日。
購買區間價 = 下一個收盤價落到 z = −2 的價格（視窗為最近 N−1 日已知收盤價加上該價格），已換算為可成交的升降檔位。門檻可在 `stocks.json` 修改（例如改成 1.5）。

## 一、建立 Telegram 機器人（手機上操作）
1. 在 Telegram 搜尋 **@BotFather**，傳 `/newbot`，依指示取名，取得一串 **Token**（`123456:ABC...`，請保密）。
2. 打開你剛建立的機器人，按 **Start**，隨便傳一句話。
3. 在手機或電腦瀏覽器開啟 `https://api.telegram.org/bot<你的Token>/getUpdates`（把 `<你的Token>` 換掉），找到 `"chat":{"id":123456789` 這串數字，就是 **Chat ID**。

## 二、建立 GitHub 專案
1. 註冊或登入 GitHub，建立新的儲存庫（建議 **Public**：Actions 免費不限分鐘；Token 放在 Secrets，不會公開。若不想公開自選名單，可用 Private，每月 2,000 分鐘免費額度，本專案大約用到 1,000 分鐘左右，仍夠用但較緊）。
2. 把本資料夾所有檔案（含隱藏的 `.github` 資料夾）上傳到儲存庫根目錄。
3. 進入 **Settings → Secrets and variables → Actions → New repository secret**，新增兩個：
   - `TELEGRAM_BOT_TOKEN`：步驟一取得的 Token
   - `TELEGRAM_CHAT_ID`：步驟一取得的 Chat ID
4. 進入 **Settings → Actions → General → Workflow permissions**，選 **Read and write permissions** 並儲存（機器人要把每日開收價存回儲存庫）。

## 三、第一次啟動（照順序）
1. **Actions** 分頁 → 左側選 **bias-bot** → **Run workflow** → mode 選 `test`。手機收到「✅ 測試訊息」代表通知通了。
2. 再執行一次，mode 選 `close`：下載歷史資料、建立購買區間，並推播收盤整理。
3. 之後交易日會自動排程執行，不需要再動作。

## 四、Android 設定，避免漏通知
- Android 13 以上：設定 → 應用程式 → Telegram → 通知，允許通知。
- Telegram 內：設定 → 通知與聲音，確認「私人聊天」通知開啟、重要性設為高。
- 設定 → 電池 → Telegram：改為「不受限制」，關閉省電最佳化（各廠牌路徑略有不同）。
- 勿擾模式：把 Telegram 或這個機器人加入例外。
- 想要盤中警示更醒目：長按與機器人的聊天室 → 自訂通知，給它獨立的鈴聲。

## 五、自訂
- 增減標的、門檻、週期：編輯 `stocks.json`（`market`：`TW` 上市、`TWO` 上櫃）。
- 修改後，手動執行一次 `close` 重算區間。
- 本機測試：`pip install requests`，然後 `DRY_RUN=1 python bias_bot.py close`（只印出訊息，不發送）。單元測試：`python -m unittest discover -s tests`。

## 注意事項
- **延遲**：GitHub 排程常延遲數分鐘，尖峰時可能更久，所以不是逐筆即時。盤中報價優先用證交所的即時介面（非官方用法，可能被限流），失敗時改用 Yahoo（約延遲 15–20 分鐘）。訊息會標註資料來源。
- **休市**：以證交所回傳的資料日期判斷，不是交易日就不發送。
- **排程停用**：GitHub 會在儲存庫長時間（約 60 天）沒有活動時停用排程；若發現沒通知，到 Actions 分頁重新啟用即可。
- **資料來源**：歷史資料取自 Yahoo Finance，未還原權息，除權息日前後的乖離會受影響。
- **標的分類**：「寡佔/獨佔」為個人整理，只有信驊、川湖有報導依據，請自行查證。
- 技術面統計，非投資建議。
