"""Vormerkungen: Warteschlange, Zurücklegen mit Abholfrist, Ausleihe, Ablauf, Einstellungen."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_people import PW, Base  # noqa: E402

from catalytix import reservations  # noqa: E402
from catalytix.models import Book, Loan, Reservation, User, db, set_setting  # noqa: E402


class ReservationTests(Base):
    def setUp(self):
        super().setUp()
        set_setting("reservations_enabled", "1")
        set_setting("self_checkout", "1")
        db.session.commit()
        self.leser = User.query.filter_by(username="leser").one()
        self.anna = User.query.filter_by(username="anna").one()
        self.notify = mock.patch("catalytix.mailer.notify_reservation_ready").start()
        self.addCleanup(mock.patch.stopall)
        # Faust (1 Exemplar) ist an „bib“ verliehen
        db.session.add(Loan(book=self.book, user=User.query.filter_by(username="bib").one(),
                            due_at=date.today() + timedelta(days=10)))
        db.session.commit()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def reserve(self, name):
        self.login(name)
        return self.c.post(f"/buecher/{self.book.id}/vormerken", follow_redirects=True).get_data(as_text=True)

    def give_back(self):
        self.login("bib")
        loan = Loan.query.filter_by(book_id=self.book.id, returned_at=None).one()
        return self.c.post(f"/ausleihen/{loan.id}/rueckgabe", follow_redirects=True).get_data(as_text=True)

    def res(self, user):
        return Reservation.query.filter_by(user_id=user.id).order_by(Reservation.id.desc()).first()

    def test_disabled(self):
        set_setting("reservations_enabled", "0")
        db.session.commit()
        self.login("leser")
        self.assertNotIn("vormerken", self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/buecher/{self.book.id}/vormerken").status_code, 404)

    def test_queue_and_positions(self):
        self.assertIn("Platz 1", self.reserve("leser"))
        self.assertIn("Platz 2", self.reserve("anna"))
        self.assertIn("bereits vorgemerkt", self.reserve("anna"))
        page = self.c.get("/ausleihen").get_data(as_text=True)
        self.assertIn("Meine Vormerkungen", page)
        self.assertIn("Platz 2", page)

    def test_only_when_nothing_free(self):
        self.book.copies = 2
        db.session.commit()
        self.assertIn("direkt ausleihen", self.reserve("leser"))
        self.assertEqual(Reservation.query.count(), 0)

    def test_maximum_per_person(self):
        set_setting("reservation_max", "1")
        other = Book(title="Werther", copies=1)
        db.session.add(other)
        db.session.add(Loan(book=other, user=self.anna, due_at=date.today() + timedelta(days=3)))
        db.session.commit()
        self.reserve("leser")
        r = self.c.post(f"/buecher/{other.id}/vormerken", follow_redirects=True).get_data(as_text=True)
        self.assertIn("mehr sind nicht möglich", r)

    def test_return_sets_aside_for_first_in_line(self):
        self.reserve("leser")
        self.reserve("anna")
        msg = self.give_back()
        self.assertIn("vorgemerkt für Leser", msg)
        r = self.res(self.leser)
        self.assertEqual(r.status, "ready")
        self.assertEqual(r.hold_until, date.today() + timedelta(days=7))
        self.notify.assert_called_once()
        self.assertEqual(self.res(self.anna).status, "waiting")
        db.session.refresh(self.book)
        self.assertEqual(self.book.available, 0)
        self.assertEqual(self.book.available_for(self.leser), 1)
        # anna darf das zurückgelegte Exemplar nicht ausleihen
        self.login("anna")
        self.c.post(f"/buecher/{self.book.id}/ausleihen")
        self.assertEqual(Loan.query.filter_by(user_id=self.anna.id).count(), 0)
        # leser schon – die Vormerkung ist damit erledigt
        self.login("leser")
        self.assertIn("Für dich zurückgelegt", self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True))
        self.c.post(f"/buecher/{self.book.id}/ausleihen")
        self.assertEqual(Loan.query.filter_by(user_id=self.leser.id, returned_at=None).count(), 1)
        self.assertEqual(self.res(self.leser).status, "done")

    def test_desk_lends_ready_reservation(self):
        self.reserve("leser")
        self.give_back()
        page = self.c.get("/theke?benutzer=leser").get_data(as_text=True)
        self.assertIn("Für Leser zurückgelegt", page)
        self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "leser", "book_id": self.book.id})
        self.assertEqual(self.res(self.leser).status, "done")

    def test_expiry_moves_queue_on(self):
        self.reserve("leser")
        self.reserve("anna")
        self.give_back()
        self.assertEqual(reservations.expire_due(date.today() + timedelta(days=8)), 1)
        self.assertEqual(self.res(self.leser).status, "expired")
        self.assertEqual(self.res(self.anna).status, "ready")

    def test_cancel_ready_moves_queue_on(self):
        self.reserve("leser")
        self.reserve("anna")
        self.give_back()
        self.login("leser")
        self.c.post(f"/vormerkungen/{self.res(self.leser).id}/stornieren")
        self.assertEqual(self.res(self.leser).status, "cancelled")
        self.assertEqual(self.res(self.anna).status, "ready")

    def test_others_cannot_cancel(self):
        self.reserve("leser")
        self.login("anna")
        self.assertEqual(self.c.post(f"/vormerkungen/{self.res(self.leser).id}/stornieren").status_code, 403)
        self.login("bib")      # Verwalter des Buchs darf
        self.c.post(f"/vormerkungen/{self.res(self.leser).id}/stornieren")
        self.assertEqual(self.res(self.leser).status, "cancelled")

    def test_reader_cannot_extend_reserved_book(self):
        set_setting("self_extend", "1")
        db.session.commit()
        loan = Loan.query.filter_by(book_id=self.book.id, returned_at=None).one()
        loan.user = self.anna
        db.session.commit()
        self.reserve("leser")
        self.login("anna")
        due = loan.due_at
        self.c.post(f"/ausleihen/{loan.id}/verlaengern")
        db.session.refresh(loan)
        self.assertEqual(loan.due_at, due)
        self.login("bib")      # Verwalter dürfen trotzdem
        self.c.post(f"/ausleihen/{loan.id}/verlaengern")
        db.session.refresh(loan)
        self.assertGreater(loan.due_at, due)

    def test_copies_changes(self):
        self.reserve("leser")
        self.reserve("anna")
        self.login("bib")
        self.c.post(f"/api/buecher/{self.book.id}/exemplar")
        self.assertEqual(self.res(self.leser).status, "ready")
        self.assertEqual(self.res(self.anna).status, "waiting")
        # wieder ein Exemplar weniger: Zurücklegung geht zurück in die Warteschlange
        self.book.copies = 1
        db.session.commit()
        reservations.rebalance(self.book.id)
        self.assertEqual(self.res(self.leser).status, "waiting")

    def test_catalog_filter_counts_held_copies(self):
        self.reserve("leser")
        self.give_back()
        self.login("anna")
        page = self.c.get("/buecher?status=verfuegbar").get_data(as_text=True)
        self.assertNotIn("Faust", page)

    def test_switching_off_cancels_open(self):
        self.reserve("leser")
        self.give_back()
        reservations.cancel_all()
        db.session.refresh(self.book)
        self.assertEqual(self.book.available, 1)
        self.assertEqual(self.res(self.leser).status, "cancelled")

    def test_dashboard_notice(self):
        self.reserve("leser")
        self.give_back()
        self.login("leser")
        self.assertIn("liegt für dich bereit", self.c.get("/").get_data(as_text=True))

    def test_retention_removes_old_closed(self):
        from catalytix.retention import run_retention
        self.reserve("leser")
        r = self.res(self.leser)
        r.status, r.closed_at = "cancelled", datetime.now() - timedelta(days=800)
        db.session.commit()
        run_retention()
        self.assertEqual(Reservation.query.count(), 0)

    def test_inactive_person_skipped(self):
        self.reserve("leser")
        self.reserve("anna")
        self.leser.active = False
        db.session.commit()
        self.give_back()
        self.assertEqual(self.res(self.leser).status, "cancelled")
        self.assertEqual(self.res(self.anna).status, "ready")


class ReservationMailTests(unittest.TestCase):
    def test_mail_renders_in_both_languages(self):
        from test_mail import MailBase
        case = MailBase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        from catalytix import mailer
        from catalytix.models import Reservation as R
        b = case.book("Momo")
        res = R(book=b, user=case.lena, status="ready", hold_until=date.today() + timedelta(days=7))
        db.session.add(res)
        db.session.commit()
        mailer._reservation_mail(res.id)
        self.assertEqual(len(case.smtp.messages), 1)
        self.assertIn("Momo", case.smtp.messages[0]["msg"]["Subject"])


if __name__ == "__main__":
    unittest.main()
