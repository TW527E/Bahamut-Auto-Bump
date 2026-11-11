#!/usr/bin/env python3
"""Safely bump one Bahamut thread once per Taiwan calendar day."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOG = logging.getLogger("bahamut-auto-bump")
DEFAULT_CONFIG = Path("config.toml")
DEFAULT_TEMPLATE = "頂🆙！\n（{timestamp}）"


class ConfigError(ValueError):
    pass


class CannotConfirm(RuntimeError):
    """Raised whenever the page state is not safe enough to make a decision."""


class RateLimited(CannotConfirm):
    """Bahamut kept answering with its HTTP 429 page; not an auth or layout problem."""


class AntiBotChallenge(CannotConfirm):
    """Cloudflare answered with its challenge page; headless Chrome cannot pass it."""


@dataclass(frozen=True)
class PostInfo:
    floor: int
    sn: str
    owner: bool
    raw_time: str
    bump: bool = False


def is_bump_text(text: str, template: str) -> bool:
    """Match a rendered post body against content_template, ignoring whitespace."""
    parts = ["".join(part.split()) for part in template.split("{timestamp}")]
    pattern = r"\d{4}-\d{2}-\d{2}\d{2}:\d{2}:\d{2}UTC\+8".join(map(re.escape, parts))
    return re.fullmatch(pattern, "".join(text.split())) is not None


def load_config(path: Path) -> dict[str, Any]:
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
    data.setdefault("cleanup", {})
    return data


def taiwan_now(config: dict[str, Any]) -> datetime:
    return datetime.now(ZoneInfo(config["schedule"]["timezone"]))


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
    value = value.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    value = re.sub(r"\s+", " ", value)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CannotConfirm(f"Unparseable post timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def is_today(parsed: datetime, now: datetime) -> bool:
    return parsed.astimezone(now.tzinfo).date() == now.date()


RATE_LIMIT_TITLE = "系統維修中"
_last_navigation = 0.0


def _jittered_seconds(base: float, jitter: float) -> float:
    """Base interval plus uniform jitter, so requests do not land on a fixed grid.

    A fixed 3.0-second gap between every page load is an easy bot tell; a
    3-5 second spread is not. Negative inputs are clamped, not rejected.
    """
    base = max(0.0, float(base))
    jitter = max(0.0, float(jitter))
    return base + (random.uniform(0.0, jitter) if jitter > 0 else 0.0)


def _pace(config: dict[str, Any]) -> None:
    """Keep requests to Bahamut at least navigation_interval_seconds apart."""
    global _last_navigation
    interval = _jittered_seconds(
        float(config["browser"].get("navigation_interval_seconds", 3)),
        float(config["browser"].get("navigation_jitter_seconds", 2)),
    )
    wait = _last_navigation + interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_navigation = time.monotonic()


def _rate_limited(page: Any, response: Any = None) -> bool:
    if response is not None and getattr(response, "status", None) == 429:
        return True
    try:
        return RATE_LIMIT_TITLE in page.title()
    except Exception:
        return False


# Cloudflare's challenge first renders Bahamut's "系統異常回報" page, then its
# own "請稍候..." page once the challenge script loads.
CHALLENGE_TITLES = ("系統異常回報", "請稍候...", "Just a moment...")


def _challenged(page: Any, response: Any = None) -> bool:
    headers = getattr(response, "headers", None) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    try:
        title = page.title()
    except Exception:
        return False
    return any(marker in title for marker in CHALLENGE_TITLES) or "challenge" in title.lower()


def _challenge_error(page: Any, where: str) -> AntiBotChallenge:
    try:
        title = page.title()
    except Exception:
        title = ""
    return AntiBotChallenge(
        f"Bahamut anti-bot challenge (Cloudflare) blocked {where}; the IP Chrome browses from is being challenged; "
        f"title={title!r}"
    )


def _goto(page: Any, url: str, config: dict[str, Any]) -> Any:
    """Navigate politely, wait out Bahamut's burst rate limit and fail on a challenge.

    Bahamut answers request bursts with an HTTP 429 "系統維修中" page that has
    no posts or site messages. Without this, a full thread scan surfaces the
    limit as missing posts, failed login or unverifiable deletions.
    """
    backoff = max(1, int(config["browser"].get("rate_limit_wait_seconds", 60)))
    retries = max(0, int(config["browser"].get("rate_limit_retries", 3)))
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or url
    for attempt in range(retries + 1):
        _pace(config)
        response = page.goto(url, wait_until="domcontentloaded")
        if _challenged(page, response):
            raise _challenge_error(page, f"{parsed.netloc}{path}")
        if not _rate_limited(page, response):
            return response
        if attempt < retries:
            LOG.warning("Bahamut rate limited %s; waiting %s seconds", path, backoff * (attempt + 1))
            time.sleep(backoff * (attempt + 1))
    raise RateLimited(f"Bahamut rate limit (HTTP 429 {RATE_LIMIT_TITLE}) persisted for {path}")


def _navigate_to_last_page(page: Any, config: dict[str, Any]) -> None:
    """Jump to the newest thread page, staying put when no later page is advertised."""
    current = urllib.parse.urlparse(str(page.url))
    current_page = int(urllib.parse.parse_qs(current.query).get("page", ["1"])[0])
    urls = _thread_page_urls(page, config)
    if len(urls) <= current_page:
        return
    _goto(page, urls[-1], config)
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    try:
        page.locator(selector).first.wait_for(
            state="attached", timeout=int(config["browser"].get("navigation_timeout_ms", 30000))
        )
    except Exception as exc:
        raise CannotConfirm("The last thread page did not load its posts in time") from exc


def latest_post_timestamp(page: Any, config: dict[str, Any], now: datetime) -> datetime:
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    time_selector = str(config["selectors"].get("post_time_selector", "time[datetime], .edittime, .c-post__header time"))
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


def _thread_page_urls(page: Any, config: dict[str, Any]) -> list[str]:
    """Return every numbered page URL advertised by the thread pagination."""
    current = urllib.parse.urlparse(str(page.url))
    current_query = urllib.parse.parse_qs(current.query)
    try:
        hrefs = page.locator("a[href*='page=']").evaluate_all(
            "links => links.map((link) => link.href).filter(Boolean)"
        )
    except Exception as exc:
        raise CannotConfirm(f"Could not inspect thread pagination: {exc}") from exc
    # Bahamut's pagination does not link the page being viewed, so a scan
    # started from the last page would otherwise silently skip that page.
    pages = {1, int(current_query.get("page", ["1"])[0])}
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


def _read_current_page_posts(page: Any, config: dict[str, Any], *, first_page: bool = False) -> list[PostInfo]:
    """Read the posts on the currently loaded thread page.

    Returns an empty list for a confirmed empty page (Bahamut can retain
    sparse page numbers after mass deletion); raises CannotConfirm for
    anything that is not a readable thread page.
    """
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    time_selector = str(config["selectors"].get("post_time_selector", ".edittime"))
    content_selector = str(config["selectors"].get("post_content_selector", ".c-article__content"))
    template = str(config["thread"].get("content_template", DEFAULT_TEMPLATE))
    try:
        posts_locator = page.locator(selector)
        if posts_locator.count() == 0:
            # After mass deletion Bahamut can retain sparse page numbers:
            # page 2 may be empty while pages 14 and 16 still contain old
            # replies. Skip a confirmed empty thread page and keep scanning.
            title = page.title()
            if _challenged(page):
                raise _challenge_error(page, "the thread scan")
            if page.locator("#BH-master").count() == 0:
                raise CannotConfirm(f"Thread page layout is missing at {page.url}; title={title!r}")
            page_number = urllib.parse.parse_qs(urllib.parse.urlparse(str(page.url)).query).get("page", ["?"])[0]
            LOG.info("Thread page %s contains no posts after cleanup; continuing scan", page_number)
            return []
        posts_locator.first.wait_for(state="attached", timeout=timeout)
        rows = page.locator("section[id^='post_']").evaluate_all(
            """
            (sections, {timeSelector, contentSelector}) => sections.map((section) => {
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
                text: section.querySelector(contentSelector)?.innerText || ''
              };
            }).filter(item => item.sn)
            """,
            {"timeSelector": time_selector, "contentSelector": content_selector},
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
    result: list[PostInfo] = []
    for row in rows:
        try:
            floor = int(row["floor"] or (1 if first_page and not result else 0))
        except (TypeError, ValueError):
            floor = 0
        if floor <= 0:
            raise CannotConfirm(f"Could not determine floor for post {row.get('sn')}")
        if not row.get("raw"):
            raise CannotConfirm(f"Post {row.get('sn')} has no readable timestamp")
        if not re.fullmatch(r"[0-9]+", str(row["sn"])) or int(row["sn"]) <= 0:
            raise CannotConfirm("Could not determine a valid post number")
        result.append(PostInfo(
            floor, str(row["sn"]), bool(row["owner"]), str(row["raw"]), is_bump_text(str(row["text"]), template),
        ))
    return result


def _should_skip_page(index: int, last_number: int, seen: set[int], skip_seen: bool) -> bool:
    """True when a page was already read and is not needed again.

    The last page is never skipped: new replies always land there. Any other
    page is frozen unless a post on it is deleted, and deletions are tracked
    by post number (sn), not by page, so skipping a seen page cannot change
    any decision the daily check or the bump verification makes.
    """
    return bool(skip_seen and index != last_number and index in seen)


def _thread_page_url(config: dict[str, Any], number: int) -> str:
    """Thread URL for one page number."""
    parsed = urllib.parse.urlparse(str(config["thread"]["url"]))
    query = urllib.parse.parse_qs(parsed.query)
    query["page"] = [str(number)]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _reload_last_page(page: Any, config: dict[str, Any], state: dict[str, Any]) -> list[PostInfo]:
    """Force a fresh load of the last thread page and return its posts.

    Used for the re-check right before submitting, so a bump posted by
    someone else in the meantime is not duplicated. The cached last page
    number may be stale when the thread grew, so the pagination on the
    loaded page is checked and the true last page is loaded instead.
    """
    try:
        number = max(1, int(state.get("last_page") or 1))
    except (TypeError, ValueError):
        number = 1
    _goto(page, _thread_page_url(config, number), config)
    posts = _read_current_page_posts(page, config, first_page=(number == 1))
    actual_last = len(_thread_page_urls(page, config))
    if actual_last != number:
        number = actual_last
        _goto(page, _thread_page_url(config, number), config)
        posts = _read_current_page_posts(page, config, first_page=(number == 1))
    if not posts:
        raise CannotConfirm("The last thread page contains no posts")
    seen = {int(n) for n in (state.get("seen_pages") or []) if str(n).isdigit()}
    seen.add(number)
    state["seen_pages"] = sorted(seen)
    state["last_page"] = number
    return sorted(posts, key=lambda post: post.floor)


def collect_posts(page: Any, config: dict[str, Any], state: dict[str, Any] | None = None, *, skip_seen: bool = False) -> list[PostInfo]:
    """Load thread pages and collect only data needed for safe cleanup.

    With skip_seen, pages already recorded in state["seen_pages"] are not
    loaded again, except the last page, which is always re-read. That is what
    the daily check and the bump verification use: they only ever need the
    newest page, so a routine check costs 1-2 page loads instead of a full
    thread scan. A full inventory build passes skip_seen=False so every page
    is read once and recorded. `state` may be None for a one-off scan.
    """
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    page.set_default_timeout(timeout)
    try:
        configured = urllib.parse.urlparse(str(config["thread"]["url"]))
        current = urllib.parse.urlparse(str(page.url))
        configured_query = urllib.parse.parse_qs(configured.query)
        current_query = urllib.parse.parse_qs(current.query)
        same_thread = (
            current.path == configured.path
            and current_query.get("bsn") == configured_query.get("bsn")
            and current_query.get("snA") == configured_query.get("snA")
        )
        if not same_thread or page.locator(selector).count() == 0:
            _goto(page, str(config["thread"]["url"]), config)
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

    last_number = len(page_urls)
    seen: set[int] = set()
    if state is not None:
        seen = {int(n) for n in (state.get("seen_pages") or []) if str(n).isdigit()}
    result: list[PostInfo] = []
    for index, page_url in enumerate(page_urls, start=1):
        if _should_skip_page(index, last_number, seen, skip_seen):
            LOG.info("Skipping already-seen thread page %s", index)
            continue
        if str(page.url) != page_url:
            _goto(page, page_url, config)
        page_posts = _read_current_page_posts(page, config, first_page=(index == 1))
        if state is not None and page_posts:
            seen.add(index)
        result.extend(page_posts)
    if state is not None:
        state["seen_pages"] = sorted(seen)
        state["last_page"] = last_number
    if not result:
        raise CannotConfirm("No posts were found for cleanup")
    return sorted({post.sn: post for post in result}.values(), key=lambda post: post.floor)


def deletion_candidates(posts: list[PostInfo], keep_latest_replies: int = 1) -> list[PostInfo]:
    """Choose owned bump replies older than the retained newest bumps; floor 1 is immutable.

    Only replies matching content_template are considered, so the owner's
    other replies in the thread are never deleted.

    Bahamut may leave non-contiguous/reused-looking floor labels after deletes.
    The post number (sn) is monotonic across the whole thread, so it is the
    stable ordering key and cannot mistake a page's last visible post for the
    thread's latest reply.
    """
    keep = max(0, int(keep_latest_replies))
    def order_key(post: PostInfo) -> tuple[int, int]:
        try:
            sn = int(post.sn)
        except (TypeError, ValueError):
            sn = -1
        return sn, post.floor

    replies = sorted((post for post in posts if post.floor > 1 and post.owner and post.bump), key=order_key, reverse=True)
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


def delete_post(page: Any, config: dict[str, Any], post: PostInfo) -> None:
    # pdel is generated on thread pages and contains the current delete token.
    _goto(page, str(config["thread"]["url"]), config)
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    page.locator(selector).first.wait_for(
        state="attached", timeout=int(config["browser"].get("navigation_timeout_ms", 30000))
    )
    request = _delete_request(page, post.sn)
    if request["cookie"]:
        page.evaluate(
            "cookie => { if (typeof window.setCookie === 'function') window.setCookie('ckFORUM_pdel', cookie); }",
            request["cookie"],
        )
    endpoint = urllib.parse.urljoin(str(page.url), "post2.php?" + request["args"])
    try:
        _goto(page, endpoint, config)
    except Exception as exc:
        # Bahamut may redirect while Playwright is waiting for DOMContentLoaded;
        # the caller always verifies the post is gone before treating this as success.
        LOG.warning("Delete navigation ended early; verifying the post: %s", exc)


def verify_post_deleted(page: Any, config: dict[str, Any], post: PostInfo) -> None:
    """Verify one deletion through Bahamut's canonical single-post URL."""
    thread = urllib.parse.urlparse(str(config["thread"]["url"]))
    query = urllib.parse.parse_qs(thread.query)
    bsn = query.get("bsn", [""])[0]
    if not bsn:
        raise CannotConfirm("Thread URL has no bsn for deletion verification")
    verification_url = urllib.parse.urljoin(
        str(config["thread"]["url"]),
        "Co.php?" + urllib.parse.urlencode({"bsn": bsn, "sn": post.sn}),
    )
    _goto(page, verification_url, config)
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    page.locator("body").wait_for(state="attached", timeout=timeout)
    if page.locator(f"#post_{post.sn}").count():
        raise CannotConfirm(f"Delete request completed but floor {post.floor} is still present")
    body_text = page.locator("body").inner_text(timeout=timeout)
    if "此文章/討論串不存在或已被刪除" not in body_text:
        raise CannotConfirm(
            f"Could not verify deletion of floor {post.floor} from Bahamut's response; title={page.title()!r}"
        )


