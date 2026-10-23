"""Start, Info-Seite und neutraler Name in der Oberfläche."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

import catalytix  # noqa: E402
from catalytix import create_app  # noqa: E402
from catalytix.models import User, db, get_setting, set_setting  # noqa: E402


class StartupTests(unittest.TestCase):
    def test_new_database_file(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(catalytix._sqlite_file(d).name, "catalytix.db")

    def test_refuses_to_start_empty_when_other_database_exists(self):
        d = Path(tempfile.mkdtemp())
        (d / "alt.db").write_bytes(b"SQLite format 3\0" + b"x" * 100)
        with self.assertRaises(SystemExit) as cm:
            catalytix._sqlite_file(d)
        self.assertIn("alt.db", str(cm.exception))
        (d / "catalytix.db").write_bytes(b"")
        self.assertEqual(catalytix._sqlite_file(d).name, "catalytix.db")

    def test_old_default_library_name_is_migrated(self):
        d = tempfile.mkdtemp()
        uri = f"sqlite:///{d}/old.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            set_setting("library_name", "CATALYTIX")
            db.session.commit()
            db.session.remove()
        app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app2.app_context():
            self.assertEqual(get_setting("library_name"), "Bibliothek")
            db.session.remove()

    def test_custom_library_name_untouched(self):
        d = tempfile.mkdtemp()
        uri = f"sqlite:///{d}/old.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            set_setting("library_name", "Schulbibliothek")
            db.session.commit()
            db.session.remove()
        app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app2.app_context():
            self.assertEqual(get_setting("library_name"), "Schulbibliothek")
            db.session.remove()


class AboutPageTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username="admin", display_name="Admin", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_about_requires_login(self):
        self.assertIn("/login", self.c.get("/info").headers["Location"])
        self.assertIn("/login", self.c.get("/info/lizenz").headers["Location"])

    def test_about_shows_license_and_source(self):
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        os.environ["CATALYTIX_SOURCE_URL"] = "https://github.com/beispiel/catalytix"
        try:
            html = self.c.get("/info").get_data(as_text=True)
        finally:
            del os.environ["CATALYTIX_SOURCE_URL"]
        self.assertIn("AGPL", html)
        self.assertIn("https://github.com/beispiel/catalytix", html)
        self.assertIn(catalytix.__version__, html)
        lic = self.c.get("/info/lizenz")
        self.assertEqual(lic.status_code, 200)
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", lic.get_data(as_text=True))

    def test_login_page_reveals_no_version(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn(catalytix.__version__, html)
        self.assertNotIn("/info", html)


if __name__ == "__main__":
    unittest.main()


class NoProductNameTests(unittest.TestCase):
    """Der Projektname erscheint nicht in der Oberfläche – nur der Name der Bibliothek."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username="admin", display_name="Admin", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        set_setting("library_name", "Bibliothek des Ausbildungsinstituts")
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def visible_text(self, html):
        import re
        html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
        html = re.sub(r"<[^>]+>", " ", html)
        return html

    def test_name_not_shown_on_pages(self):
        login = self.c.get("/login").get_data(as_text=True)
        self.assertIn("Bibliothek des Ausbildungsinstituts", login)
        self.assertNotIn("catalytix", self.visible_text(login).lower())
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        for url in ["/", "/buecher", "/buecher/neu", "/profil", "/admin/einstellungen", "/admin/erscheinungsbild",
                    "/admin/verzeichnisdienst", "/admin/email"]:
            with self.subTest(url=url):
                html = self.c.get(url).get_data(as_text=True)
                self.assertNotIn("catalytix", self.visible_text(html).lower(), url)
                self.assertIn("Bibliothek des Ausbildungsinstituts", html)

    def test_mails_use_library_name(self):
        from catalytix.mailer import render_mail
        with self.app.test_request_context():
            subject, text, html = render_mail("test")
        for part in (subject, text, self.visible_text(html)):
            self.assertNotIn("catalytix", part.lower())
        self.assertIn("Bibliothek des Ausbildungsinstituts", html)

    def test_old_default_names_become_neutral(self):
        for old in ("CATALYTIX",):
            d = tempfile.mkdtemp()
            uri = f"sqlite:///{d}/alt.db"
            app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
            with app.app_context():
                set_setting("library_name", old)
                db.session.commit()
                db.session.remove()
            app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
            with app2.app_context():
                self.assertEqual(get_setting("library_name"), "Bibliothek")
                db.session.remove()
