import json
import os
import tempfile
import unittest
from pathlib import Path

import bahamut_auto_bump as mod
from bahamut_auto_bump import AntiBotChallenge, CannotConfirm, ConfigError


class FakeContext:
    def __init__(self):
        self.added = []

    def add_cookies(self, cookies):
        self.added.append(cookies)


class FakePage:
    def __init__(self):
        self.context = FakeContext()


class ProfileSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profile = root / "profile"
        self.profile.mkdir()
        self.session = root / "session.json"
        self.config = {
            "thread": {"url": "https://forum.gamer.com.tw/C.php?bsn=1&snA=2"},
            "browser": {"user_data_dir": str(self.profile), "storage_state": str(self.session)},
            "schedule": {"state_file": str(root / "state.json"), "timezone": "Asia/Taipei"},
        }
        self.saved = (mod._authenticated_thread_page, mod.login_from_homepage, mod.verify_imported_session)
        self.saved_save = mod._save_session
        mod._save_session = lambda page, config: None
        self.checks = []

    def tearDown(self):
        mod._authenticated_thread_page, mod.login_from_homepage, mod.verify_imported_session = self.saved
        mod._save_session = self.saved_save
        self.tmp.cleanup()

    def _thread_check(self, outcomes):
        """Each call pops the next outcome: None passes, an exception is raised."""
        def check(page, config):
            self.checks.append(1)
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome
        mod._authenticated_thread_page = check

    def test_signed_in_profile_needs_no_import_or_login(self):
        self.session.write_text(json.dumps({"cookies": [{"name": "a"}]}))
        self._thread_check([None])
        mod.login_from_homepage = lambda page, config: self.fail("login must not run")
        mod.verify_imported_session = lambda page, config: self.fail("storage_state path must not run")
        page = FakePage()
        mod.authenticate(page, self.config)
        self.assertEqual(page.context.added, [])
        self.assertEqual(len(self.checks), 1)

    def test_working_session_file_is_imported_again_after_restart(self):
        # Chrome drops session cookies on exit, so the same file must be
        # usable on the next run, not only the first time it is seen.
        self.session.write_text(json.dumps({"cookies": [{"name": "a", "expires": -1}]}))
        for _ in range(2):
            self._thread_check([CannotConfirm("signed out"), None])
            page = FakePage()
            mod.verify_profile_session(page, self.config)
            self.assertEqual(page.context.added, [[{"name": "a", "expires": -1}]])

    def test_session_file_that_does_not_sign_in_is_skipped_until_replaced(self):
        self.session.write_text(json.dumps({"cookies": [{"name": "a"}]}))
        self._thread_check([CannotConfirm("expired"), CannotConfirm("expired")])
        first = FakePage()
        with self.assertRaises(CannotConfirm):
            mod.verify_profile_session(first, self.config)
        self.assertEqual(first.context.added, [[{"name": "a"}]])
        # Same file again: not re-imported, one page load instead of two.
        self._thread_check([CannotConfirm("expired")])
        again = FakePage()
        with self.assertRaises(CannotConfirm):
            mod.verify_profile_session(again, self.config)
        self.assertEqual(again.context.added, [])
        # A replaced file (new mtime) is imported again.
        self.session.write_text(json.dumps({"cookies": [{"name": "b"}]}))
        os.utime(self.session, ns=(1, self.session.stat().st_mtime_ns + 1_000_000))
        self._thread_check([CannotConfirm("expired"), None])
        third = FakePage()
        mod.verify_profile_session(third, self.config)
        self.assertEqual(third.context.added, [[{"name": "b"}]])

    def test_challenge_after_import_does_not_reject_the_file(self):
        self.session.write_text(json.dumps({"cookies": [{"name": "a"}]}))
        self._thread_check([CannotConfirm("signed out"), AntiBotChallenge("challenged")])
        with self.assertRaises(AntiBotChallenge):
            mod.verify_profile_session(FakePage(), self.config)
        self._thread_check([CannotConfirm("signed out"), None])
        page = FakePage()
        mod.verify_profile_session(page, self.config)
        self.assertEqual(page.context.added, [[{"name": "a"}]])

    def test_challenge_is_not_treated_as_signed_out(self):
        self.session.write_text(json.dumps({"cookies": [{"name": "a"}]}))
        self._thread_check([AntiBotChallenge("challenged")])
        mod.login_from_homepage = lambda page, config: self.fail("login must not run")
        page = FakePage()
        with self.assertRaises(AntiBotChallenge):
            mod.authenticate(page, self.config)
        self.assertEqual(page.context.added, [])

    def test_signed_out_profile_falls_back_to_homepage_login(self):
        self._thread_check([CannotConfirm("expired")])
        logins = []
        mod.login_from_homepage = lambda page, config: logins.append(1)
        mod.authenticate(FakePage(), self.config)
        self.assertEqual(logins, [1])

    def test_malformed_session_file_is_ignored(self):
        self.session.write_text("not json")
        self.assertFalse(mod._import_session_file(FakePage(), self.config))


class ManualLoginTests(unittest.TestCase):
    def test_requires_profile(self):
        with self.assertRaises(ConfigError):
            mod.manual_login({"browser": {}, "thread": {"url": "https://example.invalid/"}})

    def test_command_uses_profile_basic_store_and_proxy(self):
        config = {
            "thread": {"url": "https://forum.gamer.com.tw/C.php?bsn=1&snA=2"},
            "browser": {
                "user_data_dir": "/srv/profile",
                "executable_path": "/usr/bin/google-chrome-stable",
                "proxy": "socks5://127.0.0.1:1080",
            },
        }
        command = mod.manual_login_command(config)
        self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
        self.assertIn("--user-data-dir=/srv/profile", command)
        self.assertIn("--password-store=basic", command)
        self.assertIn("--proxy-server=socks5://127.0.0.1:1080", command)
        self.assertEqual(command[-1], config["thread"]["url"])
        self.assertFalse(any("remote-debugging" in part or "enable-automation" in part for part in command))


if __name__ == "__main__":
    unittest.main()
