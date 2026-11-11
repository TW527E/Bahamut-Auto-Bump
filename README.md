# Bahamut Auto Bump

Linux 上以系統安裝的 Google Chrome Stable 每天檢查指定巴哈姆特文章，並在能確認「最新樓層不是今天」時發送一次頂文。時區固定由設定中的 `Asia/Taipei`（UTC+8）決定。

安全行為：匯入的 Cookie/session 失效、首頁登入失敗、頁面載入逾時、選擇器不匹配、時間無法唯一解析、或發文後無法驗證，全部視為「無法確認」，不會發文，並由常駐服務稍後重試。狀態判斷只使用最新可見文章容器的最後一個發文時間；只要是今天，就不再送出頂文。腳本會優先使用有效的 Playwright `storage_state`，失效或不存在時才從巴哈姆特首頁右上角登入入口進行帳號、密碼與可選 TOTP 登入；不會直接導向 `login.php`。

巴哈姆特會對短時間大量請求回應 HTTP 429「系統維修中｜請關閉頁面後稍後再試」，這頁沒有文章也沒有站方訊息。腳本每次換頁至少間隔 `browser.navigation_interval_seconds`（預設 3 秒）再加上 0 到 `browser.navigation_jitter_seconds`（預設 2 秒）的隨機時間，遇到 429 會等待 `rate_limit_wait_seconds`（預設 60 秒，每次重試遞增）後重試最多 `rate_limit_retries` 次，仍被限制才回報錯誤。

為了減少對巴哈的訪問量，腳本會在 `state.json` 記住讀過哪些分頁（`seen_pages`）與最後一頁的頁碼（`last_page`）。新回覆只會出現在最後一頁，所以每天的檢查、送出前的確認與送出後的驗證都只重讀最後一頁，讀過的其他分頁不會再讀。清理舊頂文改用 `bump_inventory`：一份以文章編號（sn）記錄的自家頂文清單，第一次需要清理時全掃一次建立，之後由驗證成功的新頂文與刪除結果維護，不再重掃整篇文章。一次頂文因此約 6 到 8 次頁面載入，不是原本的四十幾次。

若日誌顯示 `Bahamut anti-bot challenge (Cloudflare)`，代表 Cloudflare 對 Chrome 上網所用的 IP 回應了驗證頁（HTTP 標頭 `cf-mitigated: challenge`；頁面標題先是 `巴哈姆特電玩資訊站 - 系統異常回報`，驗證腳本載入後變成 `請稍候...`），不是 selector 錯誤。首頁登入被擋下後，會暫停 `browser.challenge_login_pause_seconds`（預設 21600 秒）不再嘗試首頁登入，之後每連續再被擋一次暫停時間加倍，最多 `browser.challenge_login_pause_max_seconds`（預設 172800 秒）；首頁登入成功才歸零。暫停狀態存在 `state.json`，重啟服務不會立刻再撞一次。這段期間仍會照常檢查 session，用 `/session` 上傳新的 session 後下次重試就會使用。同一個挑戰不論頁面標題是哪一個、暫停多久，一天只通知一次。

Cloudflare 是依 IP 標記，服務用哪個 IP 上網就是哪個 IP 被標記。若不想讓服務主機（例如家裡）的 IP 被標記，可讓 Chrome 經由另一台機器（例如 VPS）上網：

```toml
[browser]
proxy = "socks5://127.0.0.1:1080"   # 例如 ssh -D 1080 -N user@vps 建立的通道
# 或 http 代理：proxy = "http://vps:3128"，需要帳密時設定 proxy_username／proxy_password
```

### 範例：經 Tailscale 從家裡的 OpenWrt 路由器上網

伺服器和家裡的 OpenWrt 路由器都在同一個 Tailscale 網路時，可以在路由器上跑 tinyproxy，讓伺服器的 Chrome 從家裡的 IP 上網。只有 Chrome 的流量會經過家裡，Telegram 與伺服器其他流量不變。以下的 `100.a.b.c` 是伺服器的 Tailscale IP，`100.x.y.z` 是路由器的（用 `tailscale ip -4` 查）。

在路由器上（OpenWrt 25 用 `apk`；24 以前把第一行換成 `opkg update && opkg install tinyproxy`）：

```sh
apk update && apk add tinyproxy
uci set tinyproxy.@tinyproxy[0].enabled='1'
uci -q delete tinyproxy.@tinyproxy[0].Allow
uci add_list tinyproxy.@tinyproxy[0].Allow='100.a.b.c'
uci -q delete tinyproxy.@tinyproxy[0].LogFile
uci set tinyproxy.@tinyproxy[0].Syslog='1'
uci commit tinyproxy
service tinyproxy enable
service tinyproxy restart
```

