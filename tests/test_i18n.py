"""Multilingual support and help: catalogue complete, language choice, e-mails, role-dependent help."""
import ast
import json
import os
import re
import string
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app, i18n  # noqa: E402
from catalytix.models import User, db, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "catalytix"
PW = "geheim12345"

_STR = r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\''
TPL_GETTEXT = re.compile(r"\b_\(\s*(?:" + _STR + r")")
TPL_PGETTEXT = re.compile(r"\b_p\(\s*\"(\w+)\"\s*,\s*(?:" + _STR + r")")
TPL_NGETTEXT = re.compile(r"\b_n\(\s*(?:" + _STR + r")\s*,\s*(?:" + _STR + r")")


def _unescape(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in s else s


def template_msgids() -> set[str]:
    ids = set()
    for f in (PKG / "templates").rglob("*"):
        if f.suffix not in (".html", ".txt") or (f.parent.parent.name == "help" and f.parent.name in ("de", "en")):
            continue   # help texts exist as separate files per language
        text = f.read_text(encoding="utf-8")
        for m in TPL_GETTEXT.finditer(text):
            ids.add(_unescape(m.group(1) if m.group(1) is not None else m.group(2)))
        for m in TPL_PGETTEXT.finditer(text):
            ids.add(_unescape(m.group(2) if m.group(2) is not None else m.group(3)) + "|" + m.group(1))
        for m in TPL_NGETTEXT.finditer(text):
            for a, b in ((1, 2), (3, 4)):
                v = m.group(a) if m.group(a) is not None else m.group(b)
                ids.add(_unescape(v))
    return ids


def python_msgids() -> set[str]:
    ids = set()
    for f in PKG.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "_l", "_n", "gettext", "lazy_gettext"):
                n = 2 if node.func.id == "_n" else 1
                for arg in node.args[:n]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        ids.add(arg.value)
    return ids


def js_msgids() -> set[str]:
    text = (PKG / "static" / "js" / "app.js").read_text(encoding="utf-8")
    return {json.loads(f'"{m}"') for m in re.findall(r'\bT\("((?:[^"\\]|\\.)*)"', text)}


def fields(s: str) -> set[str]:
    try:
        return {f for _t, f, _s, _c in string.Formatter().parse(s) if f}
    except ValueError:
        return {"<invalid>"}


DU_WORDS = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines|euch|euer|eure|euren|eurem|eurer|eures)\b",
                      re.IGNORECASE)


class FormalAddressTests(unittest.TestCase):
    """3.1.0: the German interface addresses users formally ("Sie") – no "du" texts may come back."""

    def test_msgids(self):
        hits = sorted(m for m in template_msgids() | python_msgids() | js_msgids() if DU_WORDS.search(m))
        self.assertEqual(hits, [])

    def test_help_mails_news(self):
        files = list((PKG / "templates" / "help" / "de").glob("*.html"))
        hits = [f"{f.name}: {m.group(0)}" for f in files for m in DU_WORDS.finditer(f.read_text(encoding="utf-8"))]
        news = json.loads((PKG / "news.json").read_text(encoding="utf-8"))
        hits += [f"news {e['version']}: {m.group(0)}" for e in news for i in e["items"]
                 for m in DU_WORDS.finditer(i["de"]["title"] + " " + i["de"]["text"])]
        self.assertEqual(hits, [])


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.en = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))

    def test_everything_translated(self):
        missing = sorted((template_msgids() | python_msgids() | js_msgids()) - set(self.en))
        self.assertEqual(missing, [], f"{len(missing)} Texte ohne englische Übersetzung")

    def test_placeholders_match(self):
        bad = [k for k, v in self.en.items() if fields(k) != fields(v)]
        self.assertEqual(bad, [])

    def test_no_empty_translation(self):
        self.assertEqual([k for k, v in self.en.items() if not v.strip()], [])


class Base(unittest.TestCase):
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
        self.users = {}
        for name, role in (("admin", "admin"), ("bib", "librarian"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local", email=f"{name}@example.org")
            u.set_password(PW)
            db.session.add(u)
            self.users[name] = u
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})


