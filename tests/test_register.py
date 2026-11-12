"""Tests of self-registration."""
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-reg-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import Group, MailLog, User, db, set_setting  # noqa: E402
from catalytix.views import register as reg  # noqa: E402


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        admin = User(username="admin", role="admin", email="admin@bib.de")
        admin.set_password("geheim123")
        db.session.add(admin)
        db.session.commit()
        self.c = self.app.test_client()
        reg._REG_LOG.clear()
        self.sent = []
        patcher = mock.patch("catalytix.mailer.send_mail", side_effect=self._fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _fake_send(self, to, subject, text, html, **kw):
        self.sent.append({"to": to, "subject": subject, "text": text})
        return True, ""

    def configure(self, mode="approval", verify="0", mail="0", **extra):
        set_setting("registration_mode", mode)
        set_setting("registration_verify_email", verify)
        set_setting("mail_enabled", mail)
        set_setting("smtp_host", "mail.test" if mail == "1" else "")
        for k, v in extra.items():
            set_setting(k, v)
        db.session.commit()

    def form(self, **kw):
        data = {"display_name": "Lena Schmidt", "email": "lena@schule.de", "username": "lena.s",
                "password": "einlangespasswort", "password2": "einlangespasswort"}
        data.update(kw)
        return data

    def submit(self, **kw):
        self.c.get("/registrieren")
        with self.c.session_transaction() as s:   # simulate the minimum fill-in time
            s["reg_started"] = time.time() - 10
        return self.c.post("/registrieren", data=self.form(**kw))

    def test_disabled_by_default(self):
        self.assertEqual(self.c.get("/registrieren").status_code, 404)
        self.assertNotIn("registrieren", self.c.get("/login").get_data(as_text=True))

    def test_approval_flow(self):
        self.configure("approval", mail="1", verify="0")
        self.assertIn("Jetzt registrieren", self.c.get("/login").get_data(as_text=True))
        r = self.submit()
        self.assertIn("freigegeben werden", r.get_data(as_text=True))
        u = User.query.filter_by(username="lena.s").one()
        self.assertTrue(u.pending_approval)
        self.assertFalse(u.active)
        self.assertEqual(u.role, "reader")
        self.assertTrue(any(m["to"] == "admin@bib.de" for m in self.sent))   # admin notified
        r = self.c.post("/login", data={"username": "lena.s", "password": "einlangespasswort"})
        self.assertIn("wartet noch auf die Freigabe", r.get_data(as_text=True))
        # wrong password reveals nothing
        r = self.c.post("/login", data={"username": "lena.s", "password": "falschfalsch"})
        self.assertIn("Benutzername oder Passwort ist falsch", r.get_data(as_text=True))
        # admin approves
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("1 neue Registrierung", html)
        self.assertIn("wartet auf Freigabe", self.c.get("/admin/benutzer?status=neu").get_data(as_text=True))
        self.c.post(f"/admin/benutzer/{u.id}/freigeben")
        db.session.refresh(u)
        self.assertTrue(u.active)
        self.assertTrue(any(m["subject"] == "Ihr Konto ist freigeschaltet" for m in self.sent))
        self.c.post("/logout")
        r = self.c.post("/login", data={"username": "lena.s", "password": "einlangespasswort"})
        self.assertEqual(r.status_code, 302)

    def test_reject(self):
        self.configure("approval")
        self.submit()
        u = User.query.filter_by(username="lena.s").one()
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})
        self.c.post(f"/admin/benutzer/{u.id}/ablehnen")
        self.assertIsNone(User.query.filter_by(username="lena.s").first())

    def test_open_mode_logs_in_directly_with_group(self):
        g = Group(name="Leser extern")
        db.session.add(g)
        db.session.commit()
        self.configure("open", registration_group_id=str(g.id))
        r = self.submit()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)
        u = User.query.filter_by(username="lena.s").one()
        self.assertTrue(u.active)
        self.assertEqual([x.name for x in u.groups], ["Leser extern"])

    def test_email_verification(self):
        self.configure("open", verify="1", mail="1", app_base_url="https://bib.schule.de")
        r = self.submit()
        self.assertIn("Bestätigungslink", r.get_data(as_text=True))
        u = User.query.filter_by(username="lena.s").one()
        self.assertFalse(u.active)
        self.assertFalse(u.email_verified)
        r = self.c.post("/login", data={"username": "lena.s", "password": "einlangespasswort"})
        self.assertIn("bestätigen Sie zuerst", r.get_data(as_text=True))
        self.c.post("/registrieren/erneut-senden")
        verify_mails = [m for m in self.sent if m["to"] == "lena@schule.de"]
        self.assertEqual(len(verify_mails), 2)
        link = re.search(r"(/registrieren/bestaetigen/\S+)", verify_mails[-1]["text"]).group(1)
        r = self.c.get(link)
        self.assertIn("Konto ist jetzt aktiv", r.get_data(as_text=True))
        db.session.refresh(u)
        self.assertTrue(u.active)
        self.assertEqual(self.c.get("/registrieren/bestaetigen/kaputt").status_code, 404)

    def test_admin_mail_without_base_url_has_no_foreign_link(self):
        self.configure("approval", mail="1", app_base_url="", registration_notify_admins="1")
        evil = "http://evil.example"
        self.c.get("/registrieren", base_url=evil)
        with self.c.session_transaction(base_url=evil) as sess:
            sess["reg_started"] = time.time() - 10
        self.c.post("/registrieren", data=self.form(), base_url=evil)
        admin_mails = [m for m in self.sent if m["to"] == "admin@bib.de"]
        self.assertEqual(len(admin_mails), 1)
        self.assertNotIn("evil.example", admin_mails[0]["text"])

    def test_links_never_from_host_header(self):
        self.configure("open", verify="1", mail="1", app_base_url="https://bib.schule.de")
        evil = "http://evil.example"
        self.c.get("/registrieren", base_url=evil)
        with self.c.session_transaction(base_url=evil) as sess:
            sess["reg_started"] = time.time() - 10
        self.c.post("/registrieren", data=self.form(), base_url=evil)
        texts = " ".join(m["text"] for m in self.sent)
        self.assertNotIn("evil.example", texts)
        self.assertIn("https://bib.schule.de/registrieren/bestaetigen/", texts)

    def test_verification_without_base_url_refused(self):
        self.configure("open", verify="1", mail="1", app_base_url="")
        self.assertEqual(self.c.get("/registrieren").status_code, 503)
        self.assertEqual(self.sent, [])

    def test_verification_skipped_without_mail(self):
        self.configure("open", verify="1", mail="0")
        self.submit()
        self.assertTrue(User.query.filter_by(username="lena.s").one().active)

    def test_validation(self):
        self.configure("open", registration_domains="schule.de", privacy_url="https://x.de/ds")
        cases = [
            ({"username": "ab"}, "3–40 Zeichen"),
            ({"username": "Admin"}, "nicht verfügbar"),
            ({"username": "admin2", "email": "a@gmail.com"}, "keine Registrierung möglich"),
            ({"password": "kurz", "password2": "kurz"}, "mindestens 10"),
            ({"password2": "anderespasswort"}, "stimmen nicht"),
            ({}, "Datenschutzhinweise"),
        ]
        for data, msg in cases:
            with self.subTest(msg=msg):
                r = self.submit(**data)
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, r.get_data(as_text=True))
        r = self.submit(email="x@eltern.schule.de", privacy="1")       # subdomain allowed
        self.assertEqual(r.status_code, 302)
        self.c.post("/logout")
        r = self.submit(username="lena2", email="X@ELTERN.schule.de", privacy="1")
        self.assertIn("bereits ein Konto", r.get_data(as_text=True))
        r = self.submit(username="LENA.S", email="neu@schule.de", privacy="1")
        self.assertIn("bereits vergeben", r.get_data(as_text=True))

    def test_bot_protection_and_rate_limit(self):
        self.configure("open")
        self.c.get("/registrieren")
        with self.c.session_transaction() as s:
            s["reg_started"] = time.time() - 10
        self.c.post("/registrieren", data=self.form(homepage_url="http://spam"))
        self.c.get("/registrieren")   # submitted too quickly
        self.c.post("/registrieren", data=self.form(username="schnell", email="s@schule.de"))
        self.assertEqual(User.query.filter(User.username != "admin").count(), 0)
        for i in range(5):
            self.c.post("/logout")
            self.submit(username=f"user{i}", email=f"u{i}@schule.de")
        self.c.post("/logout")
        r = self.submit(username="user9", email="u9@schule.de")
        self.assertEqual(r.status_code, 429)

    def test_ad_username_blocked(self):
        self.configure("open", ldap_enabled="1")
        with mock.patch.object(reg, "_exists_in_directory", return_value=True):
            r = self.submit()
        self.assertIn("bereits vergeben", r.get_data(as_text=True))
        with mock.patch.object(reg, "_exists_in_directory", return_value=None):
            r = self.submit()
        self.assertIn("gerade nicht möglich", r.get_data(as_text=True))
        self.assertIsNone(User.query.filter_by(username="lena.s").first())

    def test_cleanup_unverified(self):
        from catalytix.mailer import cleanup_unverified
        old = User(username="alt", email="a@b.de", self_registered=True, email_verified=False, active=False,
                   created_at=datetime.now() - timedelta(days=8))
        new = User(username="neu", email="n@b.de", self_registered=True, email_verified=False, active=False)
        db.session.add_all([old, new])
        db.session.commit()
        self.assertEqual(cleanup_unverified(), 1)
        self.assertEqual({u.username for u in User.query}, {"admin", "neu"})

    def test_settings_saved(self):
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})
        self.c.post("/admin/einstellungen", data={"bereich": "konten", "registration_mode": "approval",
                                                  "registration_domains": "schule.de"})
        self.c.post("/admin/einstellungen", data={"bereich": "datenschutz", "privacy_url": "javascript:alert(1)"})
        from catalytix.models import get_setting
        self.assertEqual(get_setting("registration_mode"), "approval")
        self.assertEqual(get_setting("privacy_url"), "")
        self.assertEqual(get_setting("registration_verify_email"), "0")