`Allow` 只列伺服器的 Tailscale IP，家裡區網與其他 Tailscale 裝置都不能使用這個代理。不要設定 `Listen` 為 Tailscale IP：開機時 tinyproxy 比 Tailscale 早啟動，會綁定失敗。也不要在防火牆對 WAN 開放 8888 埠。

在伺服器上先確認出口 IP 是家裡的，再寫進設定：

```sh
curl -x http://100.x.y.z:8888 https://ifconfig.me
```

```toml
[browser]
proxy = "http://100.x.y.z:8888"
```

`curl` 卡住逾時（而不是立刻回 403）代表封包沒到 tinyproxy，依序檢查：

- `tailscale ping 100.x.y.z` 會通但 `curl` 不通時，先在路由器上看 `ip addr show tailscale0` 有沒有 `inet 100.x.y.z`。沒有的話執行 `service tailscale restart`；`tailscale ping` 不經過這個介面，所以它通不代表 TCP 連得上。
- 路由器防火牆要允許 `tailscale0` 的輸入。先用 `uci show firewall | grep -i tailscale` 看是否已有包含 `tailscale0` 的區域，已有就不要再建同名區域（名稱重複會讓整份防火牆規則載入失敗）。
- Tailscale ACL 要允許伺服器連到路由器的 8888 埠。

之後家裡的 IP 就是巴哈看到的 IP：一旦被 Cloudflare 標記，你在家上巴哈也會多遇到驗證，所以請保持低請求量，不要手動反覆執行 `--once` 或 `--cleanup`。

Chrome 不支援需要帳密的 socks5 代理，有帳密時請改用 http 代理。換了出口 IP 後，原本的 session 可能需要在新 IP 上重新登入或用 `/session` 重新上傳。

## 建議做法：固定 Chrome 設定檔＋人工驗證

本工具不會嘗試自動破解或繞過 Cloudflare 人機驗證（不偽裝指紋、不改 User-Agent、不接驗證碼代解服務）。穩定運作的做法是讓驗證由人完成一次，之後服務以「同一個瀏覽器」回訪，盡量不再觸發驗證：

```toml
[browser]
user_data_dir = "/opt/Bahamut-Auto-Bump/chrome-profile"
headless = false
```

設定 `user_data_dir` 後，服務每次都用同一個 Chrome 設定檔，登入 Cookie 與你親自通過驗證後取得的 Cookie 都留在設定檔裡，不再每次用空白瀏覽器加上 JSON 重建。需要登入或被驗證擋下時，在服務主機上執行：

```sh
sudo -u bahamut-bump .venv/bin/python bahamut_auto_bump.py --config config.toml --manual-login
```

它會用同一個設定檔開啟一般的 Chrome 視窗（沒有掛上自動化控制），請親自通過驗證並登入巴哈，看到文章後關閉整個 Chrome 視窗；接著會檢查設定檔是否已登入，並解除首頁登入的暫停。執行期間持有 `state.json.lock`，不需要停止服務。Telegram 的驗證通知也會提示這個步驟。

注意事項：

- 驗證結果綁定 IP 與瀏覽器，所以必須在服務主機上、用服務的出口 IP 操作（設定了 `browser.proxy` 時視窗也會走同一個代理）。在別台電腦通過驗證再搬 Cookie 過來沒有用。
- 請用 `headless = false`。headless Chrome 的 User-Agent 是 `HeadlessChrome`，和你親自驗證的不是同一個瀏覽器。沒有螢幕的伺服器請安裝 Xvfb，並把 systemd 的 `ExecStart` 改為 `xvfb-run -a /opt/Bahamut-Auto-Bump/.venv/bin/python ...`；`--manual-login` 需要看得到視窗，可用 `ssh -X` 或 VNC。
- 最穩定的環境是家用網路上、偶爾能開視窗的機器（桌機、迷你主機）。機房／VPS 的 IP 本身就容易被驗證。
- 設定了 `user_data_dir` 時，`storage_state` 檔案只在設定檔未登入時匯入（Chrome 關閉時會丟掉沒有期限的 Cookie，所以每次驗證成功都會把 Cookie 寫回檔案，下次需要時再匯入）；匯入後仍未登入的檔案會被略過，直到換上新檔案（例如用 `/session` 上傳）。設定檔目錄含登入 Cookie，權限為 700，不可提交 Git。

每次 session 驗證成功或首頁登入成功，都會把瀏覽器目前的 Cookie 寫回 `storage_state`，讓巴哈輪替後的登入 Cookie 持續保存在檔案中。

要確認主機看到的是什麼，可停止服務後執行 `diagnose_access.py`。它會用服務的設定分別以純 HTTP、帶 session 的 Chrome 與不帶 session 的 Chrome 開啟文章與首頁，把報告、截圖與 HTML 存到 `diagnostics/`（不會寫出 Cookie 值或密碼）：

