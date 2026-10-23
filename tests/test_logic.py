import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bahamut_auto_bump import (
    CannotConfirm,
    NOTIFICATION_LABELS,
    NOTIFICATION_ORDER,
    PostInfo,
    RateLimited,
    TelegramNotifier,
    _account_credentials,
    _thread_page_urls,
    deletion_candidates,
    finish_pending_bump,
    is_bump_text,
    load_config,
    parse_post_time,
    read_state,
    write_state,
)


class TimestampTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Taipei")

    def test_iso_utc_is_converted_to_taipei(self):
        value = parse_post_time("2026-09-11T16:00:00Z", self.tz)
        self.assertEqual(value, datetime(2026, 9, 12, 0, 0, tzinfo=self.tz))

    def test_text_timestamp_uses_configured_timezone(self):
        value = parse_post_time("2026/09/12 08:30", self.tz)
        self.assertEqual(value, datetime(2026, 9, 12, 8, 30, tzinfo=self.tz))

    def test_relative_yesterday_timestamp_uses_reference_date(self):
        now = datetime(2026, 9, 12, 4, 9, tzinfo=self.tz)
        value = parse_post_time("昨天 06:31", self.tz, now)
        self.assertEqual(value, datetime(2026, 9, 11, 6, 31, tzinfo=self.tz))

    def test_relative_today_timestamp_with_edit_suffix(self):
        now = datetime(2026, 9, 12, 12, 0, tzinfo=self.tz)
        value = parse_post_time("今天 11:59 編輯", self.tz, now)
        self.assertEqual(value, datetime(2026, 9, 12, 11, 59, tzinfo=self.tz))

    def test_bad_timestamp_is_rejected(self):
        with self.assertRaises(Exception):
            parse_post_time("not-a-time", self.tz)


class AuthenticationConfigTests(unittest.TestCase):
    def test_account_credentials_are_required_for_fallback_login(self):
        config = {"account": {}, "browser": {}, "selectors": {}}
        with self.assertRaises(CannotConfirm):
            _account_credentials(config)

    def test_account_credentials_allow_optional_totp(self):
        config = {
            "account": {"username": "user", "password": "pass", "totp_secret": " ABC123 "},
            "browser": {},
            "selectors": {},
        }
        self.assertEqual(_account_credentials(config), ("user", "pass", "ABC123"))


class ConfigLoadingTests(unittest.TestCase):
    MINIMAL = """
[thread]
url = "https://forum.gamer.com.tw/C.php?bsn=1&snA=2"
[selectors]
[browser]
[schedule]
timezone = "Asia/Taipei"
time = "09:00"
retry_interval_seconds = 900
[telegram]
bot_token = "t"
target_chat_id = "-100"
admin_chat_id = "1"
"""

    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_optional_cleanup_section_always_present(self):
        # Every config["cleanup"] read depends on load_config supplying the default.
        self.assertEqual(self._load(self.MINIMAL)["cleanup"], {})

    def test_explicit_cleanup_section_is_preserved(self):
        config = self._load(self.MINIMAL + '\n[cleanup]\nenabled = true\n')
        self.assertEqual(config["cleanup"], {"enabled": True})

    def test_short_retry_interval_is_rejected(self):
        with self.assertRaises(Exception):
            self._load(self.MINIMAL.replace("retry_interval_seconds = 900", "retry_interval_seconds = 5"))


class CleanupTests(unittest.TestCase):
    def test_floor_one_and_newest_reply_are_retained(self):
        posts = [
            PostInfo(1, "root", True, "2026-01-01 00:00:00", True),
            PostInfo(2, "old", True, "2026-01-02 00:00:00", True),
            PostInfo(3, "previous", True, "2026-01-03 00:00:00", True),
            PostInfo(4, "latest", True, "2026-01-04 00:00:00", True),
            PostInfo(5, "other-user", False, "2026-01-05 00:00:00", True),
        ]
        self.assertEqual([post.sn for post in deletion_candidates(posts, 1)], ["previous", "old"])

    def test_owned_non_bump_reply_is_never_a_candidate(self):
        posts = [
            PostInfo(2, "10", True, "2026-01-02 00:00:00", True),
            PostInfo(3, "20", True, "2026-01-03 00:00:00", False),
            PostInfo(4, "30", True, "2026-01-04 00:00:00", True),
        ]
        self.assertEqual([post.sn for post in deletion_candidates(posts, 1)], ["10"])

    def test_keep_two_replies(self):
        posts = [PostInfo(floor, str(floor), True, "2026-01-01 00:00:00", True) for floor in range(1, 6)]
        self.assertEqual([post.floor for post in deletion_candidates(posts, 2)], [3, 2])

    def test_latest_is_selected_by_post_number_not_page_floor(self):
        posts = [
            PostInfo(1, "100", True, "2026-01-01 00:00:00", True),
            PostInfo(8, "800", True, "2026-01-02 00:00:00", True),
            PostInfo(140, "1400", True, "2026-01-03 00:00:00", True),
            PostInfo(2, "1500", True, "2026-01-04 00:00:00", True),
        ]
        self.assertEqual([post.sn for post in deletion_candidates(posts, 1)], ["1400", "800"])