class EmailChangeTests(RegisterTests):
    def setUp(self):
        super().setUp()
        self.configure("open", verify="1", mail="1", app_base_url="https://bib.schule.de")
        u = User(username="lena.s", display_name="Lena", email="lena@schule.de", role="reader",
                 self_registered=True, email_verified=True, active=True)
        u.set_password("einlangespasswort")
        db.session.add_all([u, User(username="tom", email="tom@schule.de", role="reader")])
        db.session.commit()
        self.c.post("/login", data={"username": "lena.s", "password": "einlangespasswort"})
        self.sent.clear()

    def change(self, mail):
        return self.c.post("/profil", data={"action": "notify", "email": mail, "email_notifications": "1"})

    def test_new_address_only_after_confirmation(self):
        self.change("lena.neu@schule.de")
        u = User.query.filter_by(username="lena.s").one()
        self.assertEqual(u.email, "lena@schule.de")
        self.assertEqual([m["to"] for m in self.sent], ["lena.neu@schule.de"])
        link = re.search(r"https://bib\.schule\.de(/profil/email-bestaetigen/\S+)", self.sent[0]["text"]).group(1)
        self.c.get(link)
        db.session.refresh(u)
        self.assertEqual(u.email, "lena.neu@schule.de")

    def test_address_of_other_account_rejected(self):
        r = self.change("TOM@schule.de")
        self.assertEqual(self.sent, [])
        self.assertEqual(User.query.filter_by(username="lena.s").one().email, "lena@schule.de")
        self.assertIn("anderen Konto", self.c.get(r.headers["Location"]).get_data(as_text=True))

    def test_link_invalid_after_password_change(self):
        self.change("lena.neu@schule.de")
        link = re.search(r"(/profil/email-bestaetigen/\S+)", self.sent[0]["text"]).group(1)
        u = User.query.filter_by(username="lena.s").one()
        u.auth_epoch = (u.auth_epoch or 0) + 1
        db.session.commit()
        self.assertEqual(self.c.get(link).status_code, 404)



# do not run the parent class's tests here again
for _name in dir(RegisterTests):
    if _name.startswith("test_") and _name not in EmailChangeTests.__dict__:
        setattr(EmailChangeTests, _name, None)


if __name__ == "__main__":
    unittest.main()
