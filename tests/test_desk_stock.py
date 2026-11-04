"""Stock search at the circulation desk."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from datetime import date, timedelta  # noqa: E402

from catalytix import create_app  # noqa: E402
from catalytix.models import Book, Group, Loan, User, db  # noqa: E402

PW = "geheim12345"


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
        self.ga, self.gb = Group(name="A"), Group(name="B")
        db.session.add_all([self.ga, self.gb])
        for name, role, groups in (("bib", "librarian", []), ("biba", "librarian", [self.ga]), ("leser", "reader", [])):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local")
            u.set_password(PW)
            u.groups = groups
            db.session.add(u)
        self.faust = Book(title="Faust", authors="Goethe", signature="DE-1", location="Regal 1", copies=2)
        self.faust2 = Book(title="Faust II", authors="Goethe", copies=1, group=self.gb)
        self.pct = Book(title="100% Sicherheit", authors="X", copies=1, group=self.ga)
        self.other = Book(title="Werther", authors="Goethe", tags="Klassiker", copies=1)
        db.session.add_all([self.faust, self.faust2, self.pct, self.other])
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def api(self, q, modus="ausleihe"):
        return self.c.get(f"/api/bestand?q={q}&modus={modus}").get_json()["results"]


class StockSearchTests(Base):
    def test_search_title_author_tags_signature(self):
        self.login("bib")
        self.assertEqual({b["title"] for b in self.api("goethe")}, {"Faust", "Faust II", "Werther"})
        self.assertEqual([b["title"] for b in self.api("klassik")], ["Werther"])
        self.assertEqual(self.api("de-1")[0]["title"], "Faust")
        r = self.api("Faust")[0]
        self.assertEqual((r["available"], r["copies"], r["location"], r["signature"]), (2, 2, "Regal 1", "DE-1"))

    def test_percent_is_literal(self):
        self.login("bib")
        self.assertEqual([b["title"] for b in self.api("100%25")], ["100% Sicherheit"])

    def test_group_librarian_sees_only_own_stock(self):
        self.login("biba")
        titles = {b["title"] for b in self.api("e")} | {b["title"] for b in self.api("Faust")}
        self.assertNotIn("Faust II", titles)

    def test_readers_forbidden(self):
        self.login("leser")
        self.assertEqual(self.c.get("/api/bestand?q=faust").status_code, 403)

    def test_return_mode_ranks_books_on_loan_first(self):
        u = User.query.filter_by(username="leser").one()
        db.session.add(Loan(book=self.faust2, user=u, due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.login("bib")
        res = self.api("Faust", "rueckgabe")
        self.assertEqual((res[0]["title"], res[0]["on_loan"]), ("Faust II", 1))
        self.assertEqual(self.api("Faust", "ausleihe")[0]["title"], "Faust")

    def test_desk_without_borrower_shows_stock_but_lends_nothing(self):
        self.login("bib")
        r = self.c.post("/theke", data={"modus": "ausleihe", "code": "Werther", "auto": "1"})
        self.assertIn("Werther", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)

    def test_desk_author_search_and_hash_selection(self):
        self.login("bib")
        r = self.c.get("/theke?modus=ausleihe&code=Goethe")
        html = r.get_data(as_text=True)
        self.assertIn("Werther", html)
        self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "leser", "code": f"#{self.other.id}", "auto": "1"})
        self.assertEqual(Loan.query.one().book_id, self.other.id)

    def test_books_of_other_groups_marked_and_ranked_last(self):
        u = User.query.filter_by(username="leser").one()
        u.groups = [self.ga]
        db.session.commit()
        self.login("bib")
        res = self.c.get("/api/bestand?q=Faust&modus=ausleihe&benutzer=leser").get_json()["results"]
        self.assertEqual([(b["title"], b["allowed"]) for b in res], [("Faust", True), ("Faust II", False)])

    def test_exact_signature_never_lost_among_many_partial_matches(self):
        db.session.add_all([Book(title=f"Aaa {i:02d}", isbn=f"97830000{i:02d}770", copies=1) for i in range(80)])
        db.session.add(Book(title="Zoologie", signature="77", copies=1))
        db.session.commit()
        self.login("bib")
        res = self.api("77")
        self.assertEqual((res[0]["title"], res[0]["exact"]), ("Zoologie", True))
        self.assertFalse(res[1]["exact"])

    def test_books_already_borrowed_ranked_last(self):
        u = User.query.filter_by(username="leser").one()
        u.groups = [self.gb]
        db.session.add(Loan(book=self.faust, user=u, due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.login("bib")
        res = self.c.get("/api/bestand?q=Faust&modus=ausleihe&benutzer=leser").get_json()["results"]
        self.assertEqual([(b["title"], b["has"]) for b in res][-1], ("Faust", True))

    def test_directory_candidate_permissions_used(self):
        from unittest import mock
        from catalytix import people
        from catalytix.ldap_auth import LDAPUser
        from catalytix.models import set_setting
        set_setting("ldap_enabled", True); set_setting("ldap_bind_dn", "CN=svc"); set_setting("ldap_group_sync", True)
        self.ga.ldap_dn = "CN=GA,DC=x"
        db.session.commit()
        people._found.clear()
        info = LDAPUser(username="neu", dn="CN=Neu,DC=x", display_name="Neu", groups=["CN=GA,DC=x"], role="reader")
        with mock.patch("catalytix.ldap_auth.Directory") as cls:
            cls.return_value.check_account.return_value = info
            self.login("bib")
            res = self.c.get("/api/bestand?q=Faust&modus=ausleihe&benutzer=neu").get_json()["results"]
            self.c.get("/api/bestand?q=Faust&modus=ausleihe&benutzer=neu")
        self.assertEqual([(b["title"], b["allowed"]) for b in res], [("Faust", True), ("Faust II", False)])
        self.assertEqual(cls.return_value.check_account.call_count, 1)    # cached


if __name__ == "__main__":
    unittest.main()