class FakePaginationPage:
    def __init__(self, url, hrefs):
        self.url = url
        self.hrefs = hrefs

    def locator(self, selector):
        return self

    def evaluate_all(self, script):
        return self.hrefs


class PaginationTests(unittest.TestCase):
    BASE = "https://forum.gamer.com.tw/C.php?bsn=1&snA=2"

    def test_scan_from_last_page_includes_that_page(self):
        # Regression: the viewed page is not a link, so page 18 used to be skipped.
        hrefs = [f"{self.BASE}&page={n}" for n in range(1, 18)]
        urls = _thread_page_urls(FakePaginationPage(f"{self.BASE}&page=18", hrefs), {})
        self.assertEqual(len(urls), 18)
        self.assertTrue(urls[-1].endswith("page=18"))

    def test_scan_from_first_page_uses_linked_pages(self):
        hrefs = [f"{self.BASE}&page={n}" for n in range(2, 19)]
        self.assertEqual(len(_thread_page_urls(FakePaginationPage(self.BASE, hrefs), {})), 18)


class BumpTextTests(unittest.TestCase):
    TEMPLATE = "頂🆙！\n（{timestamp}）"

    def test_rendered_bump_matches_template(self):
        self.assertTrue(is_bump_text("頂🆙！\n （2026-10-05 09:00:04 UTC+8）\n", self.TEMPLATE))

    def test_other_text_does_not_match(self):
        self.assertFalse(is_bump_text("頂🆙！（2026-10-05 09:00:04 UTC+8）伺服器今晚維修", self.TEMPLATE))
        self.assertFalse(is_bump_text("歡迎加入伺服器", self.TEMPLATE))
        self.assertFalse(is_bump_text("", self.TEMPLATE))

    def test_custom_template_is_respected(self):
        self.assertTrue(is_bump_text("up 2026-10-05 09:00:04 UTC+8", "up {timestamp}"))
        self.assertFalse(is_bump_text("頂🆙！（2026-10-05 09:00:04 UTC+8）", "up {timestamp}"))


