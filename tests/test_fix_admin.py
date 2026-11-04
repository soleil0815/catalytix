"""Fixes from the review: administration, mail schedule, backup, help, translation."""
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app, mailer  # noqa: E402
from catalytix.models import Book, Group, Loan, MailLog, Setting, User, db, get_setting, set_setting  # noqa: E402

PW = "geheim12345"


class Base(unittest.TestCase):
    db_uri = "sqlite://"

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": self.db_uri})
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from flask import g

        def _fresh():
            g.pop("_login_user", None)
            g.pop("_catalytix_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        self.admin = self.make("admin", "admin")
        self.bib = self.make("bib", "librarian")
        self.leser = self.make("leser", "reader")
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password(PW)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        return self.c.post("/login", data={"username": name, "password": PW})

    def book(self, title="X"):
        b = Book(title=title, copies=1)
        db.session.add(b)
        db.session.commit()
        return b


# ---------------------------------------------------------------- 1. Reject / last admin

class RejectTests(Base):
    def test_reject_refuses_self_and_admins(self):
        self.admin.self_registered = True
        db.session.commit()
        self.login("admin")
        self.c.post(f"/admin/benutzer/{self.admin.id}/ablehnen")
        self.assertIsNotNone(db.session.get(User, self.admin.id))
        other = self.make("admin2", "admin")
        other.self_registered = True
        other.pending_approval = True
        db.session.commit()
        self.c.post(f"/admin/benutzer/{other.id}/ablehnen")
        self.assertIsNotNone(db.session.get(User, other.id))

    def test_reject_only_pending(self):
        self.leser.self_registered = True     # approved and confirmed – no more rejecting
        self.leser.email_verified = True
        self.leser.pending_approval = False
        db.session.commit()
        self.login("admin")
        self.c.post(f"/admin/benutzer/{self.leser.id}/ablehnen")
        self.assertIsNotNone(db.session.get(User, self.leser.id))

    def test_reject_with_returned_loans(self):
        self.leser.self_registered = True
        self.leser.pending_approval = True
        b = self.book()
        db.session.add(Loan(book_id=b.id, user_id=self.leser.id, due_at=date.today(), returned_at=datetime.now()))
        db.session.commit()
        lid = self.leser.id
        self.login("admin")
        r = self.c.post(f"/admin/benutzer/{lid}/ablehnen")
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, lid))
        self.assertEqual(db.session.get(Book, b.id).archived_loans, 1)

    def test_last_admin_cannot_be_demoted_or_deactivated(self):
        other = self.make("admin2", "admin")
        other.active = False
        db.session.commit()
        self.login("admin")
        # demoting / locking oneself
        self.c.post(f"/admin/benutzer/{self.admin.id}", data={"username": "admin", "role": "reader", "active": "1"})
        self.c.post(f"/admin/benutzer/{self.admin.id}", data={"username": "admin", "role": "admin"})
        db.session.expire_all()
        a = db.session.get(User, self.admin.id)
        self.assertEqual((a.role, a.active), ("admin", True))
        self.c.post(f"/admin/benutzer/{self.admin.id}/loeschen")
        self.assertIsNotNone(db.session.get(User, self.admin.id))

    def test_cleanup_unverified_with_returned_loans(self):
        self.leser.self_registered = True
        self.leser.email_verified = False
        self.leser.created_at = datetime.now() - timedelta(days=10)
        b = self.book()
        db.session.add(Loan(book_id=b.id, user_id=self.leser.id, due_at=date.today(), returned_at=datetime.now()))
        db.session.commit()
        mailer.cleanup_unverified()
        self.assertEqual(User.query.filter_by(username="leser").count(), 0)


# ---------------------------------------------------------------- 2. Unicode digits

