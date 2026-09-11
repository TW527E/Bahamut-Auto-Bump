#!/usr/bin/env python3
"""Safely bump one Bahamut thread once per Taiwan calendar day."""

from __future__ import annotations

import argparse
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

    @property
    def cleanup(self) -> dict[str, Any]:
        return self.values.get("cleanup", {})


@dataclass(frozen=True)
class PostInfo:
    floor: int
    sn: str
    owner: bool
    raw_time: str
    text: str


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

    for section in ("thread", "selectors", "browser", "schedule", "telegram"):
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"Missing [{section}] section")
    required = {
        "thread": ("url",),
        "browser": ("storage_state",),
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
    except (KeyError, ValueError, TypeError) as exc:
        raise ConfigError(f"Invalid schedule setting: {exc}") from exc
    return Config(data)


def taiwan_now(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.schedule["timezone"]))


def parse_post_time(raw: str, timezone: ZoneInfo, reference: datetime | None = None) -> datetime:
    """Parse ISO/HTML datetime or common Bahamut textual timestamps."""
    value = raw.strip()
    relative = re.search(r"(今天|昨天|前天)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if relative:
        offsets = {"今天": 0, "昨天": 1, "前天": 2}
        base = (reference or datetime.now(timezone)).astimezone(timezone)
        hour = int(relative.group(2))
        minute = int(relative.group(3))
        second = int(relative.group(4) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise CannotConfirm(f"Invalid relative post timestamp: {raw!r}")
        post_date = base.date() - timedelta(days=offsets[relative.group(1)])
        return datetime.combine(post_date, dt_time(hour, minute, second), tzinfo=timezone)
    textual = re.search(r"\d{4}(?:[-/]\d{2}[-/]\d{2}|年\d{2}月\d{2}日)\s+\d{2}:\d{2}(?::\d{2})?", value)
    if textual:
        value = textual.group(0)
    value = value.replace("年", "-").replace("月", "-").replace("日", "")
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


def _navigate_to_last_page(page: Any, config: Config) -> None:
    current = urllib.parse.urlparse(str(page.url))
    current_query = urllib.parse.parse_qs(current.query)
    current_page = int(current_query.get("page", ["1"])[0])
    try:
        hrefs = page.locator("a[href*='page=']").evaluate_all(
            "links => links.map((link) => link.href).filter(Boolean)"
        )
    except Exception as exc:
        raise CannotConfirm(f"Could not inspect thread pagination: {exc}") from exc

    page_numbers = {current_page}
    for href in hrefs:
        parsed = urllib.parse.urlparse(str(href))
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path != current.path:
            continue
        if query.get("bsn") != current_query.get("bsn") or query.get("snA") != current_query.get("snA"):
            continue
        try:
            page_numbers.add(int(query.get("page", [""])[0]))
        except ValueError:
            continue

    last_page = max(page_numbers)
    if last_page <= current_page:
        return
    current_query["page"] = [str(last_page)]
    target = urllib.parse.urlunparse(
        current._replace(query=urllib.parse.urlencode(current_query, doseq=True))
    )
    page.goto(target, wait_until="domcontentloaded")
    timeout = int(config.browser.get("navigation_timeout_ms", 30000))
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    try:
        page.locator(selector).first.wait_for(state="attached", timeout=timeout)
    except Exception as exc:
        raise CannotConfirm("The last thread page did not load its posts in time") from exc


def latest_post_timestamp(page: Any, config: Config, now: datetime) -> datetime:
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    time_selector = str(config.selectors.get("post_time_selector", "time[datetime], .edittime, .c-post__header time"))
    try:
        result = page.locator(selector).evaluate_all(
            """
            (posts, timeSelector) => posts.map((post) => {
              const visible = !!(post.offsetWidth || post.offsetHeight || post.getClientRects().length);
              const time = post.querySelector(timeSelector);
              return {
                visible,
                raw: time?.getAttribute('datetime') || time?.getAttribute('data-mtime') || time?.textContent?.trim() || ''
              };
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
    return parse_post_time(result[-1]["raw"], now.tzinfo, now)


def _thread_page_urls(page: Any, config: Config) -> list[str]:
    """Return every numbered page URL advertised by the thread pagination."""
    current = urllib.parse.urlparse(str(page.url))
    current_query = urllib.parse.parse_qs(current.query)
    try:
        hrefs = page.locator("a[href*='page=']").evaluate_all(
            "links => links.map((link) => link.href).filter(Boolean)"
        )
    except Exception as exc:
        raise CannotConfirm(f"Could not inspect thread pagination: {exc}") from exc
    pages = {1}
    for href in hrefs:
        parsed = urllib.parse.urlparse(str(href))
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path != current.path:
            continue
        if query.get("bsn") != current_query.get("bsn") or query.get("snA") != current_query.get("snA"):
            continue
        try:
            pages.add(int(query.get("page", [""])[0]))
        except (ValueError, TypeError):
            continue
    urls = []
    for number in range(1, max(pages) + 1):
        query = dict(current_query)
        query["page"] = [str(number)]
        urls.append(urllib.parse.urlunparse(current._replace(query=urllib.parse.urlencode(query, doseq=True))))
    return urls


def collect_posts(page: Any, config: Config) -> list[PostInfo]:
    """Load all thread pages and collect only data needed for safe cleanup."""
    timeout = int(config.browser.get("navigation_timeout_ms", 30000))
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    time_selector = str(config.selectors.get("post_time_selector", ".edittime"))
    page.set_default_timeout(timeout)
    try:
        configured = urllib.parse.urlparse(str(config.thread["url"]))
        current = urllib.parse.urlparse(str(page.url))
        configured_query = urllib.parse.parse_qs(configured.query)
        current_query = urllib.parse.parse_qs(current.query)
        same_thread = (
            current.path == configured.path
            and current_query.get("bsn") == configured_query.get("bsn")
            and current_query.get("snA") == configured_query.get("snA")
        )
        if not same_thread or page.locator(selector).count() == 0:
            page.goto(str(config.thread["url"]), wait_until="domcontentloaded")
        page.locator(selector).first.wait_for(state="attached", timeout=timeout)
        page_urls = _thread_page_urls(page, config)
    except Exception as exc:
        try:
            title = page.title()
            url = page.url
        except Exception:
            title, url = "<unavailable>", "<unavailable>"
        raise CannotConfirm(
            f"Could not inspect thread pages; url={url!r}, title={title!r}: {exc}"
        ) from exc

    result: list[PostInfo] = []
    for page_url in page_urls:
        if str(page.url) != page_url:
            page.goto(page_url, wait_until="domcontentloaded")
        try:
            posts_locator = page.locator(selector)
            if posts_locator.count() == 0:
                # Deleting replies can shrink the final page while an older
                # pagination link is still present. Since pages are scanned in
                # ascending order, an empty page after collected posts is a
                # stale trailing page; an empty first page remains an error.
                if result:
                    LOG.info("Reached an empty trailing page after cleanup; stopping at the new last page")
                    break
                raise CannotConfirm(f"Thread page has no post containers: {page_url}")
            posts_locator.first.wait_for(state="attached", timeout=timeout)
            rows = page.locator("section[id^='post_']").evaluate_all(
                """
                (sections, timeSelector) => sections.map((section) => {
                  const floorLink = Array.from(section.querySelectorAll('a')).find(a => /\\d+\\s*樓/.test((a.textContent || '').trim()));
                  // The section also contains data-tippy attributes for GP/BP
                  // counters; only the option-menu metadata includes owner.
                  const menu = section.querySelector('button.tippy-option-menu') || section.querySelector('[data-tippy]');
                  let meta = {};
                  try { meta = JSON.parse(menu?.getAttribute('data-tippy') || '{}'); } catch (_) {}
                  const time = section.querySelector(timeSelector) || section.querySelector('time[datetime], .edittime');
                  return {
                    floor: (floorLink?.textContent || '').match(/(\\d+)\\s*樓/)?.[1] || '',
                    sn: String(meta.sn || section.id.replace(/^post_/, '')),
                    owner: meta.owner === true,
                    raw: time?.getAttribute('datetime') || time?.getAttribute('data-mtime') || time?.textContent?.trim() || '',
                    text: (section.innerText || '').trim()
                  };
                }).filter(item => item.sn)
                """,
                time_selector,
            )
        except CannotConfirm:
            raise
        except Exception as exc:
            try:
                title = page.title()
                url = page.url
            except Exception:
                title, url = "<unavailable>", "<unavailable>"
            raise CannotConfirm(f"Could not inspect post layout; url={url!r}, title={title!r}: {exc}") from exc
        for row in rows:
            try:
                floor = int(row["floor"] or (1 if not result else 0))
            except (TypeError, ValueError):
                floor = 0
            if floor <= 0:
                raise CannotConfirm(f"Could not determine floor for post {row.get('sn')}")
            if not row.get("raw"):
                raise CannotConfirm(f"Post {row.get('sn')} has no readable timestamp")
            result.append(PostInfo(floor, str(row["sn"]), bool(row["owner"]), str(row["raw"]), str(row.get("text", ""))))
    if not result:
        raise CannotConfirm("No posts were found for cleanup")
    return sorted({post.sn: post for post in result}.values(), key=lambda post: post.floor)


def deletion_candidates(posts: list[PostInfo], keep_latest_replies: int = 1) -> list[PostInfo]:
    """Choose owned replies older than the retained newest replies; floor 1 is immutable."""
    keep = max(0, int(keep_latest_replies))
    replies = sorted((post for post in posts if post.floor > 1 and post.owner), key=lambda post: post.floor, reverse=True)
    return replies[keep:]


def _delete_request(page: Any, sn: str) -> dict[str, str]:
    """Capture the site's own pdel arguments without opening its confirmation dialog."""
    request = page.evaluate(
        """
        (postSn) => {
          if (typeof window.pdel === 'function' && typeof window.delPost === 'function') {
            const original = window.delPost;
            let captured = null;
            window.delPost = (sn, args, cookie) => { captured = {sn: String(sn), args: String(args), cookie: String(cookie || '')}; };
            try { window.pdel(postSn); } finally { window.delPost = original; }
            if (captured) return captured;
          }
          // Some page revisions keep pdel in an inline script that failed to
          // publish a global function. Parse only its simple generated args;
          // anything more complex fails closed in Python below.
          const source = Array.from(document.scripts).map(s => s.textContent || '').find(t => t.includes('function pdel')) || '';
          const match = source.match(/var\\s+args\\s*=\\s*['"]([^'"]*)['"]\\s*\\+\\s*sn\\s*\\+\\s*['"]([^'"]*)['"][\\s\\S]*?delPost\\(sn\\s*,\\s*args\\s*,\\s*['"]([^'"]*)['"]\\s*\\)/);
          if (!match) return null;
          return {sn: String(postSn), args: match[1] + String(postSn) + match[2], cookie: match[3]};
        }
        """,
        sn,
    )
    if not request or request.get("sn") != str(sn) or not request.get("args"):
        raise CannotConfirm(f"Could not obtain Bahamut delete parameters for post {sn}")
    params = urllib.parse.parse_qs(request["args"], keep_blank_values=True)
    required = {"bsn", "snA", "sn", "type", "code", "pwd"}
    if not required.issubset(params) or params["sn"][0] != str(sn) or params["type"][0] != "4":
        raise CannotConfirm(f"Bahamut delete parameters for post {sn} are incomplete")
    request["cookie"] = str(request.get("cookie", ""))
    return request


def delete_post(page: Any, config: Config, post: PostInfo) -> None:
    # pdel is generated on thread pages and contains the current delete token.
    page.goto(str(config.thread["url"]), wait_until="domcontentloaded")
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    page.locator(selector).first.wait_for(
        state="attached", timeout=int(config.browser.get("navigation_timeout_ms", 30000))
    )
    request = _delete_request(page, post.sn)
    if request["cookie"]:
        page.evaluate(
            "cookie => { if (typeof window.setCookie === 'function') window.setCookie('ckFORUM_pdel', cookie); }",
            request["cookie"],
        )
    endpoint = urllib.parse.urljoin(str(page.url), "post2.php?" + request["args"])
    try:
        page.goto(endpoint, wait_until="domcontentloaded")
    except Exception as exc:
        # Bahamut may redirect while Playwright is waiting for DOMContentLoaded;
        # the caller always verifies the post is gone before treating this as success.
        LOG.warning("Delete navigation ended early; verifying the post: %s", exc)


def verify_post_deleted(page: Any, config: Config, post: PostInfo) -> None:
    """Verify one deletion through Bahamut's canonical single-post URL."""
    thread = urllib.parse.urlparse(str(config.thread["url"]))
    query = urllib.parse.parse_qs(thread.query)
    bsn = query.get("bsn", [""])[0]
    if not bsn:
        raise CannotConfirm("Thread URL has no bsn for deletion verification")
    verification_url = urllib.parse.urljoin(
        str(config.thread["url"]),
        "Co.php?" + urllib.parse.urlencode({"bsn": bsn, "sn": post.sn}),
    )
    page.goto(verification_url, wait_until="domcontentloaded")
    timeout = int(config.browser.get("navigation_timeout_ms", 30000))
    page.locator("body").wait_for(state="attached", timeout=timeout)
    if page.locator(f"#post_{post.sn}").count():
        raise CannotConfirm(f"Delete request completed but floor {post.floor} is still present")
    body_text = page.locator("body").inner_text(timeout=timeout)
    if "此文章/討論串不存在或已被刪除" not in body_text:
        raise CannotConfirm(f"Could not verify deletion of floor {post.floor} from Bahamut's response")


def cleanup_previous_reply(page: Any, config: Config) -> bool:
    posts = collect_posts(page, config)
    candidates = deletion_candidates(posts, keep_latest_replies=1)
    if not candidates:
        LOG.info("No previous owned reply is available for cleanup")
        return False
    target = candidates[0]
    if bool(config.cleanup.get("dry_run", True)):
        LOG.info("Cleanup dry-run: would delete floor %s (sn=%s)", target.floor, target.sn)
        return False
    delete_post(page, config, target)
    verify_post_deleted(page, config, target)
    LOG.info("Deleted previous bump floor %s (sn=%s)", target.floor, target.sn)
    return True


def cleanup_thread(page: Any, config: Config, max_deletions: int = 0) -> int:
    posts = collect_posts(page, config)
    keep = int(config.cleanup.get("keep_latest_replies", 1))
    candidates = deletion_candidates(posts, keep_latest_replies=keep)
    if max_deletions > 0:
        candidates = candidates[:max_deletions]
    if not candidates:
        LOG.info("No cleanup candidates; floor 1 and the newest %s owned replies are retained", keep)
        return 0
    dry_run = bool(config.cleanup.get("dry_run", True))
    interval = max(5, int(config.cleanup.get("interval_seconds", 5)))
    deleted = 0
    for index, target in enumerate(candidates):
        if dry_run:
            LOG.info("Cleanup dry-run: would delete floor %s (sn=%s)", target.floor, target.sn)
        else:
            delete_post(page, config, target)
            verify_post_deleted(page, config, target)
            deleted += 1
            LOG.info("Deleted floor %s (sn=%s)", target.floor, target.sn)
        if not dry_run and index + 1 < len(candidates):
            time.sleep(interval)
    return deleted


def _first_visible(page: Any, selectors: str):
    for selector in selectors.split(","):
        locator = page.locator(selector.strip()).first
        if locator.count() and locator.is_visible():
            return locator
    return None


def verify_imported_session(page: Any, config: Config) -> None:
    """Require an imported authenticated session; never enter credentials."""
    timeout = int(config.browser.get("navigation_timeout_ms", 30000))
    storage_state = str(config.browser.get("storage_state", "")).strip()
    if not storage_state:
        raise CannotConfirm("browser.storage_state is required; automatic account/password login is disabled")
    if not Path(storage_state).exists():
        raise CannotConfirm(f"Configured browser storage_state file does not exist: {storage_state}")
    page.goto(str(config.thread["url"]), wait_until="domcontentloaded")
    page.set_default_timeout(timeout)
    title = page.title()
    if title in {"請稍候...", "Just a moment..."} or "challenge" in title.lower():
        raise CannotConfirm(
            f"Bahamut anti-bot challenge blocked the imported session; url={page.url!r}, title={title!r}"
        )
    top_login = _first_visible(page, "#BH-top-data a[href*='login.php']")
    selector = str(config.selectors.get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    posts = page.locator(selector).count()
    if top_login or not posts:
        raise CannotConfirm(
            "Imported browser storage_state is expired or not authenticated; export a new session manually"
        )
    LOG.info("Using authenticated Playwright storage state: %s", storage_state)


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
    _navigate_to_last_page(page, config)
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
        page.evaluate(
            """
            ({editorSelector, value}) => {
              const iframe = document.querySelector(editorSelector);
              const doc = iframe && iframe.contentDocument;
              const edit = doc && (doc.getElementsByClassName('editstyle')[0] || doc.body);
              if (!edit) throw new Error('Bahamut iframe editor is not ready');
              edit.replaceChildren();
              value.split('\\n').forEach((line, index) => {
                if (index) edit.appendChild(doc.createElement('br'));
                edit.appendChild(doc.createTextNode(line));
              });
              edit.dispatchEvent(new Event('input', {bubbles: true}));
              edit.dispatchEvent(new Event('change', {bubbles: true}));
            }
            """,
            {"editorSelector": editor_selector, "value": body},
        )
        # The quick-reply controls are injected only after authentication. When
        # their markup changes, submit the same form used by Bahamut's quickPost.
        page.evaluate(
            """
            (editorSelector) => {
              const iframe = document.querySelector(editorSelector);
              const doc = iframe && iframe.contentDocument;
              const edit = doc && (doc.getElementsByClassName('editstyle')[0] || doc.body);
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
        LOG.warning("Reply navigation timed out; checking from an independent page")
    verification_page = page.context.new_page()
    try:
        verification_page.goto(str(config.thread["url"]), wait_until="domcontentloaded")
        verification_page.set_default_timeout(int(config.browser.get("navigation_timeout_ms", 30000)))
        verification_page.locator(selector).first.wait_for(state="attached")
        _navigate_to_last_page(verification_page, config)
        verified = latest_post_timestamp(verification_page, config, now)
    finally:
        verification_page.close()
    if not is_today(verified, now):
        raise CannotConfirm("Reply was submitted but the latest post could not be verified as today")
    LOG.info("Bump submitted and verified at %s", verified.isoformat())
    if bool(config.cleanup.get("enabled", False)):
        delay = max(5, int(config.cleanup.get("after_bump_delay_seconds", 5)))
        LOG.info("Waiting %s seconds before cleaning the previous reply", delay)
        time.sleep(delay)
        cleanup_previous_reply(page, config)
    return "posted"


def run_once(config: Config) -> str:
    now = taiwan_now(config)
    LOG.info("Checking %s at %s", config.thread["url"], now.isoformat())
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError as exc:
        raise CannotConfirm("The Playwright Python package is required; run pip install -r requirements.txt") from exc
    with sync_playwright() as playwright:
        storage_state = str(config.browser.get("storage_state", "")).strip()
        if storage_state and not Path(storage_state).exists():
            raise CannotConfirm(f"Configured browser storage_state file does not exist: {storage_state}")
        launch_options = {
            "headless": bool(config.browser.get("headless", True)),
            "channel": str(config.browser.get("channel", "chrome")),
        }
        if config.browser.get("executable_path"):
            launch_options.pop("channel")
            launch_options["executable_path"] = str(config.browser["executable_path"])
        browser = None
        try:
            browser = playwright.chromium.launch(**launch_options)
            context_options = {"locale": "zh-TW", "timezone_id": config.schedule["timezone"]}
            if storage_state:
                context_options["storage_state"] = storage_state
            context = browser.new_context(**context_options)
            page = context.new_page()
            verify_imported_session(page, config)
            return inspect_and_maybe_bump(page, config, now)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "Failed to launch" in str(exc):
                raise CannotConfirm(
                    "Could not launch system Google Chrome; install google-chrome-stable or set [browser] executable_path"
                ) from exc
            raise
        finally:
            if browser is not None:
                browser.close()


def run_cleanup(config: Config, max_deletions: int = 0) -> int:
    """Run the explicit cleanup command in the same authenticated Chrome session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CannotConfirm("The Playwright Python package is required; run pip install -r requirements.txt") from exc
    with sync_playwright() as playwright:
        storage_state = str(config.browser.get("storage_state", "")).strip()
        if storage_state and not Path(storage_state).exists():
            raise CannotConfirm(f"Configured browser storage_state file does not exist: {storage_state}")
        launch_options = {
            "headless": bool(config.browser.get("headless", True)),
            "channel": str(config.browser.get("channel", "chrome")),
        }
        if config.browser.get("executable_path"):
            launch_options.pop("channel")
            launch_options["executable_path"] = str(config.browser["executable_path"])
        browser = None
        try:
            browser = playwright.chromium.launch(**launch_options)
            context_options = {"locale": "zh-TW", "timezone_id": config.schedule["timezone"]}
            if storage_state:
                context_options["storage_state"] = storage_state
            context = browser.new_context(**context_options)
            page = context.new_page()
            verify_imported_session(page, config)
            return cleanup_thread(page, config, max_deletions=max_deletions)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "Failed to launch" in str(exc):
                raise CannotConfirm(
                    "Could not launch system Google Chrome; install google-chrome-stable or set [browser] executable_path"
                ) from exc
            raise
        finally:
            if browser is not None:
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
    if any(word in message for word in ("login", "cookie", "session", "authenticated", "登入")):
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
    parser.add_argument("--cleanup", action="store_true", help="inspect and clean old owned replies")
    parser.add_argument("--apply", action="store_true", help="allow live deletion with --cleanup; otherwise dry-run")
    parser.add_argument("--cleanup-limit", type=int, default=0, help="maximum replies to delete during --cleanup (0 = all)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.cleanup:
            cleanup_values = dict(config.values.get("cleanup", {}))
            if args.apply:
                cleanup_values["dry_run"] = False
            cleanup_config = Config({**config.values, "cleanup": cleanup_values})
            deleted = run_cleanup(cleanup_config, max_deletions=max(0, args.cleanup_limit))
            LOG.info("Cleanup finished; deleted %s post(s)", deleted)
            return 0
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
