import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bahamut_auto_bump import (
    CannotConfirm,
    Config,
    NOTIFICATION_LABELS,
    NOTIFICATION_ORDER,
    PostInfo,
    TelegramNotifier,
    _account_credentials,
    deletion_candidates,
    finish_pending_bump,
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
        config = Config({"account": {}, "browser": {}, "selectors": {}})
        with self.assertRaises(CannotConfirm):
            _account_credentials(config)

    def test_account_credentials_allow_optional_totp(self):
        config = Config({
            "account": {"username": "user", "password": "pass", "totp_secret": " ABC123 "},
            "browser": {},
            "selectors": {},
        })
        self.assertEqual(_account_credentials(config), ("user", "pass", "ABC123"))


class CleanupTests(unittest.TestCase):
    def test_floor_one_and_newest_reply_are_retained(self):
        posts = [
            PostInfo(1, "root", True, "2026-01-01 00:00:00", "root"),
            PostInfo(2, "old", True, "2026-01-02 00:00:00", "頂"),
            PostInfo(3, "previous", True, "2026-01-03 00:00:00", "頂"),
            PostInfo(4, "latest", True, "2026-01-04 00:00:00", "頂"),
            PostInfo(5, "other-user", False, "2026-01-05 00:00:00", "頂"),
        ]
        self.assertEqual([post.sn for post in deletion_candidates(posts, 1)], ["previous", "old"])

    def test_keep_two_replies(self):
        posts = [PostInfo(floor, str(floor), True, "2026-01-01 00:00:00", "頂") for floor in range(1, 6)]
        self.assertEqual([post.floor for post in deletion_candidates(posts, 2)], [3, 2])

    def test_latest_is_selected_by_post_number_not_page_floor(self):
        posts = [
            PostInfo(1, "100", True, "2026-01-01 00:00:00", "root"),
            PostInfo(8, "800", True, "2026-01-02 00:00:00", "old page tail"),
            PostInfo(140, "1400", True, "2026-01-03 00:00:00", "old page tail"),
            PostInfo(2, "1500", True, "2026-01-04 00:00:00", "newest reply"),
        ]
        self.assertEqual([post.sn for post in deletion_candidates(posts, 1)], ["1400", "800"])


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
        buttons = notifier._notification_buttons("toggle")
        labels = [row[0]["text"] for row in buttons]
        self.assertEqual(labels[:5], [
            f"✅ {NOTIFICATION_LABELS['success']}",
            f"❌ {NOTIFICATION_LABELS['error']}",
            f"✅ {NOTIFICATION_LABELS['auth']}",
            f"✅ {NOTIFICATION_LABELS['layout']}",
            f"❌ {NOTIFICATION_LABELS['system']}",
        ])
        self.assertEqual(labels[5], "❌ 全部通知")
        self.assertEqual(tuple(NOTIFICATION_LABELS), NOTIFICATION_ORDER)

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
        return Config({
            "thread": {"url": self.url},
            "selectors": {},
            "browser": {},
            "schedule": {"timezone": "Asia/Taipei", "state_file": "state.json"},
            "telegram": {},
            "cleanup": cleanup,
        })

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
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100, "previous_reply": None}}
        posts = [PostInfo(1, "50", True, "2026-01-01 00:00:00", "root")]
        done, page, state = self._run(state, posts, self._config())
        self.assertFalse(done)
        self.assertIn("pending_bump", state)

    def test_confirms_new_reply_without_cleanup(self):
        state = {"pending_bump": {"thread_url": self.url, "before_sn": 100, "previous_reply": None}}
        posts = [PostInfo(2, "150", True, "2026-01-05 09:00:00", "頂")]
        done, page, state = self._run(state, posts, self._config())
        self.assertTrue(done)
        self.assertEqual(state["last_confirmed_date"], "2026-01-05")
        self.assertNotIn("pending_bump", state)
        self.assertEqual(page.deleted, [])

    def test_deletes_pending_previous_reply(self):
        state = {"pending_bump": {
            "thread_url": self.url, "before_sn": 100,
            "previous_reply": {"floor": 2, "sn": "80"},
        }}
        posts = [
            PostInfo(2, "80", True, "2026-01-04 09:00:00", "頂"),
            PostInfo(3, "150", True, "2026-01-05 09:00:00", "頂"),
        ]
        done, page, state = self._run(state, posts, self._config(enabled=True, dry_run=False))
        self.assertTrue(done)
        self.assertEqual(page.deleted, ["80"])
        self.assertEqual(page.verified, ["80"])

    def test_already_gone_target_verified_not_redeleted(self):
        state = {"pending_bump": {
            "thread_url": self.url, "before_sn": 100, "posted_sn": "150", "posted_date": "2026-01-05",
            "previous_reply": {"floor": 2, "sn": "80"},
        }}
        posts = [PostInfo(3, "150", True, "2026-01-05 09:00:00", "頂")]
        done, page, state = self._run(state, posts, self._config(enabled=True, dry_run=False))
        self.assertTrue(done)
        self.assertEqual(page.deleted, [])
        self.assertEqual(page.verified, ["80"])

    def test_does_not_delete_a_different_reply(self):
        # Target 80 already gone, but a newer owned reply 90 must not be deleted.
        state = {"pending_bump": {
            "thread_url": self.url, "before_sn": 100, "posted_sn": "150", "posted_date": "2026-01-05",
            "previous_reply": {"floor": 2, "sn": "80"},
        }}
        posts = [
            PostInfo(2, "90", True, "2026-01-04 09:00:00", "頂"),
            PostInfo(3, "150", True, "2026-01-05 09:00:00", "頂"),
        ]
        done, page, state = self._run(state, posts, self._config(enabled=True, dry_run=False))
        self.assertTrue(done)
        self.assertEqual(page.deleted, [])

    def test_rejects_pending_from_other_thread(self):
        state = {"pending_bump": {"thread_url": "https://other", "before_sn": 1, "previous_reply": None}}
        with self.assertRaises(CannotConfirm):
            self._run(state, [], self._config())

    def test_rejects_cleanup_target_newer_than_before_sn(self):
        state = {"pending_bump": {
            "thread_url": self.url, "before_sn": 100,
            "previous_reply": {"floor": 2, "sn": "150"},
        }}
        with self.assertRaises(CannotConfirm):
            self._run(state, [], self._config(enabled=True, dry_run=False))


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