class DigitTests(Base):
    def test_superscript_digits_no_500(self):
        self.login("admin")
        for url, data in ((f"/admin/benutzer/{self.leser.id}", {"username": "leser", "role": "reader",
                                                                "active": "1", "groups": "²"}),
                          ("/admin/gruppen/neu", {"name": "g", "members": ["²", "9" * 40]}),
                          ("/admin/einstellungen", {"registration_group_id": "²"}),
                          ("/admin/email", {"action": "save", "smtp_port": "²", "mail_hour": "³"})):
            r = self.c.post(url, data=data)
            self.assertEqual(r.status_code, 302, url)
        self.assertEqual(get_setting("smtp_port"), "587")
        self.assertEqual(get_setting("registration_group_id"), "")


# ---------------------------------------------------------------- 3./4. Mail schedule and overview

class MailScheduleTests(Base):
    def setUp(self):
        super().setUp()
        set_setting("mail_enabled", "1")
        set_setting("smtp_host", "x")
        set_setting("smtp_from", "a@example.org")
        set_setting("mail_digest", "1")
        self.leser.email = "l@example.org"
        self.bib.email = "b@example.org"
        b = self.book()
        db.session.add(Loan(book_id=b.id, user_id=self.leser.id, due_at=date.today() - timedelta(days=1)))
        db.session.commit()

    def ok_smtp(self):
        return mock.patch.object(mailer, "smtp_connect", return_value=mock.MagicMock(send_message=lambda m: {}))

    def test_smtp_failure_is_retried_later(self):
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
        with mock.patch.object(mailer, "smtp_connect", side_effect=mailer.MailError("down")):
            self.assertIn("error", mailer.run_scheduled(now))
        with self.ok_smtp():
            self.assertIsNone(mailer.run_scheduled(now + timedelta(minutes=5)))   # not again right away
            stats = mailer.run_scheduled(now + timedelta(minutes=20))
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(mailer.collect_due(), {})
        with self.ok_smtp():
            self.assertIsNone(mailer.run_scheduled(now + timedelta(hours=2)))   # done today

    def test_digest_once_per_day(self):
        with self.ok_smtp():
            first = mailer.run_notifications()
            second = mailer.run_notifications()   # "send now" / send-reminders in addition
        self.assertEqual(first["digest"], 1)
        self.assertEqual(second["digest"], 0)
        self.assertEqual(MailLog.query.filter_by(kind="digest", user_id=self.bib.id).count(), 1)


# ---------------------------------------------------------------- 5. Backup

class BackupTests(Base):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-bk-")
        self.db_uri = f"sqlite:///{self.tmp}/app.db"
        os.environ["CATALYTIX_BACKUP_DIR"] = os.path.join(self.tmp, "backups")
        super().setUp()
        db.session.execute(db.text("PRAGMA journal_mode=WAL"))

    def tearDown(self):
        os.environ.pop("CATALYTIX_BACKUP_DIR", None)
        super().tearDown()

    def test_no_wal_leftovers_and_collisions_listed(self):
        from catalytix import backup
        now = datetime(2026, 10, 22, 2, 0, 0)
        backup.create_backup("-manuell", now)
        p2 = backup.create_backup("-manuell", now)
        p3 = backup.create_backup("", now)
        p4 = backup.create_backup("", now)
        files = sorted(os.listdir(backup.backup_dir()))
        self.assertFalse([f for f in files if not f.endswith(".db")], files)
        names = [i["name"] for i in backup.list_backups()]
        for p in (p2, p3, p4):
            self.assertIn(p.name, names)
            self.assertIsNotNone(backup.find_backup(p.name))
        self.assertEqual({i["kind"] for i in backup.list_backups() if i["name"] == p4.name}, {"automatisch"})
        self.assertEqual(backup.rotate(1), 3)

    def test_rotate_keeps_fresh_part_files(self):
        from catalytix import backup
        d = backup.backup_dir()
        fresh = d / "catalytix-2026-10-22_020000.part"
        old = d / "catalytix-2026-10-21_020000.part"
        old_wal = d / "catalytix-2026-10-21_020000.part-wal"
        for p in (fresh, old, old_wal):
            p.write_bytes(b"x")
        past = time.time() - 2 * 3600
        os.utime(old, (past, past))
        os.utime(old_wal, (past, past))
        backup.rotate()
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())
        self.assertFalse(old_wal.exists())