class NotificationMenuTests(unittest.TestCase):
    def test_command_menu_uses_toggle_only(self):
        commands = {item["command"] for item in TelegramNotifier.COMMANDS}
        self.assertIn("toggle", commands)
        self.assertIn("test_login", commands)
        self.assertNotIn("enable", commands)
        self.assertNotIn("disable", commands)

    def test_buttons_show_chinese_labels_and_current_emoji_state(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.disabled = {"error", "system"}
        buttons = notifier._notification_buttons()
        labels = [row[0]["text"] for row in buttons]
        self.assertEqual(labels[:5], [
            f"✅ {NOTIFICATION_LABELS['success']}",
            f"❌ {NOTIFICATION_LABELS['error']}",
            f"✅ {NOTIFICATION_LABELS['auth']}",
            f"✅ {NOTIFICATION_LABELS['layout']}",
            f"❌ {NOTIFICATION_LABELS['system']}",
        ])
        self.assertEqual(labels[5], "❌ 全部通知")

    def test_callback_data_round_trips_through_toggle(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.disabled = set()
        notifier._write_state = lambda: None
        payloads = [row[0]["callback_data"] for row in notifier._notification_buttons()]
        self.assertEqual(payloads, [f"notify:{kind}" for kind in NOTIFICATION_ORDER] + ["notify:all"])
        for payload in payloads:
            kind = payload.split(":", 1)[1]
            self.assertTrue(kind == "all" or kind in NOTIFICATION_LABELS)
        notifier._toggle("error")
        self.assertEqual(notifier.disabled, {"error"})
        notifier._toggle("error")
        self.assertEqual(notifier.disabled, set())
        notifier._toggle("all")
        self.assertEqual(notifier.disabled, set(NOTIFICATION_ORDER))

    def test_send_prefixes_success_and_error_notifications(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.disabled = set()
        notifier.target_chat_id = "chat"
        notifier.admin_chat_id = "admin"
        sent = []
        notifier._api = lambda method, params: sent.append(params)
        notifier.send("success", "頂文成功")
        notifier.send("auth", "Cookie 已失效")
        self.assertEqual(sent[0]["text"], "✅ 頂文成功")
        self.assertEqual(sent[0]["chat_id"], "chat")
        self.assertEqual(sent[1]["text"], "❌ Cookie 已失效")
        self.assertEqual(sent[1]["chat_id"], "admin")


class RecordingPage:
    """Playwright stand-in that never deletes; records verify/delete calls."""

    def __init__(self):
        self.deleted = []
        self.verified = []


class PendingBumpTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=self.tz)
        self.url = "https://forum.gamer.com.tw/C.php?bsn=1&snA=2"

    def _config(self, **cleanup):
        return {
            "thread": {"url": self.url},
            "selectors": {},
            "browser": {},
            "schedule": {"timezone": "Asia/Taipei", "state_file": "state.json"},
            "telegram": {},
            "cleanup": cleanup,
        }

    def _run(self, state, posts, config, page=None, delete=None, verify=None):
        import bahamut_auto_bump as mod
        page = page or RecordingPage()
        saved = (mod.delete_post, mod.verify_post_deleted, mod.state_path, mod.time.sleep)
        mod.delete_post = delete or (lambda p, c, post: p.deleted.append(post.sn))
        mod.verify_post_deleted = verify or (lambda p, c, post: p.verified.append(post.sn))
        mod.time.sleep = lambda s: None
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            mod.state_path = lambda c: state_file
            try:
                done = finish_pending_bump(page, config, state, posts, self.now)
            finally:
                mod.delete_post, mod.verify_post_deleted, mod.state_path, mod.time.sleep = saved
        return done, page, state

    def test_missing_new_reply_leaves_pending(self):
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [PostInfo(1, "50", True, "2026-01-01 00:00:00")]
        done, page, state = self._run(state, posts, self._config())
        self.assertFalse(done)
        self.assertIn("pending_bump", state)

    def test_confirms_new_reply_without_cleanup(self):
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [PostInfo(2, "80", True, "2026-01-04 09:00:00", True), PostInfo(3, "150", True, "2026-01-05 09:00:00", True)]
        done, page, state = self._run(state, posts, self._config())
        self.assertTrue(done)
        self.assertEqual(state["last_confirmed_date"], "2026-01-05")
        self.assertNotIn("pending_bump", state)
        self.assertEqual(page.deleted, [])

    def test_catches_up_every_older_bump_only(self):
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [
            PostInfo(1, "1", True, "2026-01-01 09:00:00", True),
            PostInfo(2, "70", True, "2026-01-02 09:00:00", True),
            PostInfo(3, "75", True, "2026-01-02 12:00:00", False),
            PostInfo(4, "80", True, "2026-01-03 09:00:00", True),
            PostInfo(5, "90", False, "2026-01-04 09:00:00", True),
            PostInfo(6, "150", True, "2026-01-05 09:00:00", True),
        ]
        done, page, state = self._run(state, posts, self._config(enabled=True, dry_run=False))
        self.assertTrue(done)
        self.assertEqual(page.deleted, ["80", "70"])
        self.assertEqual(page.verified, ["80", "70"])
        self.assertNotIn("pending_bump", state)

    def test_dry_run_deletes_nothing(self):
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [PostInfo(2, "80", True, "2026-01-04 09:00:00", True), PostInfo(3, "150", True, "2026-01-05 09:00:00", True)]
        done, page, state = self._run(state, posts, self._config(enabled=True, dry_run=True))
        self.assertTrue(done)
        self.assertEqual(page.deleted, [])

    def test_failed_cleanup_keeps_pending_for_retry(self):
        def fail(p, c, post):
            raise CannotConfirm("delete failed")
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [PostInfo(2, "80", True, "2026-01-04 09:00:00", True), PostInfo(3, "150", True, "2026-01-05 09:00:00", True)]
        with self.assertRaises(CannotConfirm):
            self._run(state, posts, self._config(enabled=True, dry_run=False), delete=fail)
        self.assertEqual(state["pending_bump"]["posted_sn"], "150")

    def test_unrecognized_bump_is_confirmed_then_reported(self):
        # A template/markup mismatch must not block tomorrow's bump, but must not be silent.
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100}}
        posts = [PostInfo(2, "80", True, "2026-01-04 09:00:00", True), PostInfo(3, "150", True, "2026-01-05 09:00:00", False)]
        with self.assertRaises(CannotConfirm):
            self._run(state, posts, self._config(enabled=True, dry_run=False))
        self.assertEqual(state["last_confirmed_date"], "2026-01-05")
        self.assertNotIn("pending_bump", state)

    def test_rejects_pending_from_other_thread(self):
        state = {"pending_bump": {"thread_url": "https://other", "before_sn": 1}}
        with self.assertRaises(CannotConfirm):
            self._run(state, [], self._config())


