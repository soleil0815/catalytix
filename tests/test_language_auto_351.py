"""3.5.1: "Automatisch" removes a fixed language again; the default-language setting says when the admin's own
account is fixed to another language (a fixed choice always wins); the diagnostic package counts languages per
role."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix.models import User, db, set_setting  # noqa: E402


class LanguageAutoTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")
        self.login("admin")

    def lang(self):
        return db.session.get(User, self.admin.id).language

    def page(self, url, **hdr):
        from flask import g
        g.pop("_catalytix_locale", None)     # the test keeps one app context open; production has one per request
        return self.c.get(url, headers=hdr).get_data(as_text=True)

    def test_auto_clears_fixed_choice(self):
        self.c.post("/sprache/en")
        self.assertEqual(self.lang(), "en")
        self.assertIn("Sign out", self.page("/", **{"Accept-Language": "de"}))      # fixed wins over browser
        self.c.post("/sprache/auto")
        self.assertEqual(self.lang(), "")
        self.assertIn("Abmelden", self.page("/", **{"Accept-Language": "de"}))
        self.assertIn("Sign out", self.page("/", **{"Accept-Language": "en"}))      # now follows the browser

    def test_auto_without_sign_in_only_drops_session_choice(self):
        self.c.post("/logout")
        self.c.post("/sprache/en")
        self.assertIn("Sign in", self.page("/login", **{"Accept-Language": "de"}))
        self.c.post("/sprache/auto")
        self.assertIn("Anmelden", self.page("/login", **{"Accept-Language": "de"}))

    def test_unknown_code_404(self):
        self.assertEqual(self.c.post("/sprache/fr").status_code, 404)

    def test_profile_shows_choice_and_effect(self):
        page = self.page("/profil")
        self.assertIn("/sprache/auto", page)
        self.assertIn("Automatisch: Sie sehen die Sprache Ihres Browsers", page)
        auto_btn = page.split("/sprache/auto", 1)[1].split("</button>", 1)[0]
        self.assertIn('class="on"', auto_btn)
        self.c.post("/sprache/de")
        page = self.page("/profil")
        self.assertIn("Fest eingestellt: Deutsch", page)
        self.assertNotIn('class="on"', page.split("/sprache/auto", 1)[1].split("</button>", 1)[0])

    def test_topbar_keeps_two_buttons(self):
        page = self.page("/")
        top = page.split('class="topbar"', 1)[1].split("user-menu", 1)[0]
        self.assertNotIn("/sprache/auto", top)
        self.assertIn("/sprache/de", top)

    def test_settings_hint_only_when_own_language_differs(self):
        set_setting("default_language", "de")
        db.session.commit()
        url = "/admin/einstellungen?bereich=allgemein"
        self.assertNotIn("data-own-language", self.page(url))          # automatic
        self.c.post("/sprache/de")
        self.assertNotIn("data-own-language", self.page(url))          # same as default
        self.c.post("/sprache/en")
        page = self.page(url)
        self.assertIn("data-own-language", page)
        self.assertIn("/profil#sprache", page)

    def test_diagnostic_counts_per_role(self):
        from catalytix import diagnostics
        self.make_user("leser")
        self.c.post("/sprache/en")
        counts = diagnostics._counts(30)
        per_role = counts["konten"]["sprache_nach_rolle"]
        self.assertEqual(per_role["admin"], {"en": 1})
        self.assertEqual(per_role["reader"], {"-": 1})
        self.assertNotIn("admin", str(per_role.values()))     # no names, only roles and language codes


if __name__ == "__main__":
    unittest.main()