```bash
sudo systemctl stop bahamut-auto-bump
cd /opt/Bahamut-Auto-Bump && sudo -u bahamut-bump .venv/bin/python diagnose_access.py --config config.toml
sudo systemctl start bahamut-auto-bump
```

純 HTTP 也拿到 `cf-mitigated: challenge` 表示 IP 被擋（設定了 http 代理時純 HTTP 也走代理，socks5 代理則是直連，報告的 `plain_http_via` 會註明）；只有 headless Chrome 被擋時可加 `--headed` 並以 `xvfb-run -a` 執行，比較一般 Chrome 是否能通過。

若伺服器已能正常使用帳密登入，可在設定檔提供帳號資料；也可以在有圖形介面與正常瀏覽器的電腦上手動登入並完成站方驗證，再匯出 Playwright session。匯出工具預設使用系統安裝的 Google Chrome Stable，不會下載或使用 Chromium for Testing：

```sh
python export_bahamut_session.py --output bahamut-session.json
chmod 600 bahamut-session.json
scp bahamut-session.json user@your-server:/tmp/
# 在伺服器上：sudo install -m 600 -o bahamut-bump -g bahamut-bump /tmp/bahamut-session.json /opt/Bahamut-Auto-Bump/
```

在 Linux 的 `config.toml` 設定（`storage_state` 可保留作為優先使用的 session）：

```toml
[browser]
storage_state = "/opt/Bahamut-Auto-Bump/bahamut-session.json"
```

若要啟用 session 失效後的自動登入：

```toml
[account]
username = "你的巴哈帳號"
password = "你的巴哈密碼"
totp_secret = "你的 TOTP Base32 秘密（沒有就留空）"

[browser]
homepage_url = "https://www.gamer.com.tw/"
```

腳本會先開啟 `homepage_url`。若首頁當天顯示全版封面廣告（只有「進入巴哈姆特 >>」連結），會先點進該連結指向的首頁（`homepage_cover_enter` 選擇器）。若首頁第一次顯示「登入後可從置頂導航搜尋、接收通知與展開個人選單」提示，會先按下 `Close` 關閉，再點擊右上角「登入」，最後填寫登入 iframe 中的欄位；提示不存在時會直接略過。所有登入選擇器（包含 `login_onboarding` 與 `login_onboarding_close`）都在 `[selectors]`，可依巴哈姆特改版調整。若首頁本身被站方挑戰頁阻擋，腳本會停止並通知。

這個檔案含有登入 Cookie，不能提交 Git 或貼到聊天中。登入 Cookie 可能因 IP、瀏覽器指紋或有效期限而失效；若仍看到 `請稍候...`，代表 Cloudflare 擋的是這個 IP，轉移 session 也沒用，需等 IP 冷卻或改用 `browser.proxy` 換出口 IP。

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

只有在 `[account] totp_secret` 要填入 TOTP 兩步驗證秘密時，才需要額外安裝 `pip install "pyotp>=2.9,<3"`。

編輯 `config.toml` 的文章網址、每天執行時間、可選的 `browser.storage_state` 路徑與 `[account]` 帳號資料，以及 Telegram Bot 的 `bot_token`、目標頻道 `target_chat_id` 和可控制通知的管理者 `admin_chat_id`。先把 Bot 加入目標頻道並授予發文權限；頻道 ID 通常是 `-100...`。管理者先對 Bot 傳訊息，再從 Telegram 的更新資料或 `getUpdates` 取得自己的數字 chat id。若巴哈姆特改版，先用瀏覽器開發者工具確認 `[selectors]` 中的選擇器，再用 `--once` 測試。

```sh
. .venv/bin/activate
python bahamut_auto_bump.py --config config.toml --once
```

## 自動清理舊頂文

腳本只會刪除「屬於目前登入帳號、且內文符合 `content_template` 頂文格式」的回覆，並保留一樓與最新的幾筆頂文（至少一筆）；你自己發的其他回覆永遠不會被刪除。內文由 `[selectors] post_content_selector`（預設 `.c-article__content`）讀取，比對時忽略空白，`{timestamp}` 對應 `YYYY-MM-DD HH:MM:SS UTC+8`。刪除動作每筆至少間隔 5 秒；自動頂文則會在新頂文重新載入確認成功後等待 5 秒，再刪除**所有**更早的頂文，因此某天漏刪的頂文會在下一次頂文時一併補刪。

若剛送出的頂文本身無法被辨識為頂文格式（例如巴哈改版導致 `post_content_selector` 失效，或用 `/set_message` 換了訊息後舊格式的頂文），清理會跳過並送出 `layout` 錯誤通知，但不會影響隔天頂文。改過訊息格式後，舊格式的頂文需手動刪除。

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

