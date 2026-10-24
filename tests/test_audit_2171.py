"""Code-Audit zu 2.17.1: Regressionstests für die behobenen Befunde."""
import os
import smtplib
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_mail import MailBase  # noqa: E402
from test_people import PW, Base  # noqa: E402

from catalytix import ldap_auth, mailer, news  # noqa: E402
from catalytix.models import Book, Group, Loan, LoginAttempt, MailLog, User, db, set_setting  # noqa: E402


class GroupScopeTests(Base):
    """Ein Gruppen-Verwalter, der seine letzte Gruppe verliert, darf nicht plötzlich alles verwalten."""

    def setUp(self):
        super().setUp()
        self.other = Group(name="Physik")
        db.session.add(self.other)
        self.pbook = Book(title="Nur Physik", copies=1, group=self.other)
        db.session.add(self.pbook)
        bib = User.query.filter_by(username="bib").one()
        bib.groups = [self.group]
        admin = User(username="chef", display_name="Chef", role="admin", auth_source="local")
        admin.set_password(PW)
        db.session.add(admin)
        db.session.commit()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def test_adding_a_group_binds(self):
        self.assertTrue(User.query.filter_by(username="bib").one().group_bound)

    def test_deleted_group_does_not_escalate(self):
        bib = User.query.filter_by(username="bib").one()
        self.assertFalse(bib.can_manage(self.pbook))
        self.login("chef")
        self.c.post(f"/admin/gruppen/{self.group.id}/loeschen")
        db.session.expire_all()
        bib = User.query.filter_by(username="bib").one()
        self.assertEqual(bib.groups, [])
        self.assertFalse(bib.is_global_manager)
        self.assertFalse(bib.can_manage(self.pbook))
        self.login("bib")
        self.assertEqual(self.c.get(f"/buecher/{self.pbook.id}/bearbeiten").status_code, 403)

    def test_removed_by_directory_sync_does_not_escalate(self):
        bib = User.query.filter_by(username="bib").one()
        bib.groups.remove(self.group)       # wie beim AD-Abgleich
        db.session.commit()
        self.assertFalse(bib.is_global_manager)

    def test_admin_can_make_global_explicitly(self):
        bib = User.query.filter_by(username="bib").one()
        bib.groups = []
        db.session.commit()
        self.login("chef")
        form = {"username": "bib", "display_name": "Bib", "role": "librarian", "active": "1"}
        page = self.c.get(f"/admin/benutzer/{bib.id}").get_data(as_text=True)
        self.assertIn("manage_all", page)
        self.c.post(f"/admin/benutzer/{bib.id}", data=form)                     # ohne Bestätigung: bleibt gebunden
        db.session.expire_all()
        self.assertFalse(User.query.filter_by(username="bib").one().is_global_manager)
        self.c.post(f"/admin/benutzer/{bib.id}", data={**form, "manage_all": "1"})
        db.session.expire_all()
        self.assertTrue(User.query.filter_by(username="bib").one().is_global_manager)

    def test_new_librarian_without_group_stays_global(self):
        u = User(username="neu", role="librarian", auth_source="local")
        db.session.add(u)
        db.session.commit()
        self.assertTrue(u.is_global_manager)


class DeskTests(Base):
    def test_auto_return_only_on_exact_match(self):
        leser = User.query.filter_by(username="leser").one()
        self.book.authors = "Goethe"
        self.book.signature = "GOE-1"
        db.session.add(Loan(book=self.book, user=leser, due_at=date.today() + timedelta(days=5)))
        db.session.commit()
        self.c.post("/theke", data={"modus": "rueckgabe", "code": "Goethe", "auto": "1"})
        self.assertIsNone(Loan.query.one().returned_at)                          # ungefähr: nichts passiert
        self.c.post("/theke", data={"modus": "rueckgabe", "code": "goe-1", "auto": "1"})
        self.assertIsNotNone(Loan.query.one().returned_at)                       # Signatur: zurückgenommen

    def test_return_twice_notifies_once(self):
        leser = User.query.filter_by(username="leser").one()
        loan = Loan(book=self.book, user=leser, due_at=date.today() + timedelta(days=5))
        db.session.add(loan)
        db.session.commit()
        from catalytix.views import loans as views
        with mock.patch.object(views, "notify_return") as notify, self.app.test_request_context():
            from flask_login import login_user
            login_user(User.query.filter_by(username="bib").one())
            views.return_loan(loan)
            stamp = Loan.query.one().returned_at
            views.return_loan(loan)                       # zweite Rückgabe derselben Ausleihe (Doppelklick)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(Loan.query.one().returned_at, stamp)