class LanguageTests(Base):
    def test_default_is_german(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertIn('<html lang="de">', html)
        self.assertIn("Anmelden", html)

    def test_browser_language(self):
        html = self.c.get("/login", headers={"Accept-Language": "en-GB,en;q=0.9"}).get_data(as_text=True)
        self.assertIn('<html lang="en">', html)
        self.assertNotIn(">Anmelden<", html)

    def test_admin_default_language(self):
        set_setting("default_language", "en")
        db.session.commit()
        html = self.c.get("/login", headers={"Accept-Language": "fr"}).get_data(as_text=True)
        self.assertIn('<html lang="en">', html)

    def test_switch_before_login_and_saved_in_profile(self):
        self.c.post("/sprache/en", data={"next": "/login"})
        self.assertIn('<html lang="en">', self.c.get("/login").get_data(as_text=True))
        self.login("leser")
        self.c.post("/sprache/en", data={"next": "/"})
        db.session.refresh(self.users["leser"])
        self.assertEqual(self.users["leser"].language, "en")
        # new session, different browser: the profile applies
        other = self.app.test_client()
        other.post("/login", data={"username": "leser", "password": PW})
        self.assertIn('<html lang="en">', other.get("/").get_data(as_text=True))

    def test_switch_rejects_foreign_redirect_and_unknown_language(self):
        r = self.c.post("/sprache/en", data={"next": "https://evil.example/"})
        self.assertNotIn("evil", r.headers["Location"])
        self.assertEqual(self.c.post("/sprache/xx").status_code, 404)

    def test_english_pages_have_no_common_german_words(self):
        """Rough check: typical German words do not appear on English pages."""
        words = re.compile(r"\b(und|oder|nicht|Bücher|Ausleihe|Benutzer|Einstellungen|Speichern|Zurück|Abmelden|"
                           r"Übersicht|Katalog|Sicherheit|Hilfe|bitte|werden|wird)\b")
        self.login("admin")
        self.c.post("/sprache/en", data={"next": "/"})
        pages = ["/", "/buecher", "/buecher/neu", "/buecher/schnellerfassung", "/ausleihen", "/theke", "/standorte",
                 "/profil", "/profil/sicherheit", "/hilfe", "/info", "/admin/benutzer", "/admin/benutzer/neu",
                 "/admin/gruppen", "/admin/gruppen/neu", "/admin/einstellungen", "/admin/verzeichnisdienst",
                 "/admin/email", "/admin/erscheinungsbild", "/admin/datensicherung"]
        found = {}
        for url in pages:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.get_data(as_text=True), flags=re.S)
            hits = sorted(set(words.findall(text)))
            if hits:
                found[url] = hits
        self.assertEqual(found, {})


class MailLanguageTests(Base):
    def test_mail_in_recipient_language(self):
        from catalytix.mailer import render_mail
        u = self.users["leser"]
        u.language = "en"
        db.session.commit()
        en_subject, en_text, _h = render_mail("test", user=u)
        u.language = ""
        db.session.commit()
        de_subject, de_text, _h = render_mail("test", user=u)
        self.assertNotEqual(en_subject, de_subject)
        self.assertNotEqual(en_text, de_text)


class HelpTests(Base):
    def topics(self):
        return set(re.findall(r'<section class="card help-topic" id="([\w-]+)"', self.c.get("/hilfe").get_data(as_text=True)))

    def test_role_filtering(self):
        from catalytix.views.help import TOPICS
        self.login("leser")
        reader = self.topics()
        self.login("bib")
        librarian = self.topics()
        self.login("admin")
        admin = self.topics()
        self.assertIn("borrow", reader)
        self.assertNotIn("desk", reader)
        self.assertNotIn("users", reader)
        self.assertIn("desk", librarian)
        self.assertNotIn("ldap", librarian)
        self.assertEqual(admin, {t.id for t in TOPICS})

    def test_every_topic_has_content_in_both_languages(self):
        from catalytix.views.help import TOPICS
        for t in TOPICS:
            for lang in ("de", "en"):
                f = PKG / "templates" / "help" / lang / f"{t.id}.html"
                self.assertTrue(f.exists() and len(f.read_text(encoding="utf-8")) > 200, f)

    def test_context_link(self):
        self.login("bib")
        html = self.c.get("/theke").get_data(as_text=True)
        self.assertIn("/hilfe?thema=desk", html)
        self.login("leser")
        html = self.c.get("/buecher").get_data(as_text=True)
        self.assertIn("/hilfe?thema=catalog", html)

    def test_help_follows_settings(self):
        self.login("leser")
        set_setting("loan_days", "21")
        db.session.commit()
        self.assertIn("21", self.c.get("/hilfe").get_data(as_text=True))

    def test_reader_cannot_open_admin_topic(self):
        self.login("leser")
        html = self.c.get("/hilfe?thema=ldap").get_data(as_text=True)
        self.assertNotIn('id="ldap"', html)

    def test_english_help(self):
        self.login("leser")
        self.c.post("/sprache/en", data={"next": "/"})
        html = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("Getting started", html)
        self.assertNotIn("Erste Schritte", html)


class LazyTests(unittest.TestCase):
    def test_lazy_and_format(self):
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        with app.app_context():
            db.create_all()
            with i18n.force_locale("en"):
                self.assertEqual(i18n.gettext("unbekannter Text {x}", x=1), "unbekannter Text 1")
                self.assertEqual(i18n.format_day(__import__("datetime").date(2026, 10, 20)), "20 Oct 2026")
            with i18n.force_locale("de"):
                self.assertEqual(i18n.format_day(__import__("datetime").date(2026, 10, 20)), "20.10.2026")
                self.assertEqual(str(i18n.lazy_gettext("Leser")), "Leser")


if __name__ == "__main__":
    unittest.main()
