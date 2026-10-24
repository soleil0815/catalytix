"""Neuigkeiten in der App: Inhalt, Zielgruppen, „gesehen“-Stand und dezente Hinweise."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import __version__, create_app, news  # noqa: E402
from catalytix.models import User, db  # noqa: E402
from pathlib import Path  # noqa: E402

PW = "geheim12345"
NEWS = Path(__file__).resolve().parent.parent / "catalytix" / "news.json"


class NewsFileTests(unittest.TestCase):
    def test_entries_are_complete(self):
        data = json.loads(NEWS.read_text(encoding="utf-8"))
        versions = [news.vtuple(e["version"]) for e in data]
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(set(versions)), len(versions))
        for entry in data:
            self.assertLessEqual(news.vtuple(entry["version"]), news.vtuple(__version__))
            self.assertTrue(entry["items"], entry["version"])
            for item in entry["items"]:
                self.assertIn(item["kind"], news.KINDS)
                self.assertIn(item["audience"], news.AUDIENCES)
                for lang in ("de", "en"):
                    self.assertTrue(item[lang]["title"].strip() and item[lang]["text"].strip())
                text = json.dumps(item).lower()
                self.assertNotIn("catalytix", text)         # Programmname nur auf der Info-Seite

    def test_current_version_has_news(self):
        self.assertEqual(json.loads(NEWS.read_text(encoding="utf-8"))[0]["version"], __version__)


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from flask import g

        def _fresh():
            g.pop("_login_user", None)
            g.pop("_catalytix_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        for name, role in (("admin", "admin"), ("bib", "librarian"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local")
            u.set_password(PW)
            db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()
        news._entries.cache_clear()

    def tearDown(self):
        news._entries.cache_clear()
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def user(self, name):
        return User.query.filter_by(username=name).one()

    def fake(self, entries):
        news._entries.cache_clear()
        return mock.patch.object(news, "_entries", return_value=tuple(entries))

    def item(self, kind="new", audience="all", title="Etwas Neues"):
        return {"kind": kind, "audience": audience, "de": {"title": title, "text": "Text"},
                "en": {"title": title + " EN", "text": "Text"}}

    def test_new_account_starts_without_old_news(self):
        self.assertEqual(self.user("leser").news_seen, __version__)
        self.login("leser")
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("news-hint", html)
        self.assertNotIn("news-dot", html)
        self.assertIn("/neuigkeiten", html)                  # Link bleibt dezent in der Seitenleiste

    def test_existing_account_sees_only_this_version(self):
        leser = self.user("leser")
        leser.news_seen = None                                # Konto von vor dieser Funktion
        db.session.commit()
        entries = news.for_user(leser, "de")
        unseen = [e["version"] for e in entries if e["unseen"]]
        self.assertIn(news.INTRODUCED, unseen)                    # auch bei einem Sprung über mehrere Versionen
        self.assertTrue(all(news.vtuple(v) >= news.vtuple(news.INTRODUCED) for v in unseen))
        self.assertTrue(any(not e["unseen"] for e in entries))     # ältere Einträge nur zum Nachlesen

    def test_hint_dot_and_mark_seen(self):
        with self.fake([{"version": __version__, "date": "2026-10-24",
                         "items": [self.item(), self.item("security", title="Sicherer")]}]):
            leser = self.user("leser")
            leser.news_seen = "1.0.0"
            db.session.commit()
            self.login("leser")
            html = self.c.get("/").get_data(as_text=True)
            self.assertIn("news-hint", html)
            self.assertIn("Sicherer", html)                   # Sicherheit steht vorne
            self.assertIn("und 1 weitere Neuigkeit", html)
            self.assertIn("news-dot", html)
            page = self.c.get("/neuigkeiten").get_data(as_text=True)
            self.assertIn("neu für dich", page)
            self.assertEqual(self.user("leser").news_seen, __version__)
            html = self.c.get("/").get_data(as_text=True)
            self.assertNotIn("news-hint", html)
            self.assertNotIn("news-dot", html)

    def test_dismiss(self):
        with self.fake([{"version": __version__, "items": [self.item()]}]):
            leser = self.user("leser")
            leser.news_seen = "1.0.0"
            db.session.commit()
            self.login("leser")
            r = self.c.post("/neuigkeiten/gelesen")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.user("leser").news_seen, __version__)
            self.assertNotIn("news-hint", self.c.get("/").get_data(as_text=True))

    def test_dismiss_needs_post(self):
        self.login("leser")
        self.assertEqual(self.c.get("/neuigkeiten/gelesen").status_code, 405)

    def test_audience(self):
        entry = {"version": __version__, "items": [self.item(title="Für alle"),
                                                   self.item(audience="librarian", title="Für die Theke"),
                                                   self.item("security", "admin", title="Nur Admin")]}
        with self.fake([entry, {"version": "0.1.0", "items": [self.item(audience="admin", title="Alt")]}]):
            def titles(name):
                return [i["title"] for e in news.for_user(self.user(name), "de") for i in e["items"]]
            self.assertEqual(titles("leser"), ["Für alle"])
            self.assertEqual(titles("bib"), ["Für alle", "Für die Theke"])
            self.assertEqual(titles("admin"), ["Nur Admin", "Für alle", "Für die Theke", "Alt"])
            # eine Version nur mit Admin-Themen zeigt Lesern keinen Punkt
            leser = self.user("leser")
            leser.news_seen = "0.0.1"
            db.session.commit()
            with self.fake([{"version": __version__, "items": [self.item(audience="admin")]}]):
                self.assertFalse(news.has_unseen(leser))
                admin = self.user("admin")
                admin.news_seen = "0.0.1"
                self.assertTrue(news.has_unseen(admin))

    def test_admin_only_news_hidden_from_reader_page(self):
        with self.fake([{"version": __version__, "items": [self.item("security", "admin", title="Nur Admin")]}]):
            self.login("leser")
            self.assertNotIn("Nur Admin", self.c.get("/neuigkeiten").get_data(as_text=True))
            self.login("admin")
            self.assertIn("Nur Admin", self.c.get("/neuigkeiten").get_data(as_text=True))

    def test_future_entries_ignored(self):
        data = [{"version": "999.0.0", "items": [self.item(title="Zukunft")]},
                {"version": __version__, "items": [self.item()]}]
        with mock.patch.object(Path, "read_text", return_value=json.dumps(data)):
            news._entries.cache_clear()
            self.assertEqual([e["version"] for e in news._entries()], [__version__])
        news._entries.cache_clear()

    def test_english(self):
        with self.fake([{"version": __version__, "date": "2026-10-24", "items": [self.item()]}]):
            self.login("leser")
            self.c.post("/sprache/en")
            page = self.c.get("/neuigkeiten").get_data(as_text=True)
            self.assertIn("Etwas Neues EN", page)
            self.assertIn("What's new", page.replace("&#39;", "'"))
            self.assertIn("24 Oct 2026", page)

    def test_needs_login(self):
        self.assertEqual(self.c.get("/neuigkeiten").status_code, 302)

    def test_column_added_to_existing_database_is_null(self):
        from catalytix.models import _add_column_ddl
        col = User.__table__.c.news_seen
        ddl = _add_column_ddl(User.__table__, col, db.engine.dialect)
        self.assertEqual(len(ddl), 1)
        self.assertNotIn("DEFAULT", ddl[0])                  # bestehende Konten: NULL → sehen die neue Version


if __name__ == "__main__":
    unittest.main()
