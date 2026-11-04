"""People search when lending: local and in Active Directory (people who never logged in)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.ldap_auth import LDAPError, LDAPUser  # noqa: E402
from catalytix.models import Book, Group, Loan, User, db, set_setting  # noqa: E402

PW = "geheim12345"
GDN = "CN=Klasse-IT,OU=Gruppen,DC=x,DC=de"


def ad(name, display, role="reader", groups=(GDN,)):
    return LDAPUser(username=name, dn=f"CN={display},DC=x,DC=de", display_name=display,
                    email=f"{name}@x.de", groups=list(groups), role=role)


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
        self.group = Group(name="IT", ldap_dn=GDN)
        db.session.add(self.group)
        for name, role in (("bib", "librarian"), ("leser", "reader"), ("anna", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local")
            u.set_password(PW)
            db.session.add(u)
        self.book = Book(title="Faust", copies=1)
        self.gbook = Book(title="Nur IT", copies=1, group=self.group)
        db.session.add_all([self.book, self.gbook])
        for k, v in {"ldap_enabled": True, "ldap_bind_dn": "CN=svc,DC=x,DC=de", "ldap_group_sync": True}.items():
            set_setting(k, v)
        db.session.commit()
        self.c = self.app.test_client()
        self.c.post("/login", data={"username": "bib", "password": PW})

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def setUp_cache(self):
        from catalytix import people
        people._cache.clear()
        people._found.clear()
        people._rechecked.clear()
        people._outage_until = 0.0

    def directory(self, search=(), account=None, error=None):
        self.setUp_cache()
        patcher = mock.patch("catalytix.ldap_auth.Directory")
        cls = patcher.start()
        self.addCleanup(patcher.stop)
        cls.return_value.find_by_guid.return_value = None      # the previous person no longer exists in AD
        if error:
            cls.return_value.search_users.side_effect = error
            cls.return_value.check_account.side_effect = error
        else:
            cls.return_value.search_users.return_value = list(search)
            cls.return_value.check_account.side_effect = lambda n: account if account and account.username == n else None
        return cls


class SearchTests(Base):
    def test_finds_local_and_new_directory_people(self):
        self.directory(search=[ad("berta", "Berta Brecht"), ad("anna", "Anna Local")])
        r = self.c.get("/api/personen?q=ann").get_json()
        names = [(x["username"], x["new"]) for x in r["results"]]
        self.assertIn(("anna", False), names)          # local account
        self.assertIn(("berta", True), names)          # new from AD
        self.assertEqual(sum(1 for u, _n in names if u == "anna"), 1)   # not duplicated
        self.assertEqual(next(x for x in r["results"] if x["username"] == "berta")["groups"], ["IT"])

    def test_nothing_is_created_by_searching(self):
        self.directory(search=[ad("berta", "Berta Brecht")])
        self.c.get("/api/personen?q=ber")
        self.c.get("/theke?modus=ausleihe&benutzer=berta")
        self.assertIsNone(User.query.filter_by(username="berta").first())

    def test_directory_down_still_finds_local(self):
        self.directory(error=LDAPError("down"))
        r = self.c.get("/api/personen?q=leser").get_json()
        self.assertEqual([x["username"] for x in r["results"]], ["leser"])
        self.assertTrue(r["directory_error"])

    def test_readers_cannot_search(self):
        self.c.post("/logout")
        self.c.post("/login", data={"username": "leser", "password": PW})
        self.assertEqual(self.c.get("/api/personen?q=an").status_code, 403)

    def test_without_service_account_only_local(self):
        set_setting("ldap_bind_dn", "")
        db.session.commit()
        cls = self.directory(search=[ad("berta", "Berta Brecht")])
        r = self.c.get("/api/personen?q=be").get_json()
        self.assertFalse(r["directory"])
        cls.return_value.search_users.assert_not_called()


class LendTests(Base):
    def test_desk_shows_candidate_and_lending_creates_account(self):
        self.directory(account=ad("berta", "Berta Brecht"))
        html = self.c.get("/theke?modus=ausleihe&benutzer=berta").get_data(as_text=True)
        self.assertIn("Berta Brecht", html)
        self.assertIn("neu aus dem AD", html)
        r = self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "berta", "book_id": self.gbook.id},
                        follow_redirects=True)
        self.assertIn("aus dem Active Directory übernommen", r.get_data(as_text=True))
        u = User.query.filter_by(username="berta").one()
        self.assertEqual((u.auth_source, u.role, u.password_hash), ("ldap", "reader", None))
        self.assertEqual([g.name for g in u.groups], ["IT"])
        self.assertEqual(Loan.query.filter_by(user_id=u.id).count(), 1)

    def test_failed_lend_leaves_no_account(self):
        self.directory(account=ad("carl", "Carl Ohne", groups=()))      # not in IT → may not borrow the IT book
        self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "carl", "book_id": self.gbook.id})
        self.assertIsNone(User.query.filter_by(username="carl").first())

    def test_book_page_lend_by_username(self):
        self.directory(account=ad("berta", "Berta Brecht"))
        self.c.post(f"/buecher/{self.book.id}/ausleihen", data={"benutzer": "berta"})
        u = User.query.filter_by(username="berta").one()
        self.assertEqual(self.book.active_loans[0].user_id, u.id)

    def test_directory_person_without_role_not_offered(self):
        self.directory(account=ad("dora", "Dora Keinrecht", role=None))
        r = self.c.post(f"/buecher/{self.book.id}/ausleihen", data={"benutzer": "dora"}, follow_redirects=True)
        self.assertIn("nicht gefunden", r.get_data(as_text=True))
        self.assertIsNone(User.query.filter_by(username="dora").first())

    def test_local_account_never_converted(self):
        self.directory(account=ad("anna", "Anna AD"))
        self.c.post(f"/buecher/{self.book.id}/ausleihen", data={"benutzer": "anna"})
        u = User.query.filter_by(username="anna").one()
        self.assertEqual(u.auth_source, "local")
        self.assertTrue(u.check_password(PW))

    def test_new_account_can_log_in_only_via_ad(self):
        self.directory(account=ad("berta", "Berta Brecht"))
        self.c.post(f"/buecher/{self.book.id}/ausleihen", data={"benutzer": "berta"})
        self.c.post("/logout")
        r = self.c.post("/login", data={"username": "berta", "password": ""})
        self.assertNotEqual(r.status_code, 302)



class ReviewFixTests(Base):
    def test_candidate_with_hit_list_no_500(self):
        self.directory(account=ad("berta", "Berta Brecht"))
        db.session.add(Book(title="Faust II", copies=1))
        db.session.commit()
        r = self.c.get("/theke?modus=ausleihe&benutzer=berta&code=Faust&auto=0")
        self.assertEqual(r.status_code, 200)

    def test_candidate_can_borrow_mirrors_groups(self):
        from catalytix.people import Candidate
        self.assertTrue(Candidate(ad("x", "X")).can_borrow(self.gbook))
        self.assertFalse(Candidate(ad("y", "Y", groups=())).can_borrow(self.gbook))
        self.assertTrue(Candidate(ad("z", "Z", groups=())).can_borrow(self.book))

    def test_concurrent_provisioning_no_500(self):
        from catalytix import people
        self.directory(account=ad("berta", "Berta Brecht"))
        real_sync = __import__("catalytix.views.main", fromlist=["sync_ldap_user"]).sync_ldap_user

        def racing(info, existing):
            # someone else creates the account right now (own, already committed transaction)
            db.session.add(User(username="berta", display_name="B", role="reader", auth_source="ldap"))
            db.session.commit()
            return real_sync(info, existing)
        with mock.patch("catalytix.views.main.sync_ldap_user", racing):
            r = self.c.post(f"/buecher/{self.book.id}/ausleihen", data={"benutzer": "berta"})
        self.assertLess(r.status_code, 500)
        self.assertEqual(User.query.filter_by(username="berta").count(), 1)

    def test_group_librarian_sees_only_own_groups(self):
        other = Group(name="Personal")
        db.session.add(other)
        anna = User.query.filter_by(username="anna").one()
        anna.groups = [other]
        bib = User.query.filter_by(username="bib").one()
        bib.groups = [self.group]
        db.session.add(Loan(book=Book(title="Geheim", copies=1, group=other), user=anna,
                            due_at=__import__("datetime").date.today()))
        db.session.commit()
        self.directory()
        r = self.c.get("/api/personen?q=anna").get_json()
        row = next(x for x in r["results"] if x["username"] == "anna")
        self.assertEqual((row["groups"], row["loans"]), ([], 0))

    def test_known_person_found_via_directory_attributes(self):
        self.directory(search=[ad("anna", "Anna Neu")])
        r = self.c.get("/api/personen?q=neue-adresse").get_json()
        self.assertEqual([(x["username"], x["new"]) for x in r["results"]], [("anna", False)])

    def test_directory_cached_and_paused_after_error(self):
        cls = self.directory(search=[ad("berta", "Berta Brecht")])
        self.c.get("/api/personen?q=ber")
        self.c.get("/api/personen?q=ber")
        self.assertEqual(cls.return_value.search_users.call_count, 1)
        cls.return_value.search_users.side_effect = LDAPError("down")
        self.c.get("/api/personen?q=bert")
        self.c.get("/api/personen?q=berta")
        self.assertEqual(cls.return_value.search_users.call_count, 2)   # second request: pause, no AD call

    def test_short_query_does_not_hit_directory(self):
        cls = self.directory(search=[ad("berta", "Berta Brecht")])
        self.c.get("/api/personen?q=be")
        cls.return_value.search_users.assert_not_called()


if __name__ == "__main__":
    unittest.main()
