"""3.0.5: every form field on every page has an accessible name – a <label for> linked by id, a label around it,
aria-label/aria-labelledby or a title. Before, about 150 labels stood next to their field without being linked:
screen readers announced the fields without a name, and clicking a label did not focus its field."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import Book, Group, Loan, User, db, set_setting  # noqa: E402

PW = "geheim123"
CONTROLS = {"input", "select", "textarea"}
UNNAMED_OK = {"hidden", "submit", "button", "reset", "image"}


class FieldCollector(HTMLParser):
    """Form fields with what names them; labels with their target."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = []          # (tag, attrs, inside_label)
        self.label_for = set()
        self.ids = []
        self.label_depth = 0
        self.labelledby_ok = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in CONTROLS:
            self.fields.append((tag, a, self.label_depth > 0))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "label":
            self.label_depth -= 1

    def handle_endtag(self, tag):
        if tag == "label" and self.label_depth:
            self.label_depth -= 1

    def problems(self):
        out = []
        for tag, a, wrapped in self.fields:
            if tag == "input" and (a.get("type") or "text") in UNNAMED_OK:
                continue
            named = (wrapped or (a.get("id") and a["id"] in self.label_for) or a.get("aria-label")
                     or a.get("aria-labelledby") or a.get("title"))
            if not named:
                out.append(f"<{tag} name={a.get('name')!r} type={a.get('type')!r}> has no name")
        dup = sorted({i for i in self.ids if self.ids.count(i) > 1})
        if dup:
            out.append(f"duplicate ids: {dup}")
        missing = sorted(f for f in self.label_for if f not in self.ids)
        if missing:
            out.append(f"<label for> without a field: {missing}")
        return out


class FormLabelTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                               "SECRET_KEY": "k"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from flask import g

        def _fresh():
            g.pop("_login_user", None)
            g.pop("_catalytix_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        self.group = Group(name="IT")
        db.session.add(self.group)
        users = {}
        for name, role in (("chef", "admin"), ("bib", "librarian"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local",
                     email=f"{name}@firma.local")
            u.set_password(PW)
            db.session.add(u)
            users[name] = u
        self.book = Book(title="Faust", copies=2, isbn="9783161484100", location="Regal 1")
        db.session.add(self.book)
        db.session.flush()
        db.session.add(Loan(book=self.book, user=users["leser"], due_at=date.today() + timedelta(days=7)))
        for k, v in {"reservations_enabled": "1", "self_checkout": "1", "registration_mode": "approval",
                     "setup_done": "1"}.items():
            set_setting(k, v)
        db.session.commit()
        self.users = users
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def check(self, urls):
        problems = []
        for url in urls:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            p = FieldCollector()
            p.feed(r.get_data(as_text=True))
            problems += [f"{url}: {x}" for x in p.problems()]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_signed_out_pages(self):
        self.check(["/login", "/registrieren"])

    def test_reader_pages(self):
        self.login("leser")
        self.check(["/", "/buecher", f"/buecher/{self.book.id}", "/profil", "/profil/sicherheit", "/ausleihen"])

    def test_librarian_pages(self):
        self.login("bib")
        self.check(["/", "/buecher", "/buecher/neu", f"/buecher/{self.book.id}", f"/buecher/{self.book.id}/bearbeiten",
                    "/buecher/schnellerfassung", "/ausleihen", "/theke", "/standorte", "/profil"])

    def test_admin_pages(self):
        self.login("chef")
        with mock.patch("catalytix.twofa.step_up_needed", return_value=False, create=True):
            self.check(["/admin/einstellungen", "/admin/verzeichnisdienst", "/admin/email", "/admin/benutzer",
                        "/admin/benutzer/neu", f"/admin/benutzer/{self.users['leser'].id}", "/admin/gruppen",
                        "/admin/gruppen/neu", f"/admin/gruppen/{self.group.id}", "/admin/erscheinungsbild",
                        "/admin/datensicherung"])

    def test_setup_page(self):
        User.query.delete()
        db.session.commit()
        self.check(["/setup"])

    def test_checker_finds_unlinked_labels(self):
        p = FieldCollector()
        p.feed('<div class="field"><label>Titel</label><input name="title"></div>'
               '<label class="check"><input type="checkbox" name="x"> ok</label>'
               '<label for="f-a">A</label><input id="f-a" name="a"><input name="q" aria-label="Suche">')
        self.assertEqual(p.problems(), ["<input name='title' type=None> has no name"])


if __name__ == "__main__":
    unittest.main()