# ---------------------------------------------------------------- 6./7. Translated default names

class DefaultNameTests(Base):
    def test_library_name_translated_in_english(self):
        self.leser.language = "en"
        self.leser.email = "l@example.org"
        db.session.commit()
        b = self.book()
        loan = Loan(book_id=b.id, user_id=self.leser.id, due_at=date.today())
        db.session.add(loan)
        db.session.commit()
        _s, text, html = mailer.render_mail("loan", loan=loan, user=self.leser, book=b)
        self.assertIn("Library", text)
        self.assertNotIn("Bibliothek", text + html)
        self.login("leser")
        page = self.c.get("/").get_data(as_text=True)
        self.assertIn("· Library</title>", page)
        self.assertNotIn("Bibliothek", page.split("</title>")[0])

    def test_custom_library_name_unchanged(self):
        set_setting("library_name", "Stadtbücherei")
        db.session.commit()
        self.leser.language = "en"
        db.session.commit()
        self.login("leser")
        self.assertIn("Stadtbücherei", self.c.get("/").get_data(as_text=True))

    def test_security_key_default_name_translated(self):
        from catalytix.models import WebAuthnCredential
        self.leser.language = "en"
        db.session.commit()
        self.login("leser")   # before creating the key – otherwise the login asks for the second factor
        db.session.add(WebAuthnCredential(user_id=self.leser.id, rp_id="localhost", name="Sicherheitsschlüssel",
                                          credential_id="abc", credential_data="eA=="))
        db.session.commit()
        page = self.c.get("/profil/sicherheit").get_data(as_text=True)
        self.assertNotIn("<b>Sicherheitsschlüssel</b>", page)
        self.assertIn("<b>Security key</b>", page)


# ---------------------------------------------------------------- 8. Help

class HelpTests(Base):
    def test_all_loans_help_for_librarian(self):
        self.login("bib")
        page = self.c.get("/ausleihen").get_data(as_text=True)
        self.assertIn("thema=all-loans", page)
        page = self.c.get("/ausleihen?nur=meine").get_data(as_text=True)
        self.assertIn("thema=my-loans", page)

    def test_help_plurals(self):
        set_setting("max_loans", "1")
        set_setting("retention_months", "3")
        set_setting("loan_days", "1")
        db.session.commit()
        self.login("leser")
        de = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("höchstens 1 Buch gleichzeitig", de)
        self.assertIn("1 Tag.", de)
        self.assertIn("3 Monate", de)
        self.leser.language = "en"
        db.session.commit()
        en = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("at most 1 book at a time", en)
        self.assertIn("1 day.", en)


# ---------------------------------------------------------------- 9. LazyString

class LazyStringTests(Base):
    def test_html_and_hash(self):
        from markupsafe import Markup, escape
        from catalytix.i18n import _l
        s = _l("A & B")
        self.assertIsInstance(s.__html__(), Markup)
        self.assertEqual(str(escape(s)), "A &amp; B")
        self.assertEqual(str(Markup("{}").format(s)), "A &amp; B")
        self.assertEqual(hash(s), hash("A & B"))
        self.assertEqual(s, "A & B")
        self.assertEqual(len({s, "A & B"}), 1)


# ---------------------------------------------------------------- 10. Time of the test mail

class TestMailTimeTests(Base):
    def test_sent_time_localized(self):
        set_setting("smtp_from", "a@example.org")
        self.admin.language = "en"
        db.session.commit()
        self.login("admin")
        sent = {}

        def fake_send(to, subject, text, html, **kw):
            sent["text"] = text
            return True, ""
        with mock.patch.object(mailer, "send_mail", side_effect=fake_send):
            self.c.post("/admin/email", data={"action": "test", "test_to": "x@example.org", "smtp_host": "h"})
        from catalytix.i18n import MONTHS_EN
        self.assertIn(MONTHS_EN[datetime.now().month - 1], sent["text"])


if __name__ == "__main__":
    unittest.main()
