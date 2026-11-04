"""2.20.1: findings of the code review – reservations that no longer apply, queue jumping, invisible books when
lending, message when merging duplicate ISBNs, display name in the profile."""
import os
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_people import PW, Base  # noqa: E402

from catalytix import reservations  # noqa: E402
from catalytix.models import Book, Group, Loan, Reservation, User, db, set_setting  # noqa: E402


class ReservationValidityTests(Base):
    def setUp(self):
        super().setUp()
        set_setting("reservations_enabled", "1")
        set_setting("self_checkout", "1")
        db.session.commit()
        self.bib = User.query.filter_by(username="bib").one()
        self.leser = User.query.filter_by(username="leser").one()
        self.anna = User.query.filter_by(username="anna").one()
        self.carl = User(username="carl", display_name="Carl", role="reader", auth_source="local")
        self.carl.set_password(PW)
        self.chef = User(username="chef", display_name="Chef", role="admin", auth_source="local")
        self.chef.set_password(PW)
        db.session.add_all([self.carl, self.chef])
        db.session.commit()
        self.notify = mock.patch("catalytix.mailer.notify_reservation_ready").start()
        self.addCleanup(mock.patch.stopall)

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def lend_to_bib(self, book):
        db.session.add(Loan(book=book, user=self.bib, due_at=date.today() + timedelta(days=10)))
        db.session.commit()

    def give_back(self, book):
        self.login("bib")
        loan = Loan.query.filter_by(book_id=book.id, returned_at=None).one()
        return self.c.post(f"/ausleihen/{loan.id}/rueckgabe", follow_redirects=True).get_data(as_text=True)

    def queue(self, book, *names):
        for n in names:
            self.login(n)
            self.c.post(f"/buecher/{book.id}/vormerken")

    def status(self, user):
        db.session.expire_all()
        return Reservation.query.filter_by(user_id=user.id).order_by(Reservation.id.desc()).first().status

    def test_book_moved_into_reference_only_group(self):
        self.lend_to_bib(self.book)
        self.queue(self.book, "leser")
        arch = Group(name="Archiv", lendable=False)
        db.session.add(arch)
        db.session.commit()
        self.login("bib")
        self.c.post(f"/buecher/{self.book.id}/bearbeiten", data={"title": "Faust", "copies": "1",
                                                                  "group_id": str(arch.id)})
        self.assertEqual(self.status(self.leser), "cancelled")
        self.give_back(self.book)
        self.assertEqual(self.notify.call_count, 0)          # no "ready for pickup" mail for a reference-only book

    def test_deleted_user_with_ready_hold_next_in_line_gets_it(self):
        self.lend_to_bib(self.book)
        self.queue(self.book, "leser", "anna")
        self.give_back(self.book)
        self.assertEqual(self.status(self.leser), "ready")
        self.login("chef")
        self.c.post(f"/admin/benutzer/{self.leser.id}/loeschen")
        self.assertIsNone(db.session.get(User, self.leser.id))
        self.assertEqual(self.status(self.anna), "ready")
        # and Carl cannot jump the queue
        self.login("carl")
        self.c.post(f"/buecher/{self.book.id}/ausleihen")
        self.assertIsNone(Loan.query.filter_by(user_id=self.carl.id).first())

    def test_no_queue_jumping_when_a_copy_is_free_but_not_yet_held(self):
        self.lend_to_bib(self.book)
        self.queue(self.book, "leser", "anna")
        # copy free without promote having run (e.g. return just booked)
        Loan.query.filter_by(book_id=self.book.id, returned_at=None).one().returned_at = db.func.now()
        db.session.commit()
        self.login("carl")
        html = self.c.post(f"/buecher/{self.book.id}/ausleihen", follow_redirects=True).get_data(as_text=True)
        self.assertIn("vorgemerkt haben", html)
        self.assertIsNone(Loan.query.filter_by(user_id=self.carl.id).first())
        self.assertEqual(self.status(self.leser), "ready")      # given straight to the first in line
        self.login("anna")                                       # second in the queue: cannot skip ahead either
        self.c.post(f"/buecher/{self.book.id}/ausleihen")
        self.assertIsNone(Loan.query.filter_by(user_id=self.anna.id).first())
        self.login("leser")
        self.c.post(f"/buecher/{self.book.id}/ausleihen")
        self.assertIsNotNone(Loan.query.filter_by(user_id=self.leser.id, returned_at=None).first())

    def test_user_who_left_the_group_is_skipped(self):
        self.leser.groups = [self.group]
        self.anna.groups = [self.group]
        db.session.commit()
        self.lend_to_bib(self.gbook)
        self.queue(self.gbook, "leser", "anna")
        db.session.get(User, self.leser.id).groups = []      # e.g. by the AD sync
        db.session.commit()
        self.give_back(self.gbook)
        self.assertEqual(self.status(self.leser), "cancelled")
        self.assertEqual(self.status(self.anna), "ready")

    def test_ready_hold_released_when_user_loses_group_in_background(self):
        self.leser.groups = [self.group]
        self.anna.groups = [self.group]
        db.session.commit()
        self.lend_to_bib(self.gbook)
        self.queue(self.gbook, "leser", "anna")
        self.give_back(self.gbook)
        self.assertEqual(self.status(self.leser), "ready")
        db.session.get(User, self.leser.id).groups = []      # AD sync, without the admin form
        db.session.commit()
        reservations.expire_due()                            # regular background run
        self.assertEqual(self.status(self.leser), "cancelled")
        self.assertEqual(self.status(self.anna), "ready")

    def test_deactivated_by_admin_releases_hold(self):
        self.lend_to_bib(self.book)
        self.queue(self.book, "leser", "anna")
        self.give_back(self.book)
        self.login("chef")
        self.c.post(f"/admin/benutzer/{self.leser.id}", data={"username": "leser", "display_name": "Leser",
                                                               "role": "reader"})   # "account active" not ticked
        db.session.expire_all()
        self.assertFalse(db.session.get(User, self.leser.id).active)
        self.assertEqual(self.status(self.leser), "cancelled")
        self.assertEqual(self.status(self.anna), "ready")


