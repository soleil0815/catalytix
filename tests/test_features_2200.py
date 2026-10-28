"""2.20.0: Präsenzbestand (Gruppe ohne Verleih), Verleihen aus dem Bestand, doppelte ISBN zählt Exemplare hinzu,
eigene Daten für lokale Konten."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import Book, Group, Loan, Reservation, User, db, ensure_columns, set_setting  # noqa: E402

PW = "geheim12345"
ISBN = "9783161484100"


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
        self.ref = Group(name="Kein Verleih", lendable=False)
        self.ga = Group(name="A")
        db.session.add_all([self.ref, self.ga])
        for name, role in (("chef", "admin"), ("bib", "librarian"), ("leser", "reader"), ("ad", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local",
                     email=f"{name}@schule.de")
            u.set_password(PW)
            db.session.add(u)
        self.lex = Book(title="Lexikon", signature="REF-1", isbn=ISBN, copies=2, group=self.ref)
        self.faust = Book(title="Faust", signature="DE-1", copies=1)
        db.session.add_all([self.lex, self.faust])
        set_setting("self_checkout", "1")
        set_setting("reservations_enabled", "1")
        db.session.commit()
        self.c = self.app.test_client()
        self.login("bib")

    def tearDown(self):
        mock.patch.stopall()
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def page(self, url, **kw):
        return self.c.get(url, **kw).get_data(as_text=True)


class ReferenceOnlyTests(Base):
    def test_book_property(self):
        self.assertFalse(self.lex.lendable)
        self.assertTrue(self.faust.lendable)
        self.ga.lendable = True
        self.assertTrue(Book(title="x", group=self.ga).lendable)

    def test_cannot_lend_at_detail_or_for_self(self):
        set_setting("catalog_visibility", "all")    # Leser sieht das Buch (sonst: 404, siehe test_review_2201)
        db.session.commit()
        self.c.post(f"/buecher/{self.lex.id}/ausleihen", data={"benutzer": "leser"})
        self.login("leser")
        self.c.post(f"/buecher/{self.lex.id}/ausleihen")
        self.assertEqual(Loan.query.count(), 0)
        html = self.page(f"/buecher/{self.lex.id}")
        self.assertIn("Präsenzbestand", html)
        self.assertNotIn("Für mich ausleihen", html)

    def test_detail_has_no_lend_form(self):
        html = self.page(f"/buecher/{self.lex.id}")
        self.assertNotIn("An Benutzer verleihen", html)
        self.assertIn("An Benutzer verleihen", self.page(f"/buecher/{self.faust.id}"))

    def test_desk_does_not_offer_it(self):
        res = self.c.get("/api/bestand?q=Lexikon&modus=ausleihe").get_json()["results"]
        self.assertEqual(res, [])
        res = self.c.get("/api/bestand?q=REF-1&modus=ausleihe").get_json()["results"]
        self.assertEqual(res, [])
        # Scan an der Theke: klare Meldung, kein Angebot, keine Ausleihe
        html = self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "leser", "code": "REF-1", "auto": "1"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("Präsenzbestand", html)
        self.assertEqual(Loan.query.count(), 0)
        html = self.page("/theke?modus=ausleihe&code=Lexikon")
        self.assertIn("Präsenzbestand", html)
        self.assertNotIn("Kein Buch zu", html)

    def test_existing_loans_can_still_be_returned(self):
        leser = User.query.filter_by(username="leser").one()
        loan = Loan(book=self.lex, user=leser, due_at=date.today() + timedelta(days=5))
        db.session.add(loan)
        db.session.commit()
        res = self.c.get("/api/bestand?q=Lexikon&modus=rueckgabe").get_json()["results"]
        self.assertEqual([r["id"] for r in res], [self.lex.id])
        self.c.post(f"/ausleihen/{loan.id}/rueckgabe")
        self.assertIsNotNone(db.session.get(Loan, loan.id).returned_at)

    def test_no_reservations(self):
        self.lex.group = self.ga          # erst ausleihbar und verliehen …
        User.query.filter_by(username="leser").one().groups = [self.ga]
        db.session.commit()
        for u in ("bib", "chef"):
            db.session.add(Loan(book=self.lex, user=User.query.filter_by(username=u).one(),
                                due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.login("leser")
        self.c.post(f"/buecher/{self.lex.id}/vormerken")
        self.assertEqual(Reservation.query.filter_by(status="waiting").count(), 1)
        # … dann Präsenzbestand: Vormerkung wird beendet, neue sind nicht möglich
        self.login("chef")
        leser_id = User.query.filter_by(username="leser").one().id
        self.c.post(f"/admin/gruppen/{self.ga.id}", data={"name": "A", "not_lendable": "1", "members": [str(leser_id)]})
        self.assertFalse(db.session.get(Group, self.ga.id).lendable)
        self.assertEqual(Reservation.query.filter_by(status="waiting").count(), 0)
        self.login("leser")
        html = self.c.post(f"/buecher/{self.lex.id}/vormerken", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Präsenzbestand", html)
        self.assertEqual(Reservation.query.filter(Reservation.status.in_(("waiting", "ready"))).count(), 0)

    def test_group_form_toggle(self):
        self.login("chef")
        self.assertIn('name="not_lendable"', self.page(f"/admin/gruppen/{self.ga.id}"))
        self.c.post(f"/admin/gruppen/{self.ref.id}", data={"name": "Kein Verleih"})
        self.assertTrue(db.session.get(Group, self.ref.id).lendable)
        self.c.post("/admin/gruppen/neu", data={"name": "Neu", "not_lendable": "1"})
        self.assertFalse(Group.query.filter_by(name="Neu").one().lendable)
        self.assertIn("Präsenzbestand", self.page("/admin/gruppen"))

    def test_catalog_badge(self):
        self.assertIn("Präsenzbestand", self.page("/buecher"))

    def test_migration_adds_column_lendable(self):
        from sqlalchemy import text
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE groups DROP COLUMN lendable"))
        self.assertIn("groups.lendable", ensure_columns())
        db.session.expire_all()
        self.assertTrue(all(g.lendable for g in Group.query.all()))


class CatalogLendTests(Base):
    def test_lend_link_for_managers_only(self):
        html = self.page("/buecher")
        self.assertIn(f"/buecher/{self.faust.id}?verleihen=1#verleihen", html)
        self.assertNotIn(f"/buecher/{self.lex.id}?verleihen=1", html)        # Präsenzbestand
        self.login("leser")
        self.assertNotIn("verleihen=1", self.page("/buecher"))

    def test_no_link_when_nothing_free(self):
        db.session.add(Loan(book=self.faust, user=User.query.filter_by(username="leser").one(),
                            due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.assertNotIn(f"/buecher/{self.faust.id}?verleihen=1", self.page("/buecher"))

    def test_detail_focuses_lend_form(self):
        html = self.page(f"/buecher/{self.faust.id}?verleihen=1")
        self.assertIn('id="verleihen"', html)
        self.assertIn("autofocus", html)
        self.assertNotIn("autofocus", self.page(f"/buecher/{self.faust.id}"))


class DuplicateIsbnTests(Base):
    def form(self, **kw):
        data = {"title": "Faust", "isbn": "978-3-16-148410-0", "copies": "2", "group_id": ""}
        data.update(kw)
        return self.c.post("/buecher/neu", data=data, follow_redirects=True).get_data(as_text=True)

    def test_same_isbn_same_group_adds_copies(self):
        self.faust.isbn = ISBN
        db.session.commit()
        html = self.form()
        self.assertIn("bereits im Bestand", html)
        self.assertEqual(Book.query.filter_by(isbn=ISBN, group_id=None).count(), 1)
        self.assertEqual(db.session.get(Book, self.faust.id).copies, 3)

    def test_other_group_creates_new(self):
        self.form(group_id=str(self.ga.id))
        self.assertEqual(Book.query.filter_by(isbn=ISBN).count(), 2)     # Lexikon (Präsenz) + neu in A

    def test_without_isbn_creates_new(self):
        self.form(isbn="")
        self.assertEqual(Book.query.filter_by(title="Faust").count(), 2)

    def test_new_copies_go_to_waiting_reservation(self):
        self.faust.isbn = ISBN
        db.session.add(Loan(book=self.faust, user=User.query.filter_by(username="chef").one(),
                            due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.login("leser")
        self.c.post(f"/buecher/{self.faust.id}/vormerken")
        self.login("bib")
        mock.patch("catalytix.mailer.notify_reservation_ready").start()
        self.form(copies="1")
        self.assertEqual(Reservation.query.one().status, "ready")


class ProfileTests(Base):
    def test_local_user_edits_name_and_email(self):
        self.login("leser")
        self.assertIn("Meine Daten", self.page("/profil"))
        self.c.post("/profil", data={"action": "data", "display_name": "Lea Leser", "email": "lea@extern.org"})
        u = User.query.filter_by(username="leser").one()
        self.assertEqual((u.display_name, u.email), ("Lea Leser", "lea@extern.org"))

    def test_invalid_or_taken_email_rejected(self):
        self.login("leser")
        self.c.post("/profil", data={"action": "data", "display_name": "Neu", "email": "kaputt"})
        self.c.post("/profil", data={"action": "data", "display_name": "Neu", "email": "bib@schule.de"})
        u = User.query.filter_by(username="leser").one()
        self.assertEqual((u.display_name, u.email), ("Leser", "leser@schule.de"))

    def test_ad_user_is_read_only(self):
        self.login("ad")                  # lokal angemeldet, dann wie ein AD-Konto behandelt
        User.query.filter_by(username="ad").one().auth_source = "ldap"
        db.session.commit()
        html = self.page("/profil")
        self.assertNotIn('value="data"', html)
        self.assertIn("Active Directory", html)
        self.assertEqual(self.c.post("/profil", data={"action": "data", "display_name": "X"}).status_code, 400)
        self.assertEqual(User.query.filter_by(username="ad").one().display_name, "Ad")

    def test_notify_without_email_field_keeps_address(self):
        set_setting("mail_enabled", "1")
        db.session.commit()
        self.login("leser")
        self.c.post("/profil", data={"action": "notify"})
        u = User.query.filter_by(username="leser").one()
        self.assertEqual(u.email, "leser@schule.de")
        self.assertFalse(u.email_notifications)


if __name__ == "__main__":
    unittest.main()