class ReminderTests(MailBase):
    def test_extension_restarts_reminders_and_overdue_notices(self):
        today = date.today()
        b = self.book("Momo")
        loan = self.loan(self.lena, b, today + timedelta(days=1))
        mailer.run_notifications(today)
        self.assertEqual(len(self.smtp.messages), 1)
        # verlängert: neue Frist, für die wieder erinnert wird
        loan.due_at = today + timedelta(days=1)
        loan.due_changed_at = datetime.now() + timedelta(seconds=1)
        db.session.commit()
        self.assertIn(self.lena.id, mailer.collect_due(today))
        # Mahnungen: Höchstzahl gilt je Frist
        loan.due_at = today - timedelta(days=30)
        db.session.commit()
        for _ in range(3):
            db.session.add(MailLog(kind="overdue", status="sent", loan_id=loan.id, user_id=self.lena.id,
                                   created_at=datetime.now() - timedelta(days=60)))
        db.session.commit()
        self.assertIn(self.lena.id, mailer.collect_due(today))   # alte Mahnungen vor der Verlängerung zählen nicht

    def test_run_lock(self):
        token = mailer._claim_run()
        self.assertIsNotNone(token)
        self.assertEqual(mailer.run_notifications().get("skipped"), "running")
        mailer._release_run(token)
        self.assertNotEqual(mailer.run_notifications().get("skipped"), "running")


class SharedSmtpTests(unittest.TestCase):
    def test_reconnects_after_disconnect(self):
        first, second = mock.Mock(), mock.Mock()
        first.send_message.side_effect = smtplib.SMTPServerDisconnected("bye")
        second.send_message.return_value = {}
        with mock.patch.object(mailer, "smtp_connect", side_effect=[first, second]):
            shared = mailer.SharedSmtp(mock.Mock())
            self.assertEqual(shared.send("msg"), {})
        second.send_message.assert_called_once_with("msg")
        self.assertFalse(shared.reconnect_failed)


class DirectoryTests(unittest.TestCase):
    def test_escaped_comma_is_not_an_ou(self):
        rule = "OU=Bib-Admins,OU=Staff,DC=x,DC=de"
        self.assertFalse(ldap_auth.rule_matches(rule, r"CN=Mallory\, OU=Bib-Admins,OU=Staff,DC=x,DC=de", set()))
        self.assertFalse(ldap_auth.rule_matches(rule, r"CN=Mallory\,OU=Bib-Admins,OU=Staff,DC=x,DC=de", set()))
        self.assertTrue(ldap_auth.rule_matches(rule, "CN=Anna,OU=Bib-Admins,OU=Staff,DC=x,DC=de", set()))
        self.assertTrue(ldap_auth.rule_matches(rule, "cn=Anna , ou=bib-admins,ou=staff,dc=x,dc=de", set()))
        self.assertFalse(ldap_auth.rule_matches(rule, "CN=Bob,OU=Staff,DC=x,DC=de", set()))

    def test_account_expiry(self):
        def ad(dt):
            return int((dt - datetime(1601, 1, 1)).total_seconds() * 10_000_000)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        past, future = ad(now - timedelta(days=1)), ad(now + timedelta(days=30))
        self.assertTrue(ldap_auth._disabled({"accountExpires": [str(past)]}))
        self.assertFalse(ldap_auth._disabled({"accountExpires": [str(future)]}))
        self.assertFalse(ldap_auth._disabled({"accountExpires": ["0"]}))
        self.assertFalse(ldap_auth._disabled({"accountExpires": [str(ldap_auth.AD_NEVER)]}))
        self.assertTrue(ldap_auth._disabled({"userAccountControl": ["514"]}))
        self.assertTrue(ldap_auth._disabled({"accountExpires": [datetime.now() - timedelta(days=1)]}))

    def test_lockout_name_normalised(self):
        from catalytix import lockout
        self.assertEqual(lockout.norm("ＡＮＮＡ"), lockout.norm("anna"))


