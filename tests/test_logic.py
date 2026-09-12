import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bahamut_auto_bump import (
    NOTIFICATION_LABELS,
    NOTIFICATION_ORDER,
    PostInfo,
    TelegramNotifier,
    deletion_candidates,
    parse_post_time,
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
        sent = []
        notifier._api = lambda method, params: sent.append(params)
        notifier.send("success", "頂文成功")
        notifier.send("auth", "Cookie 已失效")
        self.assertEqual(sent[0]["text"], "✅ 頂文成功")
        self.assertEqual(sent[1]["text"], "❌ Cookie 已失效")


if __name__ == "__main__":
    unittest.main()
