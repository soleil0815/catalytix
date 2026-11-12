"""Retention period: delete old loans and mail log, keep statistics."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import Book, Loan, MailLog, User, db, get_setting, set_setting  # noqa: E402
from catalytix.retention import run_daily, run_retention  # noqa: E402

NOW = datetime(2026, 10, 17, 3, 0)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin = self.user("admin", "admin")
        self.reader = self.user("leser")
        self.book = Book(title="Faust", copies=3)
        self.other = Book(title="Woyzeck", copies=1)
        db.session.add_all([self.book, self.other])
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def user(self, name, role="reader"):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        return u

    def loan(self, book, days_ago_loaned, days_ago_returned=None, user=None):
        loaned = NOW - timedelta(days=days_ago_loaned)
        l = Loan(book=book, user=user or self.reader, loaned_at=loaned, due_at=(loaned + timedelta(days=28)).date(),
                 returned_at=None if days_ago_returned is None else NOW - timedelta(days=days_ago_returned))
        db.session.add(l)
        db.session.commit()
        return l

    def mail(self, days_ago, loan=None, kind="loan"):
        m = MailLog(kind=kind, to_addr="leser@example.org", subject="x", created_at=NOW - timedelta(days=days_ago),
                    loan_id=loan.id if loan else None, user_id=self.reader.id)
        db.session.add(m)
        db.session.commit()
        return m

    def test_old_returned_loans_are_deleted_and_counted(self):
        old_ids = {self.loan(self.book, 500, 480).id, self.loan(self.book, 420, 400).id,
                   self.loan(self.other, 700, 690).id}
        recent = self.loan(self.book, 100, 80)
        recent_id = recent.id
        res = run_retention(NOW)
        self.assertEqual(res["loans"], 3)
        remaining = {l.id for l in Loan.query}
        self.assertEqual(remaining, {recent_id})
        db.session.refresh(self.book)
        db.session.refresh(self.other)
        self.assertEqual(self.book.archived_loans, 2)
        self.assertEqual(self.other.archived_loans, 1)
        self.assertFalse(old_ids & remaining)

    def test_active_and_overdue_loans_are_never_deleted(self):
        active = self.loan(self.book, 900)          # overdue for 900 days, not returned
        res = run_retention(NOW)
        self.assertEqual(res["loans"], 0)
        self.assertIsNotNone(db.session.get(Loan, active.id))

    def test_mail_log_cleanup_keeps_entries_of_active_loans(self):
        active = self.loan(self.book, 900)
        keep = {self.mail(800, active, "overdue").id,      # counts for "how often reminded"
                self.mail(10).id}
        gone = {self.mail(800).id,                          # e.g. account mail
                self.mail(790, self.loan(self.other, 800, 700)).id}
        res = run_retention(NOW)
        self.assertEqual(res["mails"], 2)
        ids = {m.id for m in MailLog.query}
        self.assertEqual(ids, keep)
        self.assertFalse(gone & ids)

    def test_period_is_configurable_and_can_be_switched_off(self):
        self.loan(self.book, 200, 190)
        set_setting("retention_months", "6")
        db.session.commit()
        self.assertEqual(run_retention(NOW)["loans"], 1)
        self.loan(self.book, 5000, 4000)
        set_setting("retention_months", "0")
        db.session.commit()
        res = run_retention(NOW)
        self.assertTrue(res["off"])
        self.assertEqual(Loan.query.count(), 1)

    def test_runs_only_once_per_day(self):
        self.loan(self.book, 500, 480)
        self.assertEqual(run_daily(NOW)["loans"], 1)
        self.loan(self.book, 500, 480)
        self.assertIsNone(run_daily(NOW + timedelta(hours=5)))
        self.assertEqual(run_daily(NOW + timedelta(days=1))["loans"], 1)
        self.assertIn("1 alte Ausleihe(n)", get_setting("retention_last_result"))

    def test_book_page_shows_total_including_archived(self):
        self.loan(self.book, 500, 480)
        self.loan(self.book, 30, 20)
        run_retention(NOW)
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        html = self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True)
        self.assertIn("2 Vorgänge", html)
        self.assertIn("1 ältere ohne Einzeldaten", html)

    def test_settings_and_manual_run(self):
        self.loan(self.book, 500, 480)
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        self.c.post("/admin/einstellungen", data={"bereich": "datenschutz", "retention_months": "24"})
        self.assertEqual(get_setting("retention_months"), "24")
        self.c.post("/admin/einstellungen", data={"bereich": "datenschutz", "retention_months": "999"})
        self.assertEqual(get_setting("retention_months"), "12")
        r = self.c.post("/admin/aufbewahrung/jetzt", follow_redirects=True)
        self.assertIn("1 alte Ausleihe(n)", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)
        self.c.post("/logout")
        self.c.post("/login", data={"username": "leser", "password": "geheim12345"})
        self.assertNotEqual(self.c.post("/admin/aufbewahrung/jetzt").status_code, 200)

    def test_deleting_a_user_keeps_statistics(self):
        u = self.user("weg")
        self.loan(self.book, 50, 40, user=u)
        self.loan(self.book, 30, 20, user=u)
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        self.c.post(f"/admin/benutzer/{u.id}/loeschen")
        self.assertIsNone(db.session.get(User, u.id))
        db.session.refresh(self.book)
        self.assertEqual(self.book.archived_loans, 2)

    def test_profile_mentions_retention(self):
        self.c.post("/login", data={"username": "leser", "password": "geheim12345"})
        self.assertIn("12 Monate nach der Rückgabe", self.c.get("/profil").get_data(as_text=True))

    def test_new_column_is_added_to_existing_database(self):
        from sqlalchemy import create_engine, text
        d = tempfile.mkdtemp()
        uri = f"sqlite:///{d}/alt.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        eng = create_engine(uri)
        with eng.begin() as conn:  # state as in 2.1: column missing
            conn.execute(text("ALTER TABLE books DROP COLUMN archived_loans"))
        eng.dispose()
        app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app2.app_context():
            b = Book(title="X")
            db.session.add(b)
            db.session.commit()
            self.assertEqual(b.archived_loans, 0)
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
