"""Security and logic checks from the review (version 2.8.0)."""
import io
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from PIL import Image  # noqa: E402

from catalytix import LDAP_RECHECK_SECONDS, create_app, mailer  # noqa: E402
from catalytix.ldap_auth import LDAPError, LDAPUser  # noqa: E402
from catalytix.models import Book, Group, Loan, Setting, User, db, get_setting, set_setting  # noqa: E402
from catalytix.views import safe_next  # noqa: E402

PW = "geheim12345"
ROOT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # The test keeps an app context open; Flask shares it with every request – including "g".
        # So that no user sticks around from one request to the next (or between clients):
        from flask import g

        def _fresh_user():
            g.pop("_login_user", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh_user)
        self.admin = self.make("admin", "admin")
        self.bib = self.make("bib", "librarian")
        self.leser = self.make("leser", "reader")
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, source="local"):
        u = User(username=name, display_name=name.title(), role=role, auth_source=source)
        if source == "local":
            u.set_password(PW)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name, client=None, pw=PW):
        c = client or self.c
        return c.post("/login", data={"username": name, "password": pw})

    def logged_in(self, client=None):
        r = (client or self.c).get("/")
        return r.status_code == 200


class SessionTests(Base):
    def test_password_change_logs_out_other_devices_only(self):
        other = self.app.test_client()
        self.login("leser")
        self.login("leser", other)
        self.assertTrue(self.logged_in(other))
        r = self.c.post("/profil", data={"current": PW, "new": "neuesPasswort1", "new2": "neuesPasswort1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.logged_in())
        self.assertFalse(self.logged_in(other))

    def test_logout_everywhere(self):
        other = self.app.test_client()
        self.login("leser")
        self.login("leser", other)
        self.c.post("/abmelden-ueberall")
        self.assertTrue(self.logged_in())
        self.assertFalse(self.logged_in(other))

    def test_2fa_reset_ends_sessions(self):
        other = self.app.test_client()
        self.login("leser", other)
        self.login("admin")
        self.c.post(f"/admin/benutzer/{self.leser.id}/2fa-zuruecksetzen")
        self.assertFalse(self.logged_in(other))

    def test_legacy_session_id_still_accepted(self):
        with self.c.session_transaction() as s:
            s["_user_id"] = str(self.leser.id)
            s["_fresh"] = True
        self.assertTrue(self.logged_in())

    def test_min_password_length(self):
        self.login("leser")
        self.c.post("/profil", data={"current": PW, "new": "kurz12345", "new2": "kurz12345"})
        db.session.refresh(self.leser)
        self.assertTrue(self.leser.check_password(PW))


class RedirectTests(Base):
    def test_safe_next(self):
        for bad in ("//evil.example", "https://evil.example", "/\t/evil.example", "/\\evil", "/\n/evil", None, ""):
            self.assertIsNone(safe_next(bad), bad)
        self.assertEqual(safe_next("/buecher?q=1"), "/buecher?q=1")

    def test_admin_actions_ignore_foreign_next(self):
        self.login("admin")
        for action in ("2fa-zuruecksetzen", "entsperren", "freigeben"):
            r = self.c.post(f"/admin/benutzer/{self.leser.id}/{action}", data={"next": "https://evil.example/"})
            self.assertNotIn("evil", r.headers["Location"], action)


class InputBoundsTests(Base):
    def test_setting_bounds(self):
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"bereich": "ausleihe", "loan_days": "999999", "extension_days": "-5",
                                                   "max_extensions": "100000", "max_loans": "5000000"})
        self.assertEqual(get_setting("loan_days"), "3650")
        self.assertEqual(int(get_setting("extension_days")), 1)
        self.assertEqual(get_setting("max_extensions"), "100")
        self.assertEqual(get_setting("max_loans"), "1000")

    def test_group_color_sanitised(self):
        self.login("admin")
        self.c.post("/admin/gruppen/neu", data={"name": "X", "color": "red;background:url(x)"})
        self.assertEqual(Group.query.one().color, "#2f6f62")

    def test_desk_bad_ids(self):
        self.login("bib")
        r = self.c.post("/theke", data={"modus": "rueckgabe", "loan_id": "abc"})
        self.assertEqual(r.status_code, 404)
        r = self.c.post("/theke", data={"modus": "ausleihe", "book_id": "1 OR 1", "benutzer": "leser"})
        self.assertEqual(r.status_code, 404)

    def test_due_date_range(self):
        b = Book(title="Faust", copies=1)
        db.session.add(b)
        db.session.commit()
        self.login("bib")
        far = (date.today() + timedelta(days=5000)).isoformat()
        self.c.post(f"/buecher/{b.id}/ausleihen", data={"user_id": self.leser.id, "due_at": far})
        self.assertEqual(Loan.query.count(), 0)
        past = (date.today() - timedelta(days=1)).isoformat()
        self.c.post(f"/buecher/{b.id}/ausleihen", data={"user_id": self.leser.id, "due_at": past})
        self.assertEqual(Loan.query.count(), 0)
        ok = (date.today() + timedelta(days=10)).isoformat()
        self.c.post(f"/buecher/{b.id}/ausleihen", data={"user_id": self.leser.id, "due_at": ok})
        self.assertEqual(Loan.query.one().due_at, date.today() + timedelta(days=10))

    def test_external_username_throttle(self):
        from catalytix import lockout
        with mock.patch("catalytix.netzone.is_internal", return_value=False), \
                mock.patch.object(lockout, "user_throttled", return_value=True):
            r = self.login("leser")
        self.assertEqual(r.status_code, 429)