class LocalAccountTests(Base):
    def test_desk_recheck_leaves_local_account_alone(self):
        from catalytix import people
        u = User.query.filter_by(username="anna").one()
        u.ldap_gone_at = datetime.now()
        db.session.commit()
        with mock.patch("catalytix.ldap_auth.Directory") as directory:
            self.assertEqual(people._recheck_gone(u), "")
            directory.assert_not_called()
        self.assertEqual(u.auth_source, "local")


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        from test_register import RegisterTests
        self.helper = RegisterTests("test_disabled_by_default")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)     # u. a. den Mail-Patch der Hilfsklasse wieder entfernen
        self.addCleanup(self.helper.tearDown)

    def test_existence_probes_are_limited(self):
        h = self.helper
        h.configure("approval")
        existing = User(username="taken", email="t@schule.de", auth_source="local")
        db.session.add(existing)
        db.session.commit()
        codes = [h.submit(username="taken", email=f"x{i}@schule.de").status_code for i in range(12)]
        self.assertEqual(codes[:10], [400] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_admin_lock_survives_verification_link(self):
        h = self.helper
        h.configure("open", verify="1", mail="1", app_base_url="https://bib.test")
        h.submit()
        u = User.query.filter_by(username="lena.s").one()
        self.assertFalse(u.email_verified)
        link = [m for m in h.sent if "bestaetigen" in m["text"]][0]["text"]
        token = link.split("/registrieren/bestaetigen/")[1].split()[0]
        admin = User(username="chef", role="admin", auth_source="local")
        admin.set_password("geheim12345")
        db.session.add(admin)
        db.session.commit()
        h.c.post("/login", data={"username": "chef", "password": "geheim12345"})
        h.c.post(f"/admin/benutzer/{u.id}", data={"username": "lena.s", "display_name": "Lena", "role": "reader",
                                                    "email": "lena@schule.de"})          # „Konto aktiv“ aus
        h.c.post("/logout")
        h.c.get(f"/registrieren/bestaetigen/{token}")
        db.session.expire_all()
        self.assertFalse(User.query.filter_by(username="lena.s").one().active)


class ApiSessionTests(Base):
    def test_api_without_session_gets_json_401(self):
        self.c.post("/logout")
        r = self.c.get("/api/katalog?q=Faust")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.c.get("/ausleihen").status_code, 302)              # Seiten leiten weiter um


class NewsHintTests(Base):
    def test_security_first_even_from_older_version(self):
        from catalytix import __version__
        item = {"audience": "all", "de": {"title": "x", "text": "t"}, "en": {"title": "x", "text": "t"}}
        entries = ({"version": __version__, "items": [dict(item, kind="new", de={"title": "Neu", "text": "t"})]},
                   {"version": "0.0.5", "items": [dict(item, kind="security", de={"title": "Sicher", "text": "t"})]})
        bib = User.query.filter_by(username="bib").one()
        bib.news_seen = "0.0.1"
        db.session.commit()
        news._entries.cache_clear()
        with mock.patch.object(news, "_entries", return_value=entries):
            html = self.c.get("/").get_data(as_text=True)
        self.assertIn("Neu in Version 0.0.5:", html)
        self.assertIn("Sicher", html)


if __name__ == "__main__":
    unittest.main()
