"""3.1.7: the emergency commands reset-password and reset-2fa also lift a sign-in lock."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import lockout  # noqa: E402

OUTSIDE = {"REMOTE_ADDR": "203.0.113.5"}


class UnlockOnResetTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.runner = self.app.test_cli_runner()

    def lock(self):
        for _ in range(lockout.MAX_FAILS + 2):
            self.c.post("/login", data={"username": "admin", "password": "falsch-falsch"})
        self.assertTrue(lockout.locked_usernames() & {"admin"})

    def test_reset_password_lifts_the_lock(self):
        self.lock()
        r = self.runner.invoke(args=["reset-password", "admin"], input="NeuesPasswort123\nNeuesPasswort123\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Sign-in lock lifted.", r.output)
        self.assertFalse(lockout.locked_usernames() & {"admin"})
        r = self.c.post("/login", data={"username": "admin", "password": "NeuesPasswort123"})
        self.assertEqual(r.status_code, 302)                               # signed in right away
        self.assertNotIn("/login", r.headers["Location"])

    def test_reset_2fa_lifts_the_lock(self):
        self.lock()
        r = self.runner.invoke(args=["reset-2fa", "admin"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Sign-in lock lifted.", r.output)
        self.assertFalse(lockout.locked_usernames() & {"admin"})

    def test_no_lock_no_message(self):
        r = self.runner.invoke(args=["reset-password", "admin"], input="NeuesPasswort123\nNeuesPasswort123\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("lock lifted", r.output)

    def test_other_accounts_stay_locked(self):
        self.make_user("leser")
        for _ in range(lockout.MAX_FAILS + 2):
            self.c.post("/login", data={"username": "leser", "password": "falsch-falsch"})
        self.lock()
        self.runner.invoke(args=["reset-password", "admin"], input="NeuesPasswort123\nNeuesPasswort123\n")
        self.assertIn("leser", lockout.locked_usernames())


if __name__ == "__main__":
    unittest.main()
