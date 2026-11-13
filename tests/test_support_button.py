"""3.0.6: "Buy me a coffee" – visible to admins only. 3.2.1: a card at the bottom of the overview instead of the
pill in the sidebar."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import SUPPORT_URL  # noqa: E402

LINK = 'class="coffee-btn" href="https://buymeacoffee.com/soleil0815"'


class SupportButtonTests(BaseCase):
    def page_for(self, role):
        self.make_user(role, role)
        self.login(role)
        r = self.c.get("/", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_admin_sees_card_on_the_overview(self):
        html = self.page_for("admin")
        self.assertIn('<section class="coffee-card"', html)
        self.assertIn(LINK, html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn("Gefällt Ihnen dieses Programm? Über einen Kaffee freue ich mich.", html)
        self.assertIn("Einen Kaffee spendieren", html)
        sidebar = html[:html.index('class="side-foot"')]
        self.assertNotIn("coffee-btn", sidebar)                    # no longer in the sidebar

    def test_only_on_the_overview(self):
        self.page_for("admin")
        for url in ("/buecher", "/admin/einstellungen", "/profil"):
            self.assertNotIn("buymeacoffee.com", self.c.get(url).get_data(as_text=True), url)

    def test_english(self):
        self.page_for("admin")
        self.c.post("/sprache/en", data={"next": "/"})
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("Enjoying this program? I'd be glad about a coffee.", html)
        self.assertIn("Buy me a coffee", html)

    def test_librarian_and_reader_do_not(self):
        for role in ("librarian", "reader"):
            with self.subTest(role=role):
                self.c = self.app.test_client()           # fresh session
                html = self.page_for(role)
                self.assertIn('class="side-foot"', html)   # the sidebar is there – only without the button
                self.assertNotIn("buymeacoffee.com", html)
                self.assertNotIn("coffee-btn", html)

    def test_not_shown_without_login(self):
        self.assertNotIn("buymeacoffee.com", self.c.get("/login").get_data(as_text=True))

    def test_url(self):
        self.assertEqual(SUPPORT_URL, "https://buymeacoffee.com/soleil0815")


if __name__ == "__main__":
    unittest.main()