def _previous_page_posts(page: Any, config: dict[str, Any], state: dict[str, Any]) -> list[PostInfo]:
    """Posts on the page before the recorded last page, or none for a one-page thread."""
    try:
        number = int(state.get("last_page") or 0) - 1
    except (TypeError, ValueError):
        return []
    if number < 1:
        return []
    _goto(page, _thread_page_url(config, number), config)
    return _read_current_page_posts(page, config, first_page=(number == 1))


def finish_pending_bump(
    page: Any, config: dict[str, Any], state: dict[str, Any], posts: list[PostInfo], now: datetime,
) -> bool:
    pending = state["pending_bump"]
    try:
        if not isinstance(pending, dict) or pending["thread_url"] != str(config["thread"]["url"]):
            raise ValueError("Pending bump belongs to a different thread")
        before_sn = pending["before_sn"]
        if type(before_sn) is not int or before_sn <= 0:
            raise ValueError("Invalid pre-bump post number")
        if "posted_sn" in pending and date.fromisoformat(pending["posted_date"]) > now.date():
            raise ValueError("Confirmed bump date is in the future")
    except (KeyError, TypeError, ValueError) as exc:
        raise CannotConfirm(f"Invalid pending bump state: {exc}") from exc

    if "posted_sn" not in pending:
        replies = [post for post in posts if post.owner and post.floor > 1 and int(post.sn) > before_sn]
        if not replies:
            # The new reply may have been pushed off the last page by later
            # replies; read the page before it before concluding it is missing.
            # Not a full scan: a submission that did not land (an expired
            # session, say) ends here on every retry.
            posts = posts + _previous_page_posts(page, config, state)
            replies = [post for post in posts if post.owner and post.floor > 1 and int(post.sn) > before_sn]
            if not replies:
                return False
        posted = min(replies, key=lambda post: int(post.sn))
        posted_at = parse_post_time(posted.raw_time, now.tzinfo, now)
        if posted_at.date() > now.date():
            raise CannotConfirm("New reply has a future timestamp")
        pending["posted_sn"] = posted.sn
        pending["posted_date"] = posted_at.date().isoformat()
        write_state(state_path(config), state)
        LOG.info("Confirmed submitted bump sn=%s at %s", posted.sn, posted_at.isoformat())
        if posted.bump and state.get("bump_inventory") is not None:
            inventory = state["bump_inventory"]
            if not any(str(entry.get("sn")) == posted.sn for entry in inventory):
                inventory.append({"sn": posted.sn, "floor": posted.floor, "date": posted_at.date().isoformat()})
                write_state(state_path(config), state)

    # Clean every older bump, not just the previous one, so a missed day is
    # caught up on the next bump instead of being left behind forever.
    unrecognized = None
    if config["cleanup"].get("enabled", False):
        posted = next((post for post in posts if post.sn == pending["posted_sn"]), None)
        if posted is not None and not posted.bump:
            unrecognized = posted.sn
        else:
            delay = max(5, int(config["cleanup"].get("after_bump_delay_seconds", 5)))
            LOG.info("Waiting %s seconds before cleaning up older bumps", delay)
            time.sleep(delay)
            _ensure_inventory(page, config, state)
            cleanup_inventory(page, config, state)
    state["last_confirmed_date"] = pending["posted_date"]
    del state["pending_bump"]
    write_state(state_path(config), state)
    if unrecognized:
        # The bump itself succeeded; fail loudly so a markup change cannot
        # silently stop cleanup again.
        raise CannotConfirm(
            f"Bump sn={unrecognized} does not match content_template; older bumps were not cleaned up "
            "(check selectors.post_content_selector)"
        )
    return True