class FakeResponse:
    def __init__(self, status):
        self.status = status


class RateLimitedPage:
    """Returns HTTP 429 for the first `limited` navigations, then 200."""

    def __init__(self, limited, title="巴哈姆特"):
        self.limited = limited
        self.visits = []
        self._title = title
        self.url = ""

    def goto(self, url, wait_until=None):
        self.visits.append(url)
        self.url = url
        if len(self.visits) <= self.limited:
            return FakeResponse(429)
        return FakeResponse(200)

    def title(self):
        return "巴哈姆特電玩資訊站 -- 系統維修中" if len(self.visits) <= self.limited else self._title


class RateLimitTests(unittest.TestCase):
    def _goto(self, page, retries=3):
        import bahamut_auto_bump as mod
        sleeps = []
        saved = mod.time.sleep
        mod.time.sleep = sleeps.append
        mod._last_navigation = 0.0
        config = {"browser": {"navigation_interval_seconds": 0, "rate_limit_wait_seconds": 10, "rate_limit_retries": retries}}
        try:
            mod._goto(page, "https://forum.gamer.com.tw/C.php?bsn=1&snA=2", config)
        finally:
            mod.time.sleep = saved
        return sleeps

    def test_waits_out_rate_limit_then_succeeds(self):
        page = RateLimitedPage(limited=2)
        sleeps = self._goto(page)
        self.assertEqual(len(page.visits), 3)
        self.assertEqual(sleeps, [10, 20])

    def test_persistent_rate_limit_fails_closed(self):
        page = RateLimitedPage(limited=10)
        with self.assertRaises(CannotConfirm) as ctx:
            self._goto(page, retries=2)
        self.assertEqual(len(page.visits), 3)
        self.assertIn("429", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RateLimited)
        self.assertNotIn("snA", str(ctx.exception))

    def test_no_wait_without_rate_limit(self):
        page = RateLimitedPage(limited=0)
        self.assertEqual(self._goto(page), [])


class FakeLink:
    def __init__(self, href):
        self.href = href

    def count(self):
        return 1

    def is_visible(self):
        return True

    def evaluate(self, script):
        return self.href


class CoverPage(RateLimitedPage):
    def __init__(self, href):
        super().__init__(limited=0)
        self.href = href

    def locator(self, selector):
        page = self

        class Locator:
            @property
            def first(self):
                return FakeLink(page.href) if ".sky" in selector else type("Missing", (), {"count": lambda s: 0})()

        return Locator()


class HomepageCoverTests(unittest.TestCase):
    def _enter(self, page):
        import bahamut_auto_bump as mod
        saved = mod.time.sleep
        mod.time.sleep = lambda s: None
        try:
            mod._enter_from_homepage_cover(page, {"selectors": {}, "browser": {"navigation_interval_seconds": 0}})
        finally:
            mod.time.sleep = saved

    def test_follows_cover_link_to_homepage(self):
        page = CoverPage("https://www.gamer.com.tw/index.php?ad=N")
        self._enter(page)
        self.assertEqual(page.visits, ["https://www.gamer.com.tw/index.php?ad=N"])

    def test_rejects_cover_link_to_other_host(self):
        page = CoverPage("https://ads.example.com/index.php")
        with self.assertRaises(CannotConfirm):
            self._enter(page)
        self.assertEqual(page.visits, [])


class StateRoundTripTests(unittest.TestCase):
    def test_write_then_read_preserves_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = {"last_confirmed_date": "2026-01-05", "pending_bump": {"before_sn": 5}}
            write_state(path, payload)
            self.assertEqual(read_state(path), payload)

    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_state(Path(tmp) / "absent.json"), {})

    def test_corrupt_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(CannotConfirm):
                read_state(path)


if __name__ == "__main__":
    unittest.main()
