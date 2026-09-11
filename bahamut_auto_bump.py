#!/usr/bin/env python3
"""Safely bump one Bahamut thread once per Taiwan calendar day."""

from __future__ import annotations

import argparse
import binascii
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOG = logging.getLogger("bahamut-auto-bump")
DEFAULT_CONFIG = Path("config.toml")


class ConfigError(ValueError):
    pass


class CannotConfirm(RuntimeError):
    """Raised whenever the page state is not safe enough to make a decision."""


@dataclass(frozen=True)
class Config:
    values: dict[str, Any]

    @property
    def account(self) -> dict[str, Any]:
        return self.values["account"]

    @property
    def thread(self) -> dict[str, Any]:
        return self.values["thread"]

    @property
    def selectors(self) -> dict[str, Any]:
        return self.values["selectors"]

    @property
    def browser(self) -> dict[str, Any]:
        return self.values["browser"]

    @property
    def schedule(self) -> dict[str, Any]:
        return self.values["schedule"]

    @property
    def telegram(self) -> dict[str, Any]:
        return self.values["telegram"]


def load_config(path: Path) -> Config:
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - Python 3.10 fallback message
        raise ConfigError("Python 3.11 or newer is required") from exc

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    for section in ("account", "thread", "selectors", "browser", "schedule", "telegram"):
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"Missing [{section}] section")
    required = {
        "account": ("username", "password", "totp_secret"),
        "thread": ("url",),
        "schedule": ("timezone", "time", "retry_interval_seconds"),
        "telegram": ("bot_token", "target_chat_id", "admin_chat_id"),
    }
    for section, keys in required.items():
        for key in keys:
            if not data[section].get(key):
                raise ConfigError(f"Missing [{section}] {key}")
    try:
        ZoneInfo(data["schedule"]["timezone"])
        datetime.strptime(data["schedule"]["time"], "%H:%M")
        if int(data["schedule"]["retry_interval_seconds"]) < 30:
            raise ConfigError("retry_interval_seconds must be at least 30")
        import pyotp
        pyotp.TOTP(data["account"]["totp_secret"].replace(" ", "")).now()
    except ImportError as exc:
        raise ConfigError("pyotp is required; run pip install -r requirements.txt") from exc
    except (KeyError, ValueError, TypeError, binascii.Error) as exc:
        raise ConfigError(f"Invalid schedule or TOTP setting: {exc}") from exc
    return Config(data)


def taiwan_now(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.schedule["timezone"]))