def cleanup_thread(page: Any, config: dict[str, Any], posts: list[PostInfo], max_deletions: int = 0) -> int:
    # Keeping at least one bump stops cleanup from deleting today's bump and
    # triggering a re-bump on the next check.
    keep = max(1, int(config["cleanup"].get("keep_latest_replies", 1)))
    candidates = deletion_candidates(posts, keep_latest_replies=keep)
    if max_deletions > 0:
        candidates = candidates[:max_deletions]
    if not candidates:
        LOG.info("No cleanup candidates; floor 1 and the newest %s bump(s) are retained", keep)
        return 0
    dry_run = bool(config["cleanup"].get("dry_run", True))
    interval = max(5, int(config["cleanup"].get("interval_seconds", 5)))
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


def _inventory_from_posts(posts: list[PostInfo], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Own template-matching bump replies as a durable inventory.

    Only replies matching content_template are recorded, so the owner's other
    replies in the thread are never deleted. The post number (sn) is the
    stable key; floor labels shift after deletions.
    """
    try:
        tzinfo = ZoneInfo(str(config["schedule"].get("timezone", "Asia/Taipei")))
    except Exception:
        tzinfo = ZoneInfo("Asia/Taipei")
    entries: list[dict[str, Any]] = []
    for post in posts:
        if post.floor <= 1 or not post.owner or not post.bump:
            continue
        try:
            posted_date = parse_post_time(post.raw_time, tzinfo).date().isoformat()
        except CannotConfirm:
            posted_date = ""
        entries.append({"sn": str(post.sn), "floor": post.floor, "date": posted_date})
    return sorted(entries, key=lambda entry: int(entry["sn"]))


def _inventory_candidates(inventory: list[dict[str, Any]], keep_latest_replies: int = 1) -> list[dict[str, Any]]:
    """Inventory bumps older than the retained newest ones, newest sn first."""
    keep = max(0, int(keep_latest_replies))
    ordered = sorted(
        (entry for entry in inventory if str(entry.get("sn", "")).isdigit()),
        key=lambda entry: int(entry["sn"]),
        reverse=True,
    )
    return ordered[keep:]


def _ensure_inventory(page: Any, config: dict[str, Any], state: dict[str, Any]) -> None:
    """Build the bump inventory with one full scan, the first time it is needed.

    After that the daily flow never scans old pages again: the inventory is
    maintained from verified bumps and deletions. A missing key means "not
    built yet"; an empty list means "built, no bumps found".
    """
    if state.get("bump_inventory") is not None:
        return
    LOG.info("Building the bump inventory with one full thread scan")
    posts = collect_posts(page, config, state, skip_seen=False)
    state["bump_inventory"] = _inventory_from_posts(posts, config)
    write_state(state_path(config), state)


def cleanup_inventory(page: Any, config: dict[str, Any], state: dict[str, Any], max_deletions: int = 0) -> int:
    """Delete older owned bumps from the recorded inventory, without scanning.

    The inventory lists the bot's own template-matching bumps by post number
    (sn); the newest `keep_latest_replies` are retained and the rest are
    deleted and verified one by one. A post that is already gone verifies as
    deleted and is simply dropped from the inventory.
    """
    keep = max(1, int(config["cleanup"].get("keep_latest_replies", 1)))
    candidates = _inventory_candidates(state.get("bump_inventory") or [], keep)
    if max_deletions > 0:
        candidates = candidates[:max_deletions]
    if not candidates:
        LOG.info("No cleanup candidates; floor 1 and the newest %s bump(s) are retained", keep)
        return 0
    dry_run = bool(config["cleanup"].get("dry_run", True))
    interval = max(5, int(config["cleanup"].get("interval_seconds", 5)))
    jitter = float(config["browser"].get("navigation_jitter_seconds", 2))
    deleted = 0
    for index, target in enumerate(candidates):
        sn = str(target["sn"])
        floor = int(target.get("floor", 0))
        if dry_run:
            LOG.info("Cleanup dry-run: would delete bump sn=%s (floor %s)", sn, floor)
        else:
            post = PostInfo(floor, sn, True, "")
            delete_post(page, config, post)
            verify_post_deleted(page, config, post)
            deleted += 1
            LOG.info("Deleted bump sn=%s (floor %s)", sn, floor)
            state["bump_inventory"] = [
                entry for entry in (state.get("bump_inventory") or []) if str(entry.get("sn")) != sn
            ]
            write_state(state_path(config), state)
        if not dry_run and index + 1 < len(candidates):
            time.sleep(_jittered_seconds(interval, jitter))
    return deleted


def _first_visible(page: Any, selectors: str):
    for selector in selectors.split(","):
        selector = selector.strip()
        if not selector:
            continue
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _authenticated_thread_page(page: Any, config: dict[str, Any]) -> None:
    """Navigate to the thread and fail closed unless the account is signed in."""
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    _goto(page, str(config["thread"]["url"]), config)
    page.set_default_timeout(timeout)
    top_login = _first_visible(
        page,
        str(config["selectors"].get(
            "logged_out_selector",
            "#BH-top-data a[href*='login.php'], a.main-nav__link[onclick*='requireLoginIframe'], a[onclick*='requireLoginIframe']",
        )),
    )
    selector = str(config["selectors"].get("post_selector", "#BH-master > section[id^='post_'] .c-post"))
    posts = page.locator(selector).count()
    if top_login or not posts:
        raise CannotConfirm(
            "Bahamut session is expired or not authenticated"
        )


def verify_imported_session(page: Any, config: dict[str, Any]) -> None:
    """Verify an imported storage state without attempting an automatic login."""
    storage_state = str(config["browser"].get("storage_state", "")).strip()
    if not storage_state:
        raise CannotConfirm("browser.storage_state is not configured")
    if not Path(storage_state).exists():
        raise CannotConfirm(f"Configured browser storage_state file does not exist: {storage_state}")
    try:
        _authenticated_thread_page(page, config)
    except (RateLimited, AntiBotChallenge):
        raise
    except CannotConfirm as exc:
        raise CannotConfirm(
            f"Imported browser storage_state is expired or not authenticated: {exc}"
        ) from exc
    except Exception as exc:
        raise CannotConfirm(f"Could not verify imported browser storage_state: {exc}") from exc
    LOG.info("Using authenticated Playwright storage state: %s", storage_state)
    _save_session(page, config)


def profile_dir(config: dict[str, Any]) -> str:
    """[browser] user_data_dir: a Chrome profile kept between runs, or "" for none."""
    return str(config["browser"].get("user_data_dir", "")).strip()


PROFILE_REJECTED_MARKER = "bahamut-session-rejected"


def _session_file_stamp(config: dict[str, Any]) -> tuple[Path, str] | None:
    """Marker path and current version stamp of the storage_state file, if both apply."""
    storage_state = str(config["browser"].get("storage_state", "")).strip()
    profile = profile_dir(config)
    if not storage_state or not profile:
        return None
    try:
        stamp = str(Path(storage_state).stat().st_mtime_ns)
    except OSError:
        return None
    return Path(profile) / PROFILE_REJECTED_MARKER, stamp


def _import_session_file(page: Any, config: dict[str, Any]) -> bool:
    """Load the storage_state file's cookies into the signed-out Chrome profile.

    Chrome drops session cookies (no expiry) when it closes, so a profile can
    come back signed out while the file still holds a usable login; the file
    is therefore imported again on every such run. Only a file version that
    was imported and still left the profile signed out is skipped.
    Returns True when cookies were added and the session is worth re-checking.
    """
    found = _session_file_stamp(config)
    if not found:
        return False
    marker, stamp = found
    storage_state = str(config["browser"]["storage_state"]).strip()
    try:
        if marker.read_text(encoding="utf-8").strip() == stamp:
            return False
    except OSError:
        pass
    try:
        cookies = json.loads(Path(storage_state).read_text(encoding="utf-8"))["cookies"]
        page.context.add_cookies(cookies)
    except Exception as exc:
        LOG.warning("Could not import %s into the Chrome profile: %s", storage_state, exc)
        return False
    LOG.info("Imported cookies from %s into the Chrome profile", storage_state)
    return True


def _reject_session_file(config: dict[str, Any]) -> None:
    """Remember that this version of the storage_state file does not sign in."""
    found = _session_file_stamp(config)
    if not found:
        return
    marker, stamp = found
    try:
        marker.write_text(stamp, encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not record the rejected session in %s: %s", marker, exc)


def verify_profile_session(page: Any, config: dict[str, Any]) -> None:
    """Verify the Chrome profile is signed in, importing the storage_state file if it is not.

    A verified session is written back to the file, so cookies Chrome will
    drop on exit can be restored on the next run.
    """
    try:
        _authenticated_thread_page(page, config)
    except (RateLimited, AntiBotChallenge):
        raise
    except CannotConfirm:
        if not _import_session_file(page, config):
            raise
        try:
            _authenticated_thread_page(page, config)
        except (RateLimited, AntiBotChallenge):
            raise
        except CannotConfirm:
            _reject_session_file(config)
            raise
    LOG.info("Using signed-in Chrome profile: %s", profile_dir(config))
    _save_session(page, config)


def _save_session(page: Any, config: dict[str, Any]) -> None:
    """Write the context's current cookies back to storage_state.

    Bahamut rotates its login cookies as the account browses. A file saved only
    after a homepage login drifts behind them and stopped being accepted within
    about a day, forcing a homepage login that Cloudflare may block.
    """
    storage_state = str(config["browser"].get("storage_state", "")).strip()
    if not storage_state:
        return
    path = Path(storage_state)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = json.dumps(page.context.storage_state(), ensure_ascii=False)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
            output.write(state)
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as exc:
        LOG.warning("Could not save Playwright storage state %s: %s", storage_state, exc)
        return
    LOG.info("Saved authenticated Playwright storage state: %s", storage_state)


def _login_frame(page: Any, config: dict[str, Any]):
    selector = str(config["selectors"].get("login_frame", "iframe[src*='login.php'], dialog iframe"))
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    try:
        page.locator(selector).first.wait_for(state="attached", timeout=timeout)
    except Exception as exc:
        raise CannotConfirm("Homepage login dialog did not render its login form") from exc
    return page.frame_locator(selector)


def _dismiss_homepage_onboarding(page: Any, config: dict[str, Any]) -> None:
    """Close the first-visit driver hint when it blocks the homepage login link."""
    prompt_selector = str(config["selectors"].get("login_onboarding", "#driver-popover-content"))
    prompt = _first_visible(page, prompt_selector)
    if not prompt:
        return
    close_selector = str(config["selectors"].get(
        "login_onboarding_close",
        "#driver-popover-content button[aria-label='Close'], #driver-popover-content .driver-popover-close-btn, #driver-popover-content button",
    ))
    close = _first_visible(page, close_selector)
    if not close:
        raise CannotConfirm("Homepage login onboarding prompt is visible but its close button was not found")
    try:
        close.click()
        prompt.wait_for(state="hidden", timeout=min(int(config["browser"].get("navigation_timeout_ms", 30000)), 5000))
    except Exception as exc:
        raise CannotConfirm("Could not dismiss the homepage login onboarding prompt") from exc


def _enter_from_homepage_cover(page: Any, config: dict[str, Any]) -> None:
    """Follow the full-page cover ad's "進入巴哈姆特" link to the real homepage.

    On cover days www.gamer.com.tw/ only shows an ad iframe and this link; the
    navigation bar with the login trigger lives on the index.php it points to.
    """
    selector = str(config["selectors"].get("homepage_cover_enter", ".sky a[href*='index.php']"))
    link = _first_visible(page, selector)
    if not link:
        return
    target = urllib.parse.urlparse(str(link.evaluate("element => element.href")))
    if target.scheme != "https" or target.netloc not in {"www.gamer.com.tw", "gamer.com.tw"}:
        raise CannotConfirm("Homepage cover link does not lead to the Bahamut homepage")
    LOG.info("Homepage is showing a cover ad; entering the homepage through its link")
    _goto(page, target.geturl(), config)


def _account_credentials(config: dict[str, Any]) -> tuple[str, str, str]:
    account = config.get("account") or {}
    if not isinstance(account, dict):
        raise ConfigError("[account] must be a table when provided")
    username = str(account.get("username", "")).strip()
    password = str(account.get("password", ""))
    totp_secret = str(account.get("totp_secret", "")).strip()
    if not username or not password:
        raise CannotConfirm(
            "No valid session is available; configure [account] username and password for homepage login"
        )
    return username, password, totp_secret


def _totp_code(secret: str) -> str:
    if not secret:
        return ""
    try:
        import pyotp
    except ImportError as exc:
        raise CannotConfirm('TOTP is configured but pyotp is not installed; run pip install "pyotp>=2.9,<3"') from exc
    try:
        return pyotp.TOTP(secret.replace(" ", "")).now()
    except Exception as exc:
        raise CannotConfirm("Configured TOTP secret is invalid") from exc


def login_from_homepage(page: Any, config: dict[str, Any]) -> None:
    """Open the public homepage, click its login link, then submit the iframe form."""
    username, password, totp_secret = _account_credentials(config)
    timeout = int(config["browser"].get("navigation_timeout_ms", 30000))
    page.set_default_timeout(timeout)
    homepage = str(config["browser"].get("homepage_url", "https://www.gamer.com.tw/"))
    parsed_homepage = urllib.parse.urlparse(homepage)
    if (
        parsed_homepage.scheme != "https"
        or parsed_homepage.netloc not in {"www.gamer.com.tw", "gamer.com.tw"}
        or parsed_homepage.path not in {"", "/"}
    ):
        raise CannotConfirm("Automatic login homepage_url must be https://www.gamer.com.tw/")
    _goto(page, homepage, config)
    try:
        _enter_from_homepage_cover(page, config)
        _dismiss_homepage_onboarding(page, config)
        trigger_selector = str(config["selectors"].get(
            "login_trigger",
            "a.main-nav__link[onclick*='requireLoginIframe'], a[onclick*='requireLoginIframe'], a.main-nav__link:has-text('登入'), a:has-text('登入')",
        ))
        if ":has-text('登入')" not in trigger_selector:
            trigger_selector += ", a.main-nav__link:has-text('登入'), a:has-text('登入')"
        trigger = page.locator(trigger_selector).first
        try:
            trigger.wait_for(state="visible", timeout=min(timeout, 10000))
        except Exception:
            trigger = _first_visible(page, trigger_selector)
        if not trigger:
            raise CannotConfirm("Homepage login button layout is not recognized")
        trigger.click()
        frame = _login_frame(page, config)
    except CannotConfirm:
        raise
    except Exception as exc:
        raise CannotConfirm("Could not open the homepage login dialog") from exc

    username_selector = str(config["selectors"].get("username", "#form-login input[name='userid'], input[name='userid']"))
    password_selector = str(config["selectors"].get("password", "#form-login input[name='password'], input[name='password']"))
    submit_selector = str(config["selectors"].get("login_submit", "#form-login button[type='submit'], #form-login input[type='submit'], #btn-login"))
    totp_selector = str(config["selectors"].get("totp", "#input-2sa, input[name='twoStepAuth'], input[name='otp'], input[name='code']"))
    try:
        user_field = frame.locator(username_selector).first
        pass_field = frame.locator(password_selector).first
        user_field.wait_for(state="visible", timeout=timeout)
        pass_field.wait_for(state="visible", timeout=timeout)
        user_field.fill(username)
        pass_field.fill(password)
        totp_field = frame.locator(totp_selector).first
        if totp_secret and totp_field.count() and totp_field.is_visible():
            totp_field.fill(_totp_code(totp_secret))
        frame.locator(submit_selector).first.click()
    except Exception as exc:
        raise CannotConfirm("Homepage login form layout is not recognized or could not be submitted") from exc

    page.wait_for_timeout(1000)
    try:
        _authenticated_thread_page(page, config)
    except (RateLimited, AntiBotChallenge):
        raise
    except CannotConfirm as exc:
        raise CannotConfirm(f"Automatic homepage login failed: {exc}") from exc
    _save_session(page, config)


LOGIN_PAUSE_KEY = "login_pause"


def _read_login_pause(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        pause = read_state(state_path(config)).get(LOGIN_PAUSE_KEY)
    except CannotConfirm:
        return None
    return pause if isinstance(pause, dict) else None


def _write_login_pause(config: dict[str, Any], pause: dict[str, Any] | None) -> None:
    path = state_path(config)
    try:
        state = read_state(path)
        if pause is None:
            if LOGIN_PAUSE_KEY not in state:
                return
            state.pop(LOGIN_PAUSE_KEY)
        else:
            state[LOGIN_PAUSE_KEY] = pause
        write_state(path, state)
    except (CannotConfirm, OSError) as exc:
        LOG.warning("Could not save the homepage login pause to %s: %s", path, exc)


def login_pause_seconds(config: dict[str, Any], strikes: int) -> int:
    """Pause length after the given number of challenged logins in a row.

    The first challenge pauses for challenge_login_pause_seconds and every
    further one doubles it, up to challenge_login_pause_max_seconds. A flagged
    IP stays flagged for days, so a fixed pause just meets the same challenge
    every few hours.
    """
    base = max(0, int(config["browser"].get("challenge_login_pause_seconds", 21600)))
    cap = max(base, int(config["browser"].get("challenge_login_pause_max_seconds", 172800)))
    return min(cap, base * 2 ** min(max(strikes, 1) - 1, 20))


def authenticate(page: Any, config: dict[str, Any]) -> None:
    """Use a valid session first, then fall back to the homepage login flow.

    Once Cloudflare challenges the homepage login, retrying it every
    retry_interval_seconds only keeps the IP flagged, so the login is paused
    (see login_pause_seconds). The pause is kept in state.json so a service
    restart does not retry right away. The session is still checked on every
    retry, so a fresh one uploaded with /session is used right away.
    """
    storage_state = str(config["browser"].get("storage_state", "")).strip()
    profile = profile_dir(config)
    if profile:
        try:
            verify_profile_session(page, config)
            return
        except (RateLimited, AntiBotChallenge):
            raise
        except CannotConfirm as exc:
            LOG.warning("Chrome profile is not signed in; attempting homepage login: %s", exc)
    elif storage_state and Path(storage_state).exists():
        try:
            verify_imported_session(page, config)
            return
        except (RateLimited, AntiBotChallenge):
            raise
        except CannotConfirm as exc:
            LOG.warning("Imported session unavailable; attempting homepage login: %s", exc)
    pause = _read_login_pause(config)
    now = time.time()
    if pause and now < float(pause.get("until", 0)):
        raise AntiBotChallenge(str(pause.get("message") or "Homepage login is paused after a Cloudflare challenge"))
    try:
        login_from_homepage(page, config)
    except AntiBotChallenge as exc:
        strikes = int(pause.get("strikes", 0)) + 1 if pause else 1
        seconds = login_pause_seconds(config, strikes)
        until = now + seconds
        zone = ZoneInfo(str((config.get("schedule") or {}).get("timezone", "Asia/Taipei")))
        resume = datetime.fromtimestamp(until, zone).strftime("%m-%d %H:%M")
        message = (
            f"{exc}; challenged {strikes} time(s) in a row, homepage login paused for {seconds} seconds "
            f"(until {resume}); upload a fresh session with /session"
            + (" or run --manual-login on the service host" if profile else "")
        )
        _write_login_pause(config, {"until": until, "strikes": strikes, "message": message})
        raise AntiBotChallenge(message) from exc
    _write_login_pause(config, None)


def _new_context(browser: Any, config: dict[str, Any], *, use_session: bool = True):
    """Create a context, ignoring a malformed optional session for fallback login."""
    options = {"locale": "zh-TW", "timezone_id": config["schedule"]["timezone"]}
    storage_state = str(config["browser"].get("storage_state", "")).strip()
    if use_session and storage_state and Path(storage_state).exists():
        try:
            return browser.new_context(storage_state=storage_state, **options)
        except Exception as exc:
            LOG.warning("Could not load configured storage_state; attempting homepage login: %s", exc)
    return browser.new_context(**options)


def browser_proxy(config: dict[str, Any]) -> dict[str, str] | None:
    """Playwright proxy settings from [browser] proxy, or None to browse directly.

    Cloudflare flags the IP that browses, so routing Chrome through a proxy on
    another machine (a VPS, for example) keeps this host's own IP out of it.
    """
    server = str(config["browser"].get("proxy", "")).strip()
    if not server:
        return None
    if urllib.parse.urlparse(server).scheme not in {"http", "https", "socks5"}:
        raise ConfigError("[browser] proxy must start with http://, https:// or socks5://")
    proxy = {"server": server}
    username = str(config["browser"].get("proxy_username", "")).strip()
    if username:
        if server.startswith("socks5://"):
            raise ConfigError("Chrome cannot authenticate to a socks5 proxy; use an unauthenticated tunnel or an http proxy")
        proxy["username"] = username
        proxy["password"] = str(config["browser"].get("proxy_password", ""))
    return proxy


def _prepare_profile(profile: str) -> None:
    """Create the Chrome profile directory, private to the service user."""
    path = Path(profile)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError as exc:
        LOG.warning("Could not restrict permissions on %s: %s", profile, exc)


def chrome_executable(config: dict[str, Any]) -> str:
    """Path of the Chrome the service uses, for launching it without automation."""
    explicit = str(config["browser"].get("executable_path", "")).strip()
    if explicit:
        return explicit
    for name in ("google-chrome-stable", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise CannotConfirm("Could not find Google Chrome; set [browser] executable_path")


def manual_login_command(config: dict[str, Any]) -> list[str]:
    """Command line for a plain Chrome window on the service's profile.

    --password-store=basic matches how Playwright launches Chrome; without it
    a desktop keyring encrypts the cookies and the service cannot read them.
    """
    command = [
        chrome_executable(config),
        f"--user-data-dir={profile_dir(config)}",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-TW",
    ]
    proxy = browser_proxy(config)
    if proxy:
        command.append(f"--proxy-server={proxy['server']}")
    command.append(str(config["thread"]["url"]))
    return command


def manual_login(config: dict[str, Any]) -> None:
    """Let a person sign in and pass any verification in the service's Chrome profile.

    Opens an ordinary Chrome window (no automation attached) on the same
    profile and from the same host and IP the service browses with. Closing
    the window hands the profile back; the service then checks it is signed in.
    """
    profile = profile_dir(config)
    if not profile:
        raise ConfigError("--manual-login requires [browser] user_data_dir")
    with bump_lock(config):
        _prepare_profile(profile)
        print(
            "已開啟一般的 Chrome 視窗（使用服務的設定檔）。請在視窗中完成人機驗證並登入巴哈姆特，"
            "確認能看到文章後關閉整個 Chrome 視窗。",
            flush=True,
        )
        subprocess.run(manual_login_command(config), check=False)
        with chrome_page(config) as page:
            verify_profile_session(page, config)
        _write_login_pause(config, None)
    print("設定檔已登入，服務下次檢查時會直接使用。", flush=True)


@contextmanager
def chrome_page(config: dict[str, Any], *, use_session: bool = True):
    """Launch the system Chrome, yield one page, and always close the browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CannotConfirm("The Playwright Python package is required; run pip install -r requirements.txt") from exc
    launch_options: dict[str, Any] = {"headless": bool(config["browser"].get("headless", True))}
    if config["browser"].get("executable_path"):
        launch_options["executable_path"] = str(config["browser"]["executable_path"])
    else:
        launch_options["channel"] = str(config["browser"].get("channel", "chrome"))
    proxy = browser_proxy(config)
    if proxy:
        launch_options["proxy"] = proxy
    profile = profile_dir(config) if use_session else ""
    with sync_playwright() as playwright:
        browser = None
        try:
            if profile:
                # One long-lived profile: cookies (the login and whatever a
                # person earned by passing a verification in --manual-login)
                # stay in Chrome instead of being rebuilt from JSON every run.
                _prepare_profile(profile)
                if launch_options["headless"]:
                    # Headless Chrome announces itself as "HeadlessChrome", so
                    # it is not the browser a person verified in --manual-login.
                    LOG.warning("user_data_dir works best with headless = false (run under xvfb-run on a server)")
                browser = playwright.chromium.launch_persistent_context(
                    profile, locale="zh-TW", timezone_id=config["schedule"]["timezone"], **launch_options,
                )
                yield browser.pages[0] if browser.pages else browser.new_page()
            else:
                browser = playwright.chromium.launch(**launch_options)
                yield _new_context(browser, config, use_session=use_session).new_page()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "Failed to launch" in str(exc):
                raise CannotConfirm(
                    "Could not launch system Google Chrome; install google-chrome-stable or set [browser] executable_path"
                ) from exc
            raise
        finally:
            if browser is not None:
                browser.close()


def inspect_and_maybe_bump(page: Any, config: dict[str, Any], now: datetime) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise CannotConfirm("Playwright is required; run pip install -r requirements.txt") from exc
    path = state_path(config)
    state = read_state(path)
    recovered = False
    if "pending_bump" in state:
        LOG.info("Checking the previous unfinished bump before starting another")
        recovered = finish_pending_bump(page, config, state, collect_posts(page, config, state, skip_seen=True), now)
    # Only the last page can tell whether today already has a bump: new
    # replies always land there. Seen pages are skipped, so this costs one
    # fresh page load instead of a full thread scan.
    last_posts = collect_posts(page, config, state, skip_seen=True)
    latest = parse_post_time(last_posts[-1].raw_time, now.tzinfo, now)
    if is_today(latest, now) or state.get("last_confirmed_date") == now.date().isoformat():
        LOG.info("Today's bump is already present; no additional reply is needed")
        state.pop("pending_bump", None)
        state["last_confirmed_date"] = now.date().isoformat()
        write_state(path, state)
        if not recovered and config["cleanup"].get("enabled", False):
            # A dropped pending bump skipped its cleanup; catch up here.
            _ensure_inventory(page, config, state)
            cleanup_inventory(page, config, state)
        return "recovered" if recovered else "already_done"

    before_sn = max(int(post.sn) for post in last_posts)
    state["pending_bump"] = {
        "thread_url": str(config["thread"]["url"]),
        "before_sn": before_sn,
    }
    # Fresh re-check right before submitting, so a bump posted by someone else
    # in the meantime is not duplicated.
    fresh_posts = _reload_last_page(page, config, state)
    if is_today(parse_post_time(fresh_posts[-1].raw_time, now.tzinfo, now), now):
        state.pop("pending_bump", None)
        state["last_confirmed_date"] = now.date().isoformat()
        write_state(path, state)
        return "recovered" if recovered else "already_done"
    write_state(path, state)
    body = str(config["thread"].get("content_template", DEFAULT_TEMPLATE)).format(
        timestamp=now.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    )
    editor_selector = str(config["selectors"].get("editor", "#editor"))
    submit_selector = str(config["selectors"].get("post_submit", "[data-action='quick-post'], #quick-post, button[type='submit']"))
    editor = _first_visible(page, editor_selector)
    submit = _first_visible(page, submit_selector)
    if not editor:
        raise CannotConfirm("Reply editor layout is not recognized")
    editor_tag = editor.evaluate("element => element.tagName")
    _pace(config)
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
        page.wait_for_load_state("domcontentloaded", timeout=int(config["browser"].get("navigation_timeout_ms", 30000)))
    except PlaywrightTimeoutError:
        LOG.warning("Reply navigation timed out; checking from an independent page")
    submit_rate_limited = _rate_limited(page)
    try:
        # Bahamut reports rejected replies (flood interval, content rules) on a
        # system message page; keep its text for the failure notification.
        submit_response = f"title={page.title()!r}, message={' '.join(page.locator('body').inner_text(timeout=5000).split())[:120]!r}"
    except Exception:
        submit_response = "<unavailable>"
    if submit_rate_limited:
        # The reply may still have been stored; let the limit clear before the
        # verification scan instead of guessing either way.
        wait = max(1, int(config["browser"].get("rate_limit_wait_seconds", 60)))
        LOG.warning("Reply submission hit Bahamut's rate limit; waiting %s seconds before verifying", wait)
        time.sleep(wait)
    verification_page = page.context.new_page()
    try:
        verified_posts = collect_posts(verification_page, config, state, skip_seen=True)
    finally:
        verification_page.close()
    if not finish_pending_bump(page, config, state, verified_posts, now):
        if submit_rate_limited:
            raise CannotConfirm(
                f"Reply submission was rejected by Bahamut's rate limit (HTTP 429 {RATE_LIMIT_TITLE}); it will be retried"
            )
        raise CannotConfirm(
            f"Reply was submitted but no new owned reply could be verified; Bahamut response: {submit_response}"
        )
    return "posted"


def run_once(config: dict[str, Any]) -> str:
    now = taiwan_now(config)
    LOG.info("Checking %s at %s", config["thread"]["url"], now.isoformat())
    with bump_lock(config), chrome_page(config) as page:
        authenticate(page, config)
        return inspect_and_maybe_bump(page, config, now)


def run_cleanup(config: dict[str, Any], max_deletions: int = 0) -> int:
    """Run the explicit cleanup command in the same authenticated Chrome session."""
    with bump_lock(config), chrome_page(config) as page:
        authenticate(page, config)
        state = read_state(state_path(config))
        _ensure_inventory(page, config, state)
        return cleanup_inventory(page, config, state, max_deletions=max_deletions)


def test_imported_session(config: dict[str, Any]) -> None:
    """Open Chrome with the profile or imported storage state and verify authentication only."""
    with bump_lock(config), chrome_page(config) as page:
        if profile_dir(config):
            verify_profile_session(page, config)
        else:
            verify_imported_session(page, config)


def test_automatic_login(config: dict[str, Any]) -> None:
    """Force a clean homepage login test without relying on an imported session."""
    with chrome_page(config, use_session=False) as page:
        login_from_homepage(page, config)


def state_path(config: dict[str, Any]) -> Path:
    return Path(str(config["schedule"].get("state_file", "state.json")))


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as exc:
        raise CannotConfirm(f"Could not read bump recovery state: {path}") from exc
    if not isinstance(value, dict):
        raise CannotConfirm(f"Invalid bump recovery state: {path}")
    return value


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


@contextmanager
def bump_lock(config: dict[str, Any]):
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Closing the fd releases the flock, so no explicit unlock is needed.
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CannotConfirm("Another bump or cleanup is already running") from exc
        yield


def scheduled_datetime(now: datetime, config: dict[str, Any]) -> datetime:
    hour, minute = map(int, str(config["schedule"]["time"]).split(":", 1))
    return datetime.combine(now.date(), dt_time(hour, minute), tzinfo=now.tzinfo)


def _day_jitter_seconds(now: datetime, config: dict[str, Any]) -> int:
    """Per-day random delay for the scheduled run, stable within one day.

    Seeded by the calendar date so the delay does not move while the service
    waits; a bump landing at the exact same second every day is an easy bot tell.
    """
    jitter = max(0, int(config["schedule"].get("schedule_jitter_seconds", 1800)))
    if jitter <= 0:
        return 0
    return int(random.Random(now.date().isoformat()).uniform(0, jitter))


TELEGRAM_ALLOWED_HOSTS = {"api.telegram.org"}


def _open_telegram_url(url: str, *, timeout: int, data: bytes | None = None) -> Any:
    """Open a Telegram URL after verifying it targets an allowlisted host over HTTPS.

    _replace_session builds its URL from a file_path returned by the Telegram
    API, so the target is not fully under this process's control; reject any
    scheme or host that is not the expected endpoint before fetching it.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in TELEGRAM_ALLOWED_HOSTS:
        raise RuntimeError(f"Refusing to request non-Telegram URL: {parsed.scheme}://{parsed.hostname}")
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - host allowlisted above


NOTIFICATION_LABELS = {
    "success": "成功通知",
    "error": "錯誤通知",
    "auth": "Cookie/session 通知",
    "layout": "排版通知",
    "system": "系統通知",
}
NOTIFICATION_TYPES = frozenset(NOTIFICATION_LABELS)
NOTIFICATION_ORDER = tuple(NOTIFICATION_LABELS)


class TelegramNotifier:
    """Small Bot API client; command state and update offset survive restarts."""

    COMMANDS = [
        {"command": "toggle", "description": "切換通知狀態（可用按鈕選擇）"},
        {"command": "session", "description": "上傳並替換 Bahamut session"},
        {"command": "test_cookie", "description": "測試 Cookie/session 是否有效"},
        {"command": "test_login", "description": "測試帳號自動登入"},
        {"command": "set_message", "description": "設定頂文訊息"},
        {"command": "status", "description": "查看通知與訊息設定"},
        {"command": "help", "description": "查看所有指令"},
    ]

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.token = str(config["telegram"]["bot_token"])
        self.target_chat_id = str(config["telegram"]["target_chat_id"])
        self.admin_chat_id = str(config["telegram"]["admin_chat_id"])
        self.path = Path(str(config["telegram"].get("state_file", "telegram_state.json")))
        saved = self._read_state()
        self.offset = int(saved.get("offset", 0))
        self.disabled = set(saved.get("disabled", [])) & NOTIFICATION_TYPES
        self.awaiting_session = bool(saved.get("awaiting_session", False))
        # Set when /session replaces the session file; run_loop retries at once.
        self.session_uploaded = False
        self.message_template = str(saved.get("message_template", ""))
        self._register_commands()

    def _register_commands(self) -> None:
        try:
            self._api(
                "setMyCommands",
                {
                    "commands": json.dumps(self.COMMANDS, ensure_ascii=False),
                    "scope": json.dumps({"type": "chat", "chat_id": self.admin_chat_id}),
                },
            )
        except Exception as exc:
            LOG.warning("Telegram command menu registration failed: %s", exc)

    def _read_state(self) -> dict[str, Any]:
        # A corrupt Telegram state only costs the update offset; never fail the service for it.
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self) -> None:
        write_state(self.path, {
            "offset": self.offset,
            "disabled": sorted(self.disabled),
            "awaiting_session": self.awaiting_session,
            "message_template": self.message_template,
        })

    def _api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        with _open_telegram_url(url, timeout=15, data=encoded) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return payload

    def send(self, kind: str, text: str) -> None:
        if kind in self.disabled:
            LOG.info("Telegram notification type %s is disabled", kind)
            return
        is_failure = kind in {"error", "auth", "layout", "system"}
        prefix = "✅" if kind == "success" else "❌" if is_failure else ""
        if prefix and not text.startswith(prefix):
            text = f"{prefix} {text}"
        chat_id = self.admin_chat_id if is_failure else self.target_chat_id
        try:
            self._api("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception as exc:
            LOG.warning("Telegram notification failed: %s", exc)

    def _command_reply(self, text: str) -> None:
        try:
            self._api("sendMessage", {"chat_id": self.admin_chat_id, "text": text})
        except Exception as exc:
            LOG.warning("Telegram command reply failed: %s", exc)

    def _notification_menu(self) -> None:
        try:
            self._api(
                "sendMessage",
                {
                    "chat_id": self.admin_chat_id,
                    "text": "請選擇要切換的通知類型：",
                    "reply_markup": json.dumps({"inline_keyboard": self._notification_buttons()}, ensure_ascii=False),
                },
            )
        except Exception as exc:
            LOG.warning("Telegram notification menu failed: %s", exc)

    def _notification_buttons(self) -> list[list[dict[str, str]]]:
        """Build Chinese labels with current state shown by emoji."""
        buttons = [
            [{"text": f"{'✅' if kind not in self.disabled else '❌'} {NOTIFICATION_LABELS[kind]}",
              "callback_data": f"notify:{kind}"}]
            for kind in NOTIFICATION_ORDER
        ]
        buttons.append([{
            "text": f"{'✅' if not self.disabled else '❌'} 全部通知",
            "callback_data": "notify:all",
        }])
        return buttons

    def _refresh_notification_menu(self, message: dict[str, Any]) -> None:
        message_id = message.get("message_id")
        if not message_id:
            return
        chat = message.get("chat") or {}
        try:
            self._api("editMessageReplyMarkup", {
                "chat_id": str(chat.get("id", self.admin_chat_id)),
                "message_id": str(message_id),
                "reply_markup": json.dumps({"inline_keyboard": self._notification_buttons()}, ensure_ascii=False),
            })
        except Exception as exc:
            LOG.warning("Telegram notification menu refresh failed: %s", exc)

    def _toggle(self, kind: str) -> str:
        """Flip one notification type (or every type at once) and persist it."""
        if kind == "all":
            self.disabled = set() if self.disabled else set(NOTIFICATION_TYPES)
            reply = "已切換全部通知。"
        elif kind in self.disabled:
            self.disabled.remove(kind)
            reply = f"已開啟通知類型：{NOTIFICATION_LABELS[kind]}。"
        else:
            self.disabled.add(kind)
            reply = f"已關閉通知類型：{NOTIFICATION_LABELS[kind]}。"
        self._write_state()
        return reply

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = str(callback.get("id", ""))
        if str(chat.get("id")) != self.admin_chat_id:
            return
        prefix, _, kind = str(callback.get("data", "")).partition(":")
        if prefix != "notify" or (kind not in NOTIFICATION_TYPES and kind != "all"):
            return
        reply = self._toggle(kind)
        self._refresh_notification_menu(message)
        try:
            if callback_id:
                self._api("answerCallbackQuery", {"callback_query_id": callback_id, "text": reply})
        except Exception as exc:
            LOG.warning("Telegram callback acknowledgement failed: %s", exc)
        self._command_reply(reply)

    def _replace_session(self, file_id: str) -> None:
        storage_state = str(self.config["browser"].get("storage_state", "")).strip()
        if not storage_state:
            raise ConfigError("browser.storage_state is required for Telegram session upload")
        file_info = self._api("getFile", {"file_id": file_id})
        file_path = str((file_info.get("result") or {}).get("file_path", ""))
        if not file_path:
            raise RuntimeError("Telegram did not return a session file path")
        # file_path is remote input; quote it so it cannot add a query or fragment.
        url = f"https://api.telegram.org/file/bot{self.token}/{urllib.parse.quote(file_path, safe='/')}"
        with _open_telegram_url(url, timeout=30) as response:
            raw = response.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise RuntimeError("Session file is too large")
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Uploaded file is not valid UTF-8 JSON") from exc
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise RuntimeError("Uploaded file is not a Playwright storage_state JSON")
        destination = Path(storage_state)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".telegram.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
        self.awaiting_session = False
        self.session_uploaded = True
        self._write_state()

    def _set_message(self, value: str) -> None:
        value = value.strip()
        if value.lower() in {"default", "reset", "恢復預設"}:
            self.message_template = ""
        else:
            if not value:
                raise ValueError("訊息不可為空")
            if len(value) > 1000:
                raise ValueError("訊息長度不可超過 1000 個字元")
            try:
                value.format(timestamp="2026-01-01 00:00:00 UTC+8")
            except (KeyError, ValueError, IndexError) as exc:
                raise ValueError("訊息模板格式錯誤，只能使用 {timestamp} 變數") from exc
            self.message_template = value
        self._write_state()

    def configured(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return config with a Telegram-managed message override, if present."""
        if not self.message_template:
            return config
        thread = dict(config["thread"])
        thread["content_template"] = self.message_template
        values = dict(config)
        values["thread"] = thread
        return values

    def poll_commands(self) -> None:
        try:
            payload = self._api(
                "getUpdates",
                {"offset": self.offset, "timeout": 0, "allowed_updates": '["message","callback_query"]'},
            )
        except Exception as exc:
            LOG.warning("Telegram command polling failed: %s", exc)
            return
        for update in payload.get("result", []):
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            callback = update.get("callback_query")
            if callback:
                self._handle_callback(callback)
                continue
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != self.admin_chat_id:
                continue
            text = str(message.get("text") or message.get("caption") or "").strip()
            command = text.split(maxsplit=1)
            name = command[0].split("@", 1)[0].lower() if command else ""
            arg_text = command[1] if len(command) > 1 else ""
            arg = arg_text.lower()
            document = message.get("document") or {}
            if document and (self.awaiting_session or name in {"/session", "/upload_session", "/replace_session"}):
                try:
                    self._replace_session(str(document.get("file_id", "")))
                    self._command_reply("新的 Bahamut session 已驗證格式並替換完成。使用 /test_cookie 測試登入狀態。")
                except Exception as exc:
                    LOG.warning("Telegram session replacement failed: %s", exc)
                    self._command_reply(f"session 替換失敗：{exc}")
                continue
            if name == "/toggle" and (arg in NOTIFICATION_TYPES or arg == "all"):
                self._command_reply(self._toggle(arg))
            elif name == "/toggle":
                self._notification_menu()
            elif name == "/status":
                enabled = sorted(NOTIFICATION_TYPES - self.disabled)
                message_status = "自訂" if self.message_template else "設定檔預設"
                self._command_reply("啟用通知：" + (", ".join(NOTIFICATION_LABELS[kind] for kind in NOTIFICATION_ORDER if kind in enabled) if enabled else "無") + f"\n頂文訊息：{message_status}")
            elif name in {"/session", "/upload_session", "/replace_session"}:
                self.awaiting_session = True
                self._write_state()
                self._command_reply("請直接上傳新的 bahamut-session.json 文件；收到後會原子替換目前 session。")
            elif name in {"/test_cookie", "/test_session"}:
                try:
                    test_imported_session(self.config)
                    self._command_reply("Cookie/session 測試成功，巴哈姆特目前仍是登入狀態。")
                except Exception as exc:
                    self._command_reply(f"Cookie/session 測試失敗：{exc}")
            elif name == "/test_login":
                try:
                    test_automatic_login(self.config)
                    self._command_reply("自動登入測試成功，已從巴哈姆特首頁登入。")
                except Exception as exc:
                    self._command_reply(f"自動登入測試失敗：{exc}")
            elif name in {"/set_message", "/set_bump_message"}:
                try:
                    self._set_message(arg_text)
                    shown = self.message_template or "設定檔預設訊息"
                    self._command_reply(f"頂文訊息已更新為：\n{shown}")
                except Exception as exc:
                    self._command_reply(f"頂文訊息更新失敗：{exc}")
            elif name in {"/help", "/start"}:
                self._command_reply(
                    "指令：/toggle <success|error|auth|layout|system|all>、"
                    "/session 後上傳 session JSON、/test_cookie、/test_login、"
                    "/set_message <訊息>、/status、/help"
                )
        self._write_state()


def notification_kind(exc: Exception) -> str:
    if isinstance(exc, AntiBotChallenge):
        return "auth"
    message = str(exc).lower()
    if any(word in message for word in ("login", "cookie", "session", "authenticated", "登入")):
        return "auth"
    if any(word in message for word in ("layout", "selector", "timestamp", "post containers", "樓層")):
        return "layout"
    if isinstance(exc, CannotConfirm):
        return "error"
    return "system"


def sleep_with_telegram(seconds: int, notifier: TelegramNotifier) -> None:
    poll_interval = max(1, int(notifier.config["telegram"].get("poll_interval_seconds", 2)))
    deadline = time.monotonic() + seconds
    while True:
        notifier.poll_commands()
        if notifier.session_uploaded:
            return
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            return
        time.sleep(min(poll_interval, remaining))


def notice_reason(reason: str) -> str:
    """Reduce a failure reason to what stays the same between retries.

    A challenged page alternates between two titles and its message carries
    pause lengths and times, so the raw text changed between retries and the
    same Cloudflare block was reported up to three times a day.
    """
    reason = re.sub(r"title=(['\"]).*?\1", "title=?", reason)
    return re.sub(r"\d+", "#", reason)


class FailureNotice:
    """Notify a failure once, not on every retry.

    A failure that keeps repeating (expired session, anti-bot page) used to
    send a Telegram message every retry_interval_seconds, about 96 a day with
    the default. The same kind and reason are now reported once per day; a new
    reason, a new day or a success in between reports again.
    """

    def __init__(self) -> None:
        self.last: tuple[str, str, str] | None = None

    def should_send(self, kind: str, reason: str, day: str) -> bool:
        signature = (day, kind, notice_reason(reason))
        if signature == self.last:
            return False
        self.last = signature
        return True

    def reset(self) -> None:
        self.last = None


def _retry_delay_seconds(config: dict[str, Any], consecutive_failures: int) -> float:
    """Retry wait with exponential backoff and jitter.

    Each consecutive failure doubles the wait (up to the cap), so a challenged
    host IP is not hammered every retry_interval_seconds all day; jitter keeps
    the retries from landing on a fixed grid.
    """
    base = max(30, int(config["schedule"]["retry_interval_seconds"]))
    cap = max(base, int(config["schedule"].get("retry_backoff_max_seconds", 7200)))
    jitter = max(0, int(config["schedule"].get("retry_jitter_seconds", 300)))
    backoff = min(cap, base * (2 ** max(0, consecutive_failures - 1)))
    return float(backoff) + (random.uniform(0, jitter) if jitter > 0 else 0.0)


def run_loop(config: dict[str, Any]) -> None:
    path = state_path(config)
    notifier = TelegramNotifier(config)
    failures = FailureNotice()
    consecutive_failures = 0
    while True:
        notifier.poll_commands()
        if notifier.session_uploaded:
            # A fresh session should not wait out a long retry backoff.
            notifier.session_uploaded = False
            consecutive_failures = 0
        now = taiwan_now(config)
        try:
            state = read_state(path)
            due = scheduled_datetime(now, config) + timedelta(seconds=_day_jitter_seconds(now, config))
            if state.get("last_confirmed_date") == now.date().isoformat() and "pending_bump" not in state:
                tomorrow = due + timedelta(days=1)
                sleep_for = max(30, int((tomorrow - now).total_seconds()))
                LOG.info("Today's state is confirmed; next check in %s seconds", sleep_for)
                sleep_with_telegram(min(sleep_for, 3600), notifier)
                continue
            if now < due:
                sleep_with_telegram(min(max(30, int((due - now).total_seconds())), 3600), notifier)
                continue
            result = run_once(notifier.configured(config))
            failures.reset()
            consecutive_failures = 0
            if result in {"posted", "recovered"}:
                message = "巴哈姆特頂文成功" if result == "posted" else "巴哈姆特上次頂文與清理已補完"
                notifier.send("success", f"{message}\n文章：{config['thread']['url']}\n時間：{now.strftime('%Y-%m-%d %H:%M:%S UTC+8')}")
        except Exception as exc:
            consecutive_failures += 1
            delay = _retry_delay_seconds(config, consecutive_failures)
            LOG.warning("Unable to safely confirm today's status: %s; retrying in %s seconds", exc, int(delay))
            kind = notification_kind(exc)
            hint = ""
            if isinstance(exc, AntiBotChallenge) and profile_dir(config):
                hint = "\n處理方式：在服務主機執行 --manual-login，於一般 Chrome 視窗親自通過驗證後關閉視窗"
            if failures.should_send(kind, str(exc), now.date().isoformat()):
                notifier.send(kind, f"巴哈姆特自動頂文發生問題\n文章：{config['thread']['url']}\n類型：{kind}\n原因：{exc}{hint}\n（同樣的問題今天不再重複通知，服務仍會重試，下次約 {int(delay)} 秒後）")
            sleep_with_telegram(int(delay), notifier)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--cleanup", action="store_true", help="inspect and clean old owned replies")
    parser.add_argument("--apply", action="store_true", help="allow live deletion with --cleanup; otherwise dry-run")
    parser.add_argument("--cleanup-limit", type=int, default=0, help="maximum replies to delete during --cleanup (0 = all)")
    parser.add_argument(
        "--manual-login", action="store_true",
        help="open a normal Chrome window on [browser] user_data_dir to sign in and pass verification by hand",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    notifier = None
    try:
        config = load_config(args.config)
        if args.manual_login:
            manual_login(config)
            return 0
        if args.cleanup:
            cleanup_values = dict(config["cleanup"])
            if args.apply:
                cleanup_values["dry_run"] = False
            cleanup_config = {**config, "cleanup": cleanup_values}
            deleted = run_cleanup(cleanup_config, max_deletions=max(0, args.cleanup_limit))
            LOG.info("Cleanup finished; deleted %s post(s)", deleted)
            return 0
        notifier = TelegramNotifier(config)
        if args.once:
            result = run_once(notifier.configured(config))
            if result == "posted":
                notifier.send("success", f"巴哈姆特頂文成功\n文章：{config['thread']['url']}")
            elif result == "recovered":
                notifier.send("success", f"巴哈姆特上次頂文與清理已補完\n文章：{config['thread']['url']}")
        else:
            run_loop(config)
        return 0
    except (ConfigError, CannotConfirm) as exc:
        LOG.error("%s", exc)
        if notifier is not None:
            notifier.send(notification_kind(exc), f"巴哈姆特自動頂文發生問題\n原因：{exc}")
        return 2
    except Exception:
        LOG.exception("Unexpected failure; no bump was attempted unless submission was already verified")
        if notifier is not None:
            notifier.send("system", "巴哈姆特自動頂文發生未預期錯誤，請查看服務日誌。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
