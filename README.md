# Bahamut Auto Bump

Linux 上以系統安裝的 Google Chrome Stable 每天檢查指定巴哈姆特文章，並在能確認「最新樓層不是今天」時發送一次頂文。時區固定由設定中的 `Asia/Taipei`（UTC+8）決定。

安全行為：匯入的 Cookie/session 失效、頁面載入逾時、選擇器不匹配、時間無法唯一解析、或發文後無法驗證，全部視為「無法確認」，不會發文，並由常駐服務稍後重試。狀態判斷只使用最新可見文章容器的最後一個發文時間；只要是今天，就不再送出頂文。主腳本沒有帳號、密碼或 TOTP 登入功能，只接受手動匯出的 Playwright `storage_state`。

若日誌顯示頁面標題為 `請稍候...` 或 `Just a moment...`，這是巴哈/上游反爬驗證阻擋 headless Chrome，不是 selector 錯誤。程式會安全跳過並重試，不會繞過 CAPTCHA 或瀏覽器挑戰；請先確認主機 IP、瀏覽器依賴與站方存取權限。

請在有圖形介面與正常瀏覽器的電腦上手動登入並完成站方驗證，再匯出 Playwright session。匯出工具預設使用系統安裝的 Google Chrome Stable，不會下載或使用 Chromium for Testing：

```sh
python export_bahamut_session.py --output bahamut-session.json
chmod 600 bahamut-session.json
scp bahamut-session.json root@goodvnic:/opt/Bahamut-Auto-Bump/
```

在 Linux 的 `config.toml` 設定：

```toml
[browser]
storage_state = "/opt/Bahamut-Auto-Bump/bahamut-session.json"
```

這個檔案含有登入 Cookie，不能提交 Git 或貼到聊天中。登入 Cookie 可能因 IP、瀏覽器指紋或有效期限而失效；若 VPS 仍看到 `請稍候...`，代表反爬驗證不接受轉移的 session，應改在被允許的網路環境執行，不能靠腳本繞過驗證。

## 安裝

先確認 Linux 主機是 `amd64`，再安裝正式版 Google Chrome。Google 官方沒有提供 Linux `arm64` 的 Chrome Stable 套件；若 `dpkg --print-architecture` 顯示 `arm64`，需改用 x86_64 主機才能採用本方案。

```sh
dpkg --print-architecture
curl -LO https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
google-chrome-stable --version

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
chmod 600 config.toml
```

編輯 `config.toml` 的文章網址、每天執行時間、必要的 `browser.storage_state` 路徑，以及 Telegram Bot 的 `bot_token`、目標頻道 `target_chat_id` 和可控制通知的管理者 `admin_chat_id`。先把 Bot 加入目標頻道並授予發文權限；頻道 ID 通常是 `-100...`。管理者先對 Bot 傳訊息，再從 Telegram 的更新資料或 `getUpdates` 取得自己的數字 chat id。若巴哈姆特改版，先用瀏覽器開發者工具確認 `[selectors]` 中的選擇器，再用 `--once` 測試。

```sh
. .venv/bin/activate
python bahamut_auto_bump.py --config config.toml --once
```

## 自動清理舊頂文

腳本可保留一樓與最新的幾筆回覆，並刪除更早、且屬於目前登入帳號的回覆。一樓永遠不會列入刪除候選。刪除動作每筆至少間隔 5 秒；自動頂文則會在新頂文重新載入確認成功後等待 5 秒，再嘗試刪除上一筆舊頂文。

先使用 dry-run 查看候選樓層，不會修改文章：

```sh
python bahamut_auto_bump.py --config config.toml --cleanup --cleanup-limit 1
```

確認候選正確後，才使用 `--apply` 執行刪除。建議先限制一筆，逐次觀察日誌與文章頁面：

```sh
python bahamut_auto_bump.py --config config.toml --cleanup --apply --cleanup-limit 1
```

設定檔中的 `[cleanup]` 可控制常駐服務行為：

```toml
[cleanup]
enabled = true
dry_run = false
keep_latest_replies = 1
interval_seconds = 5
after_bump_delay_seconds = 5
```

若要先讓常駐服務只觀察、不刪除，保留 `dry_run = true`。腳本會攔截巴哈姆特頁面自己的 `pdel` 參數並檢查文章擁有權；每次刪除後再以該文的 `Co.php` 網址確認站方回覆「文章不存在或已被刪除」。無法取得樓層、文章編號、站方刪文參數或明確的刪除結果時會停止，不會猜測刪除網址。

執行端固定使用系統安裝的正式 Google Chrome：

```toml
[browser]
channel = "chrome"
headless = true
```

這只使用 Playwright Python API 控制已安裝的 Chrome，不需要也不應執行 `playwright install chromium`。伺服器沒有顯示服務時保持 `headless = true` 即可。

## systemd 常駐服務

```sh
sudo useradd --system --home /opt/Bahamut-Auto-Bump --shell /usr/sbin/nologin bahamut-bump
sudo mkdir -p /opt/Bahamut-Auto-Bump
sudo cp -a . /opt/Bahamut-Auto-Bump/
sudo chown -R bahamut-bump:bahamut-bump /opt/Bahamut-Auto-Bump
sudo install -m 0644 bahamut-auto-bump.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bahamut-auto-bump.service
systemctl status bahamut-auto-bump.service
journalctl -u bahamut-auto-bump.service -f
```

服務本身會依設定的 `schedule.time` 計算每日執行時間；若當天尚未能安全確認，會依 `retry_interval_seconds` 重試。`state.json` 只在「今天已確認已有頂文」或「發文後重新讀取並驗證成功」時更新。

## Telegram 通知與指令

成功頂文與錯誤通知都會發送到 `target_chat_id` 頻道；Cookie/session 驗證問題是 `auth`，頁面選擇器或時間解析問題是 `layout`，其他可安全重試的錯誤是 `error`，未預期程式錯誤是 `system`。服務會在背景輪詢 `admin_chat_id` 管理者聊天中的指令，設定會保存到 `telegram_state.json`：

```text
/disable success       關閉成功通知
/disable error         關閉一般錯誤通知
/disable auth          關閉 Cookie/session 通知
/disable layout        關閉排版/時間解析通知
/disable system        關閉未預期錯誤通知
/disable all           關閉所有通知
/enable <類型|all>     重新開啟通知
/status                查看目前啟用的通知
/help                  查看指令
```

「今天已經頂過」是例行檢查結果，不會發送通知。Telegram API 連線失敗只寫入 systemd 日誌，避免通知失敗造成無限遞迴。Bot 必須能讀取管理者私聊訊息；若使用群組管理指令，請將該群組 ID 設為 `admin_chat_id`。

請勿把 `config.toml` 或 `bahamut-session.json` 提交到 Git，也不要分享匯出的 Cookie/session。使用本工具前請確認符合巴哈姆特帳號安全政策與版規。
