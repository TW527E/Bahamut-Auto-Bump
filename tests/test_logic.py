import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bahamut_auto_bump import parse_post_time


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


if __name__ == "__main__":
    unittest.main()