class UploadAndExportTests(Base):
    def test_cover_upload_rejects_non_image(self):
        b = Book(title="Faust", copies=1)
        db.session.add(b)
        db.session.commit()
        self.login("bib")
        self.c.post(f"/buecher/{b.id}/bearbeiten", data={
            "title": "Faust", "copies": "1",
            "cover_upload": (io.BytesIO(b"<svg onload=alert(1)>"), "x.jpg")}, content_type="multipart/form-data")
        db.session.refresh(b)
        self.assertFalse(b.cover_file)

    def test_cover_upload_reencoded(self):
        b = Book(title="Faust", copies=1)
        db.session.add(b)
        db.session.commit()
        buf = io.BytesIO()
        Image.effect_noise((40, 60), 50).convert("RGB").save(buf, "PNG")
        buf.seek(0)
        self.login("bib")
        self.c.post(f"/buecher/{b.id}/bearbeiten", data={"title": "Faust", "copies": "1",
                                                       "cover_upload": (buf, "c.png")},
                    content_type="multipart/form-data")
        db.session.refresh(b)
        self.assertTrue(b.cover_file)
        r = self.c.get(f"/covers/buch/{b.id}", follow_redirects=True)
        self.assertEqual(r.mimetype, "image/jpeg")

    def test_csv_formula_escaped(self):
        db.session.add(Book(title="=HYPERLINK(\"http://x\")", authors="+49 123", copies=1))
        db.session.commit()
        self.login("bib")
        text = self.c.get("/buecher/export.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'+49 123", text)


class MailTests(Base):
    def cfg(self):
        return mailer.SmtpConfig(host="smtp.example", port=25, security="none", user="", password="",
                                 sender="bib@example.org", sender_name="Bib", reply_to="", verify=True)

    def test_subject_without_newlines(self):
        msg = mailer.build_message(self.cfg(), "a@example.org", "Faust\r\nBcc: x@evil", "Text")
        self.assertNotIn("\n", msg["Subject"])
        self.assertIsNone(msg["Bcc"])

    def test_invalid_recipient_no_crash(self):
        for bad in ("a@example.org, b@evil.example", "a b@example.org", "<a@x.de>", "x"):
            self.assertFalse(mailer.valid_email(bad), bad)
        smtp = mock.MagicMock()
        ok, err = mailer.send_mail("a@x.org,b@evil.example", "S", "T", kind="test", cfg=self.cfg(), smtp=smtp)
        self.assertFalse(ok)
        smtp.send_message.assert_not_called()

    def test_profile_rejects_bad_email(self):
        self.login("leser")
        self.c.post("/profil", data={"action": "notify", "email": "a@x.de, b@y.de"})
        db.session.refresh(self.leser)
        self.assertNotIn(",", self.leser.email or "")


