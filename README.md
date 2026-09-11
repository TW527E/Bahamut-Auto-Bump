# Bahamut Auto Bump

Linux 上以無頭 Chromium 每天檢查指定巴哈姆特文章，並在能確認「最新樓層不是今天」時發送一次頂文。時區固定由設定中的 `Asia/Taipei`（UTC+8）決定。

安全行為：登入失敗、TOTP 失敗、頁面載入逾時、選擇器不匹配、時間無法唯一解析、或發文後無法驗證，全部視為「無法確認」，不會發文，並由常駐服務稍後重試。狀態判斷只使用最新可見文章容器的最後一個發文時間；只要是今天，就不再送出頂文。

若日誌顯示頁面標題為 `請稍候...` 或 `Just a moment...`，這是巴哈/上游反爬驗證阻擋 headless Chromium，不是 selector 錯誤。程式會安全跳過並重試，不會繞過 CAPTCHA 或瀏覽器挑戰；請先確認主機 IP、瀏覽器依賴與站方存取權限。

## 安裝

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp config.example.toml config.toml
chmod 600 config.toml
```

編輯 `config.toml` 的帳號、密碼、TOTP secret、文章網址、每天執行時間，以及 Telegram Bot 的 `bot_token`、目標頻道 `target_chat_id` 和可控制通知的管理者 `admin_chat_id`。先把 Bot 加入目標頻道並授予發文權限；頻道 ID 通常是 `-100...`。管理者先對 Bot 傳訊息，再從 Telegram 的更新資料或 `getUpdates` 取得自己的數字 chat id。若巴哈姆特改版，先用瀏覽器開發者工具確認 `[selectors]` 中的選擇器，再用 `--once` 測試。

```sh
. .venv/bin/activate
python bahamut_auto_bump.py --config config.toml --once
```

## systemd 常駐服務

```sh
sudo useradd --system --home /opt/bahamut-auto-bump --shell /usr/sbin/nologin bahamut-bump
sudo mkdir -p /opt/bahamut-auto-bump
sudo cp -a . /opt/bahamut-auto-bump/
sudo chown -R bahamut-bump:bahamut-bump /opt/bahamut-auto-bump
sudo install -m 0644 bahamut-auto-bump.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bahamut-auto-bump.service
systemctl status bahamut-auto-bump.service
journalctl -u bahamut-auto-bump.service -f
```

服務本身會依設定的 `schedule.time` 計算每日執行時間；若當天尚未能安全確認，會依 `retry_interval_seconds` 重試。`state.json` 只在「今天已確認已有頂文」或「發文後重新讀取並驗證成功」時更新。

## Telegram 通知與指令

成功頂文與錯誤通知都會發送到 `target_chat_id` 頻道；登入/TOTP 問題是 `auth`，頁面選擇器或時間解析問題是 `layout`，其他可安全重試的錯誤是 `error`，未預期程式錯誤是 `system`。服務會在背景輪詢 `admin_chat_id` 管理者聊天中的指令，設定會保存到 `telegram_state.json`：

```text
/disable success       關閉成功通知
/disable error         關閉一般錯誤通知
/disable auth          關閉登入/TOTP 通知
/disable layout        關閉排版/時間解析通知
/disable system        關閉未預期錯誤通知
/disable all           關閉所有通知
/enable <類型|all>     重新開啟通知
/status                查看目前啟用的通知
/help                  查看指令
```

「今天已經頂過」是例行檢查結果，不會發送通知。Telegram API 連線失敗只寫入 systemd 日誌，避免通知失敗造成無限遞迴。Bot 必須能讀取管理者私聊訊息；若使用群組管理指令，請將該群組 ID 設為 `admin_chat_id`。

請勿把 `config.toml` 提交到 Git，也不要把 TOTP secret 分享給他人。使用本工具前請確認符合巴哈姆特帳號安全政策與版規。
