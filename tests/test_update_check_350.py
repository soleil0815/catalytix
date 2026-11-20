"""3.5.0: daily check for a newer version on GitHub; notice for admins on the overview (only once the release has been
out for a day – automatic updaters never see it); switch, status and "Jetzt prüfen" under Settings → Wartung."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

import requests  # noqa: E402

from test_app import BaseCase  # noqa: E402

from catalytix import __version__, updatecheck  # noqa: E402
from catalytix.models import User, db, get_setting, set_setting  # noqa: E402


def bump(v, i=1):
    a, b, c = (int(x) for x in v.split("."))
    return f"{a}.{b + i}.0"


NEWER = bump(__version__)
DAY_AGO = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
JUST_NOW = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def answer(tag=f"v{NEWER}", published=DAY_AGO, status=200, **extra):
    r = mock.Mock(status_code=status, headers=extra.pop("headers", {}))
    r.json.return_value = {"tag_name": tag, "published_at": published,
                           "html_url": f"https://github.com/soleil0815/catalytix/releases/tag/{tag}", **extra}
    return r


class CheckTests(BaseCase):
    def check(self, resp=None, side_effect=None):
        with mock.patch.object(updatecheck.requests, "get", return_value=resp, side_effect=side_effect) as get:
            res = updatecheck.check_now()
        return res, get

    def test_on_by_default(self):
        self.assertEqual(get_setting("update_check"), "1")

    def test_asks_github_without_credentials(self):
        res, get = self.check(answer())
        self.assertTrue(res["ok"])
        self.assertEqual(res["latest"], NEWER)
        url = get.call_args.args[0]
        self.assertEqual(url, "https://api.github.com/repos/soleil0815/catalytix/releases/latest")
        headers = get.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertIn("timeout", get.call_args.kwargs)

    def test_failure_is_recorded_and_keeps_known_version(self):
        self.check(answer())
        res, _ = self.check(side_effect=requests.ConnectionError("proxy http://user:pw@10.0.10.5:3128 refused"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "connection")
        self.assertEqual(res["latest"], NEWER)                    # still known
        self.assertNotIn("pw@", get_setting("update_check_result"))   # exception text is never stored

    def test_error_codes(self):
        self.assertEqual(self.check(side_effect=requests.Timeout())[0]["error"], "timeout")
        self.assertEqual(self.check(answer(status=403, headers={"X-RateLimit-Remaining": "0"}))[0]["error"],
                         "ratelimit")
        res = self.check(answer(status=502))[0]
        self.assertEqual((res["error"], res["detail"]), ("http", "502"))
        self.assertEqual(self.check(answer(tag="nightly"))[0]["error"], "answer")
        self.assertEqual(self.check(answer(prerelease=True))[0]["error"], "answer")

    def test_foreign_link_replaced(self):
        r = answer()
        r.json.return_value["html_url"] = "https://example.org/evil"
        self.assertEqual(self.check(r)[0]["url"], updatecheck.RELEASES_URL)

    def test_daily_only_once_and_switch(self):
        now = datetime(2026, 11, 20, 3, 0)
        with mock.patch.object(updatecheck.requests, "get", return_value=answer()) as get:
            updatecheck.run_daily(now)
            updatecheck.run_daily(now + timedelta(hours=2))
            self.assertEqual(get.call_count, 1)
            updatecheck.run_daily(now + timedelta(days=1))
            self.assertEqual(get.call_count, 2)
            set_setting("update_check", "0")
            db.session.commit()
            self.assertIsNone(updatecheck.run_daily(now + timedelta(days=2)))
            self.assertEqual(get.call_count, 2)

    def test_version_compare(self):
        self.assertTrue(updatecheck.newer_than_running(NEWER))
        self.assertFalse(updatecheck.newer_than_running(__version__))
        self.assertFalse(updatecheck.newer_than_running("1.0.0"))
        self.assertFalse(updatecheck.newer_than_running("x"))
        self.assertEqual(updatecheck.parse("v3.10.2"), (3, 10, 2))      # numeric, not text: 3.10 > 3.9


class NoticeTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")
        self.reader = self.make_user("leser")

    def known(self, latest=NEWER, published=DAY_AGO):
        set_setting("update_check_result", json.dumps({"ok": True, "checked": "2026-11-20T03:00:00",
                                                      "latest": latest, "published": published,
                                                      "url": f"https://github.com/soleil0815/catalytix/releases/tag/v{latest}"}))
        db.session.commit()

    def overview(self, who="admin"):
        self.login(who)
        return self.c.get("/").get_data(as_text=True)

    def test_admin_sees_notice(self):
        self.known()
        page = self.overview()
        self.assertIn("data-update-notice", page)
        self.assertIn(f"Version {NEWER} ist verfügbar", page)
        self.assertIn("./update.sh --pull", page)
        self.assertNotIn("Catalytix", page.split("data-update-notice", 1)[1].split("</form>", 1)[0])

    def test_readers_never(self):
        self.known()
        self.assertNotIn("data-update-notice", self.overview("leser"))

    def test_not_in_first_day(self):
        self.known(published=JUST_NOW)          # automatic updaters are on it by tomorrow
        self.assertNotIn("data-update-notice", self.overview())

    def test_not_when_current_or_switched_off(self):
        self.known(latest=__version__)
        self.assertNotIn("data-update-notice", self.overview())
        self.known()
        set_setting("update_check", "0")
        db.session.commit()
        self.assertNotIn("data-update-notice", self.overview())

    def test_hide_until_next_version(self):
        self.known()
        self.login("admin")
        self.assertEqual(self.c.post("/versionshinweis/aus").status_code, 302)
        self.assertEqual(db.session.get(User, self.admin.id).update_hidden, NEWER)
        self.assertNotIn("data-update-notice", self.overview())
        self.known(latest=bump(__version__, 2))
        self.assertIn("data-update-notice", self.overview())

    def test_hide_only_for_admins(self):
        self.known()
        self.login("leser")
        self.c.post("/versionshinweis/aus")
        self.assertIsNone(db.session.get(User, self.reader.id).update_hidden)


class MaintenanceTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.login("admin")

    def test_status_and_switch(self):
        page = self.c.get("/admin/einstellungen?bereich=wartung").get_data(as_text=True)
        self.assertIn('id="versionen"', page)
        self.assertIn('name="update_check"', page)
        self.assertIn(__version__, page)
        self.c.post("/admin/einstellungen", data={"bereich": "wartung"})
        self.assertEqual(get_setting("update_check"), "0")
        self.c.post("/admin/einstellungen", data={"bereich": "wartung", "update_check": "1"})
        self.assertEqual(get_setting("update_check"), "1")

    def test_check_now(self):
        with mock.patch.object(updatecheck.requests, "get", return_value=answer()):
            r = self.c.post("/admin/versionspruefung", follow_redirects=True)
        self.assertIn(f"Version {NEWER} ist verfügbar.", r.get_data(as_text=True))
        with mock.patch.object(updatecheck.requests, "get", side_effect=requests.ConnectionError()):
            page = self.c.post("/admin/versionspruefung", follow_redirects=True).get_data(as_text=True)
        self.assertIn("GitHub nicht erreichbar", page)

    def test_check_now_admins_only(self):
        self.make_user("leser")
        self.login("leser")
        with mock.patch.object(updatecheck.requests, "get") as get:
            r = self.c.post("/admin/versionspruefung")
        self.assertIn(r.status_code, (302, 403))
        get.assert_not_called()

    def test_in_diagnostic_package(self):
        from catalytix import diagnostics
        self.assertIn("update_check_result", diagnostics.PLAIN)


if __name__ == "__main__":
    unittest.main()
