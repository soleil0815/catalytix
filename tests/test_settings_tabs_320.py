"""3.2.0: the settings page in tabs – one form per tab, each saves only its own fields."""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix.models import db, get_setting, set_setting  # noqa: E402
from catalytix.views.admin import SETTINGS_ANCHORS, SETTINGS_TABS  # noqa: E402

# a field of every tab that must appear only there
FIELDS = {"allgemein": "library_name", "ausleihe": "loan_days", "katalog": "reading_sample_google",
          "konten": "registration_mode", "sicherheit": "mfa_policy", "datenschutz": "retention_months"}


class SettingsTabsTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.login("admin")

    def page(self, tab=None):
        url = "/admin/einstellungen" + (f"?bereich={tab}" if tab else "")
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)

    def test_every_tab_shows_only_its_fields(self):
        self.assertEqual(list(SETTINGS_TABS), list(FIELDS))
        for tab in FIELDS:
            html = self.page(tab)
            self.assertIn(f'<input type="hidden" name="bereich" value="{tab}">', html)
            for other, field in FIELDS.items():
                self.assertEqual(f'name="{field}"' in html, other == tab, (tab, field))
            start = html.index('<nav class="tabs"')
            nav = html[start:html.index("</nav>", start)]
            self.assertEqual(nav.count("<a "), 6)
            self.assertEqual(len(re.findall(r'class="on" aria-current="page"', nav)), 1)
            self.assertIn(f'href="/admin/einstellungen?bereich={tab}" class="on"', nav)

    def test_default_and_unknown_tab(self):
        self.assertIn('name="library_name"', self.page())
        self.assertIn('name="library_name"', self.page("quatsch"))

    def test_post_without_tab_is_refused(self):
        r = self.c.post("/admin/einstellungen", data={"loan_days": "3"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(get_setting("loan_days"), "28")

    def test_saving_one_tab_leaves_the_others(self):
        for key, value in (("self_checkout", "1"), ("reservations_enabled", "1"), ("registration_verify_email", "1"),
                           ("session_warning", "1"), ("reading_sample_google", "1"), ("retention_months", "24"),
                           ("privacy_url", "https://institut.example/ds"), ("mfa_internal", "skip")):
            set_setting(key, value)
        db.session.commit()
        before = {k: get_setting(k) for k in ("self_checkout", "reservations_enabled", "registration_verify_email",
                                              "session_warning", "reading_sample_google", "retention_months",
                                              "privacy_url", "mfa_internal", "isbn_sources")}
        r = self.c.post("/admin/einstellungen", data={"bereich": "allgemein", "library_name": "Stadtbücherei"})
        self.assertEqual(r.headers["Location"], "/admin/einstellungen?bereich=allgemein")
        self.assertEqual(get_setting("library_name"), "Stadtbücherei")
        self.assertEqual({k: get_setting(k) for k in before}, before)

    def test_unticked_box_switches_off_in_its_own_tab(self):
        set_setting("self_checkout", "1")
        db.session.commit()
        self.c.post("/admin/einstellungen", data={"bereich": "ausleihe", "loan_days": "14"})
        self.assertEqual(get_setting("self_checkout"), "0")
        self.assertEqual(get_setting("loan_days"), "14")

    def test_retention_run_returns_to_privacy_tab(self):
        r = self.c.post("/admin/aufbewahrung/jetzt")
        self.assertEqual(r.headers["Location"], "/admin/einstellungen?bereich=datenschutz")

    def test_old_anchors_lead_to_their_tab(self):
        html = self.page()
        raw = re.search(r'data-settings-anchors="([^"]*)"', html).group(1).replace("&#34;", '"')
        self.assertEqual(json.loads(raw), SETTINGS_ANCHORS)
        for anchor, tab in SETTINGS_ANCHORS.items():
            self.assertIn(tab, SETTINGS_TABS)
            self.assertIn(f'id="{anchor}"', self.page(tab), anchor)
        app_js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "catalytix", "static", "js", "app.js"), encoding="utf-8").read()
        self.assertIn("data-settings-anchors", app_js)

    def test_english_labels(self):
        self.c.post("/sprache/en", data={"next": "/"})
        html = self.page()
        nav = html[html.index('<nav class="tabs"'):]
        nav = nav[:nav.index("</nav>")]
        for label in ("General", "Lending", "Catalogue", "Accounts", "Security", "Privacy"):
            self.assertIn(label, nav)


if __name__ == "__main__":
    unittest.main()