def parse_post_time(raw: str, timezone: ZoneInfo) -> datetime:
    """Parse ISO/HTML datetime or common Bahamut textual timestamps."""
    value = raw.strip().replace("年", "-").replace("月", "-").replace("日", "")
    value = re.sub(r"\s+", " ", value)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise CannotConfirm(f"Unparseable post timestamp: {raw!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def is_today(parsed: datetime, now: datetime) -> bool:
    return parsed.astimezone(now.tzinfo).date() == now.date()


def latest_post_timestamp(page: Any, config: Config, now: datetime) -> datetime:
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    time_selector = str(config.selectors.get("post_time_selector", "time[datetime], .edittime, .c-post__header time"))
    try:
        result = page.locator(selector).evaluate_all(
            """
            (posts, timeSelector) => posts.map((post) => {
              const visible = !!(post.offsetWidth || post.offsetHeight || post.getClientRects().length);
              const time = post.querySelector(timeSelector);
              return { visible, raw: time?.getAttribute('datetime') || time?.textContent?.trim() || '' };
            }).filter((item) => item.visible)
            """,
            time_selector,
        )
    except Exception as exc:
        raise CannotConfirm(f"Could not inspect post layout: {exc}") from exc
    if not result:
        raise CannotConfirm("No visible post containers matched post_selector")
    if any(not item["raw"] for item in result):
        raise CannotConfirm("At least one visible post has no readable timestamp")
    return parse_post_time(result[-1]["raw"], now.tzinfo)


def _first_visible(page: Any, selectors: str):
    for selector in selectors.split(","):
        locator = page.locator(selector.strip()).first
        if locator.count() and locator.is_visible():
            return locator
    return None


def login(page: Any, config: Config) -> None:
    try:
        import pyotp
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise CannotConfirm("pyotp is required; run pip install -r requirements.txt") from exc
    selectors = config.selectors
    timeout = int(config.browser.get("navigation_timeout_ms", 30000))
    page.goto(str(selectors.get("login_url", "https://user.gamer.com.tw/login.php")), wait_until="domcontentloaded")
    page.set_default_timeout(timeout)
    user = _first_visible(page, str(selectors.get("username", "#form-login input[name='userid']")))
    password = _first_visible(page, str(selectors.get("password", "#form-login input[name='password']")))
    submit = _first_visible(page, str(selectors.get("login_submit", "#btn-login")))
    if not user or not password or not submit:
        raise CannotConfirm("Login form layout is not recognized")
    user.fill(str(config.account["username"]))
    password.fill(str(config.account["password"]))
    submit.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError:
        LOG.warning("Login navigation timed out; checking visible state")

    otp = _first_visible(page, str(selectors.get("totp", "#input-2sa, input[name='twoStepAuth']")))
    if otp:
        secret = str(config.account["totp_secret"]).replace(" ", "")
        otp.fill(pyotp.TOTP(secret).now())
        otp_submit = _first_visible(page, str(selectors.get("totp_submit", "#btn-login, button[type='submit'], input[type='submit']")))
        if not otp_submit:
            raise CannotConfirm("TOTP field found but its submit control was not found")
        otp_submit.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except PlaywrightTimeoutError:
            LOG.warning("TOTP navigation timed out; checking visible state")

    logout = str(selectors.get("logout", "a[href*='logout'], [data-action='logout']"))
    login_form = str(selectors.get("password", "#form-login input[name='password']"))
    if not _first_visible(page, logout) and _first_visible(page, login_form):
        raise CannotConfirm("Login did not reach an authenticated state")


def inspect_and_maybe_bump(page: Any, config: Config, now: datetime) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise CannotConfirm("Playwright is required; run pip install -r requirements.txt") from exc
    page.goto(str(config.thread["url"]), wait_until="domcontentloaded")
    page.set_default_timeout(int(config.browser.get("navigation_timeout_ms", 30000)))
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    try:
        page.locator(selector).first.wait_for(state="attached")
    except PlaywrightTimeoutError as exc:
        raise CannotConfirm("Thread posts did not load in time") from exc
    latest = latest_post_timestamp(page, config, now)
    if is_today(latest, now):
        LOG.info("Latest post is dated %s; today's bump is already present", latest.isoformat())
        return "already_done"

    body = str(config.thread.get("content_template", "頂🆙！\n（{timestamp}）")).format(
        timestamp=now.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    )
    editor_selector = str(config.selectors.get("editor", "#editor"))
    submit_selector = str(config.selectors.get("post_submit", "[data-action='quick-post'], #quick-post, button[type='submit']"))
    editor = _first_visible(page, editor_selector)
    submit = _first_visible(page, submit_selector)
    if not editor:
        raise CannotConfirm("Reply editor layout is not recognized")
    editor_tag = editor.evaluate("element => element.tagName")
    if editor_tag == "IFRAME":
        page.frame_locator(editor_selector).locator("body").fill(body)
        # The quick-reply controls are injected only after authentication. When
        # their markup changes, submit the same form used by Bahamut's quickPost.
        page.evaluate(
            """
            (editorSelector) => {
              const iframe = document.querySelector(editorSelector);
              const doc = iframe && iframe.contentDocument;
              const edit = doc && doc.getElementsByClassName('editstyle')[0];
              const form = document.forms.frm;
              const target = form && form.elements.rtecontent;
              if (!edit || !form || !target) throw new Error('Bahamut editor form is not ready');
              target.value = typeof Bahacode === 'function' ? new Bahacode(edit).convert() : edit.innerHTML;
              if (window.bahaRte) window.bahaRte.onpost = 1;
              form.submit();
            }
            """,
            editor_selector,
        )
    else:
        if not submit:
            raise CannotConfirm("Reply submit control layout is not recognized")
        editor.fill(body)
        submit.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=int(config.browser.get("navigation_timeout_ms", 30000)))
    except PlaywrightTimeoutError:
        LOG.warning("Reply navigation timed out; reloading for verification")
    page.reload(wait_until="domcontentloaded")
    verified = latest_post_timestamp(page, config, now)
    if not is_today(verified, now):
        raise CannotConfirm("Reply was submitted but the latest post could not be verified as today")
    LOG.info("Bump submitted and verified at %s", verified.isoformat())
    return "posted"


