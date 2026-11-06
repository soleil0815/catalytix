"""3.0.8: automatic sign-out after a time without activity (catalytix/autologout.py)."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import autologout  # noqa: E402
from catalytix.models import db, set_setting  # noqa: E402

OUTSIDE = {"REMOTE_ADDR": "203.0.113.5"}   # documentation address – counts as "outside"


class AutoLogoutCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")         # setup done
        self.make_user("leser", "reader")

    def set(self, **values):
        for k, v in values.items():
            set_setting(k, v)
        db.session.commit()

    def idle(self, seconds):
        """Pretend the last activity was `seconds` ago."""
        with self.c.session_transaction() as s:
            s[autologout.SEEN_KEY] = int(time.time() - seconds)

    def seen(self):
        with self.c.session_transaction() as s:
            return s.get(autologout.SEEN_KEY)

    def signed_in(self, **kw):
        r = self.c.get("/", **kw)
        return r.status_code == 200, r


class LimitTests(AutoLogoutCase):
    def test_everyone_default_12_hours(self):
        self.login("leser")
        self.idle(11 * 3600)
        self.assertTrue(self.signed_in()[0])
        self.idle(12 * 3600 + 10)
        ok, r = self.signed_in()
        self.assertFalse(ok)
        self.assertIn("/login", r.headers["Location"])
        page = self.c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("nach 12 Stunden ohne Aktivität automatisch abgemeldet", page)

    def test_admin_default_60_minutes(self):
        self.login("admin")
        self.idle(59 * 60)
        self.assertTrue(self.signed_in()[0])
        self.idle(61 * 60)
        ok, r = self.signed_in()
        self.assertFalse(ok)
        self.assertIn("nach 1 Stunde ohne Aktivität", self.c.get(r.headers["Location"]).get_data(as_text=True))

    def test_admin_limit_empty_means_general(self):
        self.set(session_admin_minutes="")
        self.login("admin")
        self.idle(5 * 3600)
        self.assertTrue(self.signed_in()[0])

    def test_external_limit_only_from_outside(self):
        self.set(session_external_minutes="30")
        self.c.post("/login", data={"username": "leser", "password": "geheim123"}, environ_base=OUTSIDE)
        self.idle(29 * 60)
        self.assertTrue(self.signed_in(environ_base=OUTSIDE)[0])
        self.idle(31 * 60)
        self.assertFalse(self.signed_in(environ_base=OUTSIDE)[0])
        # internal: the general limit
        self.login("leser")
        self.idle(31 * 60)
        self.assertTrue(self.signed_in()[0])

    def test_activity_resets_the_clock(self):
        self.login("leser")
        self.idle(11 * 3600)
        self.c.get("/")
        self.assertGreater(self.seen(), time.time() - 60)

    def test_general_hours_setting_and_env_start_value(self):
        with mock.patch.dict(os.environ, {"CATALYTIX_SESSION_HOURS": "3"}):
            self.assertEqual(autologout.general_hours(), 3)
            self.set(session_hours="5")
            self.assertEqual(autologout.general_hours(), 5)       # the setting wins once saved
        for bad in ("0", "999", "abc", ""):
            with mock.patch.dict(os.environ, {"CATALYTIX_SESSION_HOURS": bad}):
                self.assertEqual(autologout.env_hours(), 12)

    def test_shortest_limit_applies(self):
        self.set(session_hours="1", session_admin_minutes="120", session_external_minutes="20")

        class Admin:
            is_admin = True
        self.assertEqual(autologout.limit_seconds(Admin(), True), 3600)       # admin limit longer: general
        self.assertEqual(autologout.limit_seconds(Admin(), False), 20 * 60)   # outside is shortest


class ApiTests(AutoLogoutCase):
    def test_status_does_not_count_as_activity(self):
        self.login("leser")
        self.idle(100)
        before = self.seen()
        data = self.c.get("/api/sitzung").get_json()
        self.assertEqual(self.seen(), before)
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(data["seconds_left"], 12 * 3600 - 100, delta=5)
        self.assertEqual(data["warn_seconds"], 120)

    def test_keepalive_counts(self):
        self.login("leser")
        self.idle(11 * 3600)
        data = self.c.post("/api/sitzung/weiter").get_json()
        self.assertGreater(data["seconds_left"], 12 * 3600 - 10)

    def test_expired_api_request_gets_json_401(self):
        self.login("admin")
        self.idle(2 * 3600)
        r = self.c.get("/api/sitzung")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["signed_out"])
        self.assertFalse(self.signed_in()[0])          # really signed out


class RememberTests(AutoLogoutCase):
    def login_remember(self, name, **kw):
        self.c.post("/logout")
        return self.c.post("/login", data={"username": name, "password": "geheim123", "remember": "1"}, **kw)

    def test_reader_may_stay_signed_in(self):
        self.login_remember("leser")
        self.assertIsNotNone(self.c.get_cookie("remember_token"))

    def test_admin_with_own_limit_may_not(self):
        self.login_remember("admin")
        self.assertIsNone(self.c.get_cookie("remember_token"))

    def test_outside_with_own_limit_may_not(self):
        self.set(session_external_minutes="30")
        self.login_remember("leser", environ_base=OUTSIDE)
        self.assertIsNone(self.c.get_cookie("remember_token"))

    def test_restored_admin_session_is_ended(self):
        # remember cookie from before the admin limit was set
        self.set(session_admin_minutes="")
        self.login_remember("admin")
        self.assertIsNotNone(self.c.get_cookie("remember_token"))
        self.set(session_admin_minutes="60")
        self.c.delete_cookie("session")                # browser restart: only the remember cookie is left
        ok, r = self.signed_in()
        self.assertFalse(ok)
        self.assertIn("/login", r.headers["Location"])

    def test_restored_reader_session_continues(self):
        self.login_remember("leser")
        self.c.delete_cookie("session")
        self.assertTrue(self.signed_in()[0])

    def test_browser_close_option(self):
        self.set(session_browser_close="1")
        self.assertNotIn('name="remember"', self.c.get("/login").get_data(as_text=True))
        r = self.login_remember("leser")
        self.assertIsNone(self.c.get_cookie("remember_token"))
        cookie = " ".join(v for k, v in r.headers.items() if k == "Set-Cookie" and v.startswith("session="))
        self.assertNotIn("Expires=", cookie)            # ends with the browser

    def test_session_cookie_has_expiry_by_default(self):
        r = self.login_remember("leser")
        cookie = " ".join(v for k, v in r.headers.items() if k == "Set-Cookie" and v.startswith("session="))
        self.assertIn("Expires=", cookie)
        self.assertIn('name="remember"', (self.c.post("/logout"), self.c.get("/login"))[1].get_data(as_text=True))


class PageTests(AutoLogoutCase):
    def test_warning_dialog(self):
        self.login("admin")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn('id="idle-dialog" data-idle-seconds="3600" data-idle-warn="120"', html)
        self.assertIn("Du wirst in 2 Minuten abgemeldet – weiterarbeiten?", html)
        self.set(session_warning="0")
        self.assertNotIn("idle-dialog", self.c.get("/").get_data(as_text=True))

    def test_settings_save(self):
        self.login("admin")
        r = self.c.post("/admin/einstellungen", data={
            "session_hours": "8", "session_admin_minutes": "900", "session_external_minutes": "",
            "session_warning": "1"}, follow_redirects=True)
        from catalytix.models import get_setting
        self.assertEqual(get_setting("session_hours"), "8")
        self.assertEqual(get_setting("session_admin_minutes"), "720")      # clamped to 5–720
        self.assertEqual(get_setting("session_external_minutes"), "")
        self.assertEqual(get_setting("session_browser_close"), "0")
        self.assertIn("Die Frist für Admins ist länger als die für alle", r.get_data(as_text=True))
        page = self.c.get("/admin/einstellungen").get_data(as_text=True)
        self.assertIn('name="session_hours" min="1" max="168" required value="8"', page)

    def test_other_settings_forms_leave_it_alone(self):
        self.set(session_hours="6")
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"library_name": "Bibliothek"})
        from catalytix.models import get_setting
        self.assertEqual(get_setting("session_hours"), "6")


if __name__ == "__main__":
    unittest.main()