class BorrowVisibilityTests(Base):
    def test_invisible_reference_only_book_reveals_nothing(self):
        set_setting("self_checkout", "1")
        set_setting("catalog_visibility", "groups")
        db.session.commit()
        secret = Group(name="Geheim", lendable=False)
        b = Book(title="Streng vertraulich", copies=1, group=secret)
        db.session.add_all([secret, b])
        db.session.commit()
        self.c.post("/logout")
        self.c.post("/login", data={"username": "leser", "password": PW})
        r = self.c.post(f"/buecher/{b.id}/ausleihen", follow_redirects=True)
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertNotIn("Streng vertraulich", html)
        self.assertNotIn("Geheim", html)


class MergeAndProfileTests(Base):
    def test_merge_reports_copies_actually_added(self):
        b = Book(title="X", isbn="9783161484100", copies=9_999)
        db.session.add(b)
        db.session.commit()
        html = self.c.post("/buecher/neu", data={"title": "X", "isbn": "9783161484100", "copies": "5"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertEqual(db.session.get(Book, b.id).copies, 10_000)
        self.assertIn("1 Exemplar hinzugefügt, jetzt 10000", html)
        self.assertIn("nicht übernommen", html)
        html = self.c.post("/buecher/neu", data={"title": "X", "isbn": "9783161484100", "copies": "0"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("es wurde nichts hinzugefügt", html)

    def test_display_name_required(self):
        self.c.post("/logout")
        self.c.post("/login", data={"username": "leser", "password": PW})
        html = self.c.post("/profil", data={"action": "data", "display_name": "  ", "email": ""},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("Bitte einen Namen angeben", html)
        self.assertEqual(User.query.filter_by(username="leser").one().display_name, "Leser")

    def test_reservation_rows_show_username(self):
        set_setting("reservations_enabled", "1")
        db.session.commit()
        db.session.add(Loan(book=self.book, user=User.query.filter_by(username="anna").one(),
                            due_at=date.today() + timedelta(days=3)))
        db.session.add(Reservation(book=self.book, user=User.query.filter_by(username="leser").one()))
        db.session.commit()
        html = self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True)
        self.assertTrue(re.search(r"Leser <small[^>]*>leser</small>", html))
        self.assertIn('class="row-badge"', html)


if __name__ == "__main__":
    unittest.main()