def run_once(config: Config) -> str:
    now = taiwan_now(config)
    LOG.info("Checking %s at %s", config.thread["url"], now.isoformat())
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError as exc:
        raise CannotConfirm("Playwright is required; run pip install -r requirements.txt and playwright install chromium") from exc
    with sync_playwright() as playwright:
        launch_options = {"headless": bool(config.browser.get("headless", True))}
        if config.browser.get("executable_path"):
            launch_options["executable_path"] = str(config.browser["executable_path"])
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(locale="zh-TW", timezone_id=config.schedule["timezone"])
            page = context.new_page()
            login(page, config)
            return inspect_and_maybe_bump(page, config, now)
        finally:
            browser.close()


def state_path(config: Config) -> Path:
    return Path(str(config.schedule.get("state_file", "state.json")))


def read_state(path: Path) -> date | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return date.fromisoformat(value.get("last_confirmed_date", ""))
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_state(path: Path, confirmed: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"last_confirmed_date": confirmed.isoformat()}), encoding="utf-8")
    temporary.replace(path)


def scheduled_datetime(now: datetime, config: Config) -> datetime:
    hour, minute = map(int, str(config.schedule["time"]).split(":", 1))
    return datetime.combine(now.date(), dt_time(hour, minute), tzinfo=now.tzinfo)


NOTIFICATION_TYPES = {"success", "error", "auth", "layout", "system"}


class TelegramNotifier:
    """Small Bot API client; command state and update offset survive restarts."""

    def __init__(self, config: Config):
        self.token = str(config.telegram["bot_token"])
        self.target_chat_id = str(config.telegram["target_chat_id"])
        self.admin_chat_id = str(config.telegram["admin_chat_id"])
        self.path = Path(str(config.telegram.get("state_file", "telegram_state.json")))
        saved = self._read_state()
        self.offset = int(saved.get("offset", 0))
        self.disabled = set(saved.get("disabled", [])) & NOTIFICATION_TYPES

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_state(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"offset": self.offset, "disabled": sorted(self.disabled)}), encoding="utf-8")
        temporary.replace(self.path)

    def _api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return payload

    def send(self, kind: str, text: str) -> None:
        if kind in self.disabled:
            LOG.info("Telegram notification type %s is disabled", kind)
            return
        try:
            self._api("sendMessage", {"chat_id": self.target_chat_id, "text": text})
        except Exception as exc:
            LOG.warning("Telegram notification failed: %s", exc)

    def _command_reply(self, text: str) -> None:
        try:
            self._api("sendMessage", {"chat_id": self.admin_chat_id, "text": text})
        except Exception as exc:
            LOG.warning("Telegram command reply failed: %s", exc)

    def poll_commands(self) -> None:
        try:
            payload = self._api("getUpdates", {"offset": self.offset, "timeout": 0, "allowed_updates": '["message"]'})
        except Exception as exc:
            LOG.warning("Telegram command polling failed: %s", exc)
            return
        for update in payload.get("result", []):
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != self.admin_chat_id:
                continue
            command = str(message.get("text", "")).strip().split()
            if not command:
                continue
            name = command[0].split("@", 1)[0].lower()
            arg = command[1].lower() if len(command) > 1 else ""
            if name == "/disable" and (arg in NOTIFICATION_TYPES or arg == "all"):
                self.disabled = set(NOTIFICATION_TYPES) if arg == "all" else self.disabled | {arg}
                self._write_state()
                self._command_reply(f"已關閉通知類型：{arg}。使用 /enable {arg} 可重新開啟。")
            elif name == "/enable" and (arg in NOTIFICATION_TYPES or arg == "all"):
                self.disabled = set() if arg == "all" else self.disabled - {arg}
                self._write_state()
                self._command_reply(f"已開啟通知類型：{arg}。")
            elif name == "/status":
                enabled = sorted(NOTIFICATION_TYPES - self.disabled)
                self._command_reply("啟用通知：" + (", ".join(enabled) if enabled else "無"))
            elif name in {"/help", "/start"}:
                self._command_reply("指令：/disable <success|error|auth|layout|system|all>、/enable <類型|all>、/status、/help")
        self._write_state()