服務本身會依設定的 `schedule.time` 計算每日執行時間，再延後 0 到 `schedule.schedule_jitter_seconds`（預設 1800 秒）的隨機時間（同一天內固定）。若當天尚未能安全確認會重試：第一次等 `retry_interval_seconds`，之後每連續失敗一次加倍，最多 `retry_backoff_max_seconds`（預設 7200 秒），每次再加上 0 到 `retry_jitter_seconds`（預設 300 秒）的隨機時間；成功一次或用 `/session` 上傳新 session 後歸零，上傳後會立刻重試。`state.json` 記錄「今天已確認日期」，並在頂文送出、驗證與刪除舊頂文的過程中保存 `pending_bump` 進度。

## 自動補完上次未完成的頂文

若上次頂文送出後因逾時、驗證失敗或刪除舊頂文中途出錯而沒有走完，`state.json` 會留下 `pending_bump` 記錄（含發文前最大文章編號）。下次啟動頂文前會先讀取這筆記錄，並在檢查新頂文之前補完：

- 若上次其實已送出成功（出現編號比發文前更大、且屬於本帳號的新回覆），會直接認定為已頂，不會重複發文。只會檢查最後一頁與前一頁，不會全掃。
- 若上次已標記發文成功但刪除舊頂文沒完成，會依 `bump_inventory` 刪除除了最新一筆以外所有符合頂文格式的舊頂文。
- 只有補完後仍無法確認今天有頂文時，才會送出新的頂文。

整個頂文與清理流程以 `state.json.lock` 檔案鎖互斥，避免常駐服務與手動 `--once`／`--cleanup` 同時操作同一文章。補完成功時會送出「上次頂文與清理已補完」的成功通知。


## Telegram 通知與指令

成功頂文通知會發送到 `target_chat_id` 頻道，錯誤與失敗通知則私訊給 `admin_chat_id`；Cookie/session 驗證問題是 `auth`，頁面選擇器或時間解析問題是 `layout`，其他可安全重試的錯誤是 `error`，未預期程式錯誤是 `system`。服務會在背景以 `telegram.poll_interval_seconds` 輪詢 `admin_chat_id` 管理者聊天中的指令，預設每 2 秒一次，設定會保存到 `telegram_state.json`。瀏覽器正在執行頂文或清理時，指令會等該次瀏覽器操作結束後處理。

Bot 啟動時會透過 Telegram `setMyCommands` 註冊指令，因此在聊天輸入 `/` 時會出現指令提示。輸入 `/toggle` 會顯示中文通知選單；每個按鈕以 `✅` 表示開啟、`❌` 表示關閉，不使用括弧顯示狀態。點擊後按鈕會立即切換並刷新為最新狀態，也支援 `/toggle success` 直接切換指定類型：

```text
/toggle <類型|all>     切換通知開關狀態
/session               等待下一則上傳的 session JSON 並替換目前檔案
/test_cookie           立即測試匯入的 Cookie/session 是否仍有效
/test_login            忽略現有 session，測試帳號密碼/TOTP 自動登入
/set_message <訊息>    設定頂文訊息，可使用 {timestamp}
/set_message default   恢復使用 config.toml 的 content_template
/status                查看目前啟用的通知
/help                  查看指令
```

「今天已經頂過」是例行檢查結果，不會發送通知。Telegram API 連線失敗只寫入 systemd 日誌，避免通知失敗造成無限遞迴。Bot 必須能讀取管理者私聊訊息；若使用群組管理指令，請將該群組 ID 設為 `admin_chat_id`。

`/session` 只接受 `admin_chat_id` 發出的命令；送出命令後直接上傳新的 `bahamut-session.json` 文件即可。Bot 會先驗證 JSON 包含 Playwright `cookies` 陣列，再以原子方式替換 `browser.storage_state` 指定的檔案。替換後可用 `/test_cookie` 立即確認登入狀態。若沒有 session 或想確認帳密自動登入，可使用 `/test_login`；它會用乾淨的 Chrome context 從首頁登入，不執行頂文或清理，成功後若設定了 `storage_state` 會保存新的 session。Cookie/session 失效時，例行檢查會把 `auth` 通知私訊給 `admin_chat_id`。同一種問題（類型與原因相同，比對時忽略頁面標題與數字）一天只通知一次，服務仍會持續重試（間隔見上方 systemd 一節）。

請勿把 `config.toml` 或 `bahamut-session.json` 提交到 Git，也不要分享匯出的 Cookie/session。使用本工具前請確認符合巴哈姆特帳號安全政策與版規。