class SecretStorageTests(Base):
    def test_secrets_encrypted_at_rest(self):
        set_setting("smtp_password", "SuperGeheim")
        db.session.commit()
        raw = db.session.get(Setting, "smtp_password").value
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("SuperGeheim", raw)
        self.assertEqual(get_setting("smtp_password"), "SuperGeheim")

    def test_plaintext_migrated(self):
        from catalytix.models import encrypt_stored_secrets
        db.session.merge(Setting(key="ldap_bind_password", value="alt-klartext"))
        db.session.commit()
        encrypt_stored_secrets()
        db.session.commit()
        self.assertTrue(db.session.get(Setting, "ldap_bind_password").value.startswith("enc:"))
        self.assertEqual(get_setting("ldap_bind_password"), "alt-klartext")


class CspTests(Base):
    def test_header(self):
        r = self.c.get("/login")
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_no_inline_script_in_templates(self):
        pat = re.compile(r"\son[a-z]+\s*=|<script(?![^>]*\s(?:src=|type=\"application/json\"))[^>]*>", re.I)
        offenders = []
        for f in (ROOT / "catalytix" / "templates").rglob("*.html"):
            for m in pat.finditer(f.read_text(encoding="utf-8")):
                offenders.append(f"{f.name}: {m.group(0)}")
        self.assertEqual(offenders, [])


class _Ctx:
    def __init__(self, m):
        self.m = m

    def __enter__(self):
        return self.m

    def __exit__(self, *a):
        return False


class LdapRecheckTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make("adleser", "reader", source="ldap")
        set_setting("ldap_enabled", True)
        set_setting("ldap_bind_dn", "CN=svc,DC=example,DC=org")
        db.session.commit()
        from flask_login import login_user  # noqa: F401
        with self.c.session_transaction() as s:
            s["_user_id"] = f"{self.ad.id}:{self.ad.auth_epoch or 0}"
            s["_fresh"] = True

    def directory(self, **kw):
        """Replace the directory; returns the mock of check_account."""
        patcher = mock.patch("catalytix.ldap_auth.Directory")
        cls = patcher.start()
        self.addCleanup(patcher.stop)
        for k, v in kw.items():
            setattr(cls.return_value.check_account, k, v)
        return _Ctx(cls.return_value.check_account)

    def info(self, role="reader"):
        return LDAPUser(username="adleser", dn="CN=adleser,DC=example,DC=org", display_name="AD Leser",
                        email="ad@example.org", role=role)

    def test_disabled_account_logged_out(self):
        with self.directory(return_value=None):
            r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_lost_role_logged_out(self):
        with self.directory(return_value=self.info(role=None)):
            self.assertFalse(self.logged_in())

    def test_ok_is_cached_and_role_synced(self):
        with self.directory(return_value=self.info("librarian")) as m:
            self.assertTrue(self.logged_in())
            self.assertTrue(self.logged_in())
        self.assertEqual(m.call_count, 1)
        db.session.refresh(self.ad)
        self.assertEqual(self.ad.role, "librarian")

    def test_directory_down_fails_open(self):
        with self.directory(side_effect=LDAPError("down")):
            self.assertTrue(self.logged_in())
        with self.c.session_transaction() as s:
            self.assertLess(time.time() - s["ldap_ok_at"], LDAP_RECHECK_SECONDS)

    def test_rechecked_after_interval(self):
        with self.directory(return_value=self.info()) as m:
            self.logged_in()
            with self.c.session_transaction() as s:
                s["ldap_ok_at"] -= LDAP_RECHECK_SECONDS + 1
            self.logged_in()
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