def notification_kind(exc: Exception) -> str:
    message = str(exc).lower()
    if any(word in message for word in ("login", "totp", "authenticated", "登入")):
        return "auth"
    if any(word in message for word in ("layout", "selector", "timestamp", "post containers", "樓層")):
        return "layout"
    if isinstance(exc, CannotConfirm):
        return "error"
    return "system"


def sleep_with_telegram(seconds: int, notifier: TelegramNotifier) -> None:
    deadline = time.monotonic() + seconds
    while True:
        notifier.poll_commands()
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            return
        time.sleep(min(30, remaining))


def run_loop(config: Config) -> None:
    retry = int(config.schedule["retry_interval_seconds"])
    path = state_path(config)
    notifier = TelegramNotifier(config)
    while True:
        notifier.poll_commands()
        now = taiwan_now(config)
        confirmed = read_state(path)
        due = scheduled_datetime(now, config)
        if confirmed == now.date():
            tomorrow = due + timedelta(days=1)
            sleep_for = max(30, int((tomorrow - now).total_seconds()))
            LOG.info("Today's state is confirmed; next check in %s seconds", sleep_for)
            sleep_with_telegram(min(sleep_for, 3600), notifier)
            continue
        if now < due:
            sleep_with_telegram(min(max(30, int((due - now).total_seconds())), 3600), notifier)
            continue
        try:
            result = run_once(config)
            write_state(path, now.date())
            if result == "posted":
                notifier.send("success", f"巴哈姆特頂文成功\n文章：{config.thread['url']}\n時間：{now.strftime('%Y-%m-%d %H:%M:%S UTC+8')}")
        except Exception as exc:
            LOG.warning("Unable to safely confirm today's status: %s; retrying in %s seconds", exc, retry)
            notifier.send(notification_kind(exc), f"巴哈姆特自動頂文發生問題\n文章：{config.thread['url']}\n類型：{notification_kind(exc)}\n原因：{exc}")
            sleep_with_telegram(retry, notifier)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="check once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        notifier = TelegramNotifier(config)
        if args.once:
            result = run_once(config)
            if result == "posted":
                notifier.send("success", f"巴哈姆特頂文成功\n文章：{config.thread['url']}")
        else:
            run_loop(config)
        return 0
    except (ConfigError, CannotConfirm) as exc:
        LOG.error("%s", exc)
        if 'notifier' in locals():
            notifier.send(notification_kind(exc), f"巴哈姆特自動頂文發生問題\n原因：{exc}")
        return 2
    except Exception:
        LOG.exception("Unexpected failure; no bump was attempted unless submission was already verified")
        if 'notifier' in locals():
            notifier.send("system", "巴哈姆特自動頂文發生未預期錯誤，請查看服務日誌。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
