"""Befunde aus dem Code Review zu 2.11.0 (Rundumcheck)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_fix_auth import EXT, LAN, PW, Base  # noqa: E402

from catalytix import twofa  # noqa: E402
from catalytix.models import User, db, get_setting, set_setting  # noqa: E402


class StoredPasswordTests(Base):
    def setUp(self):
        super().setUp()
        self.settings(smtp_host="mail.real.example", smtp_user="svc@real.example", smtp_password="TopSecret")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)

    def form(self, **kw):
        base = {"smtp_host": "mail.real.example", "smtp_port": "587", "smtp_security": "starttls",
                "smtp_user": "svc@real.example", "smtp_password": "", "smtp_from": "bib@real.example"}
        base.update(kw)
        return base

    def test_password_not_sent_to_other_server(self):
        from catalytix.views import admin
        with self.app.test_request_context("/", method="POST", data=self.form(smtp_host="evil.example")):
            self.assertEqual(admin._mail_form_cfg()["smtp_password"], "")
        with self.app.test_request_context("/", method="POST", data=self.form()):
            self.assertEqual(admin._mail_form_cfg()["smtp_password"], "TopSecret")

    def test_ldap_password_not_reused_for_other_server(self):
        from catalytix.views import admin
        self.settings(ldap_server="ldaps://dc1.real", ldap_bind_dn="CN=svc,DC=real", ldap_bind_password="LdapPw")
        with self.app.test_request_context("/", method="POST", data={"ldap_server": "ldap://evil", "ldap_bind_dn": "CN=svc,DC=real"}):
            self.assertEqual(admin._ldap_form_cfg()["ldap_bind_password"], "")
        with self.app.test_request_context("/", method="POST", data={"ldap_server": "ldaps://dc1.real", "ldap_bind_dn": "CN=svc,DC=real"}):
            self.assertEqual(admin._ldap_form_cfg()["ldap_bind_password"], "LdapPw")


class TotpDigitsTests(Base):
    def test_non_ascii_digits_no_500(self):
        secret = self.give_totp("admin")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        r = self.c.post("/login/2fa", data={"code": "²²²²²²"}, environ_base=EXT)
        self.assertLess(r.status_code, 500)
        arabic = "".join(chr(0x0660 + int(d)) for d in twofa.totp_now(secret))
        r = self.c.post("/login/2fa", data={"code": arabic}, environ_base=EXT)
        self.assertEqual(r.status_code, 302)          # arabische Ziffern werden angenommen


class GuardScopeTests(Base):
    def login_skipped(self):
        self.settings(mfa_internal="skip")
        self.give_totp("admin")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)

    def test_approve_and_backup_download_need_confirmation(self):
        u = User(username="neu", role="reader", auth_source="local", active=False, pending_approval=True,
                 self_registered=True)
        db.session.add(u)
        db.session.commit()
        self.login_skipped()
        r = self.c.post(f"/admin/benutzer/{u.id}/freigeben", environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])
        db.session.refresh(u)
        self.assertTrue(u.pending_approval)
        r = self.c.get("/admin/datensicherung/catalytix-2026-01-01_020000.db", environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])
        r = self.c.post("/admin/email", data={"action": "save"}, environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])

    def test_destructive_actions_need_confirmation(self):
        self.login_skipped()
        for url, data in (("/admin/aufbewahrung/jetzt", {}), ("/admin/datensicherung", {"backup_enabled": ""}),
                          ("/admin/gruppen/1/loeschen", {})):
            r = self.c.post(url, data=data, environ_base=LAN)
            self.assertIn("/login/2fa", r.headers.get("Location", ""), url)
        self.assertEqual(get_setting("backup_enabled"), "1")

    def test_branding_still_free(self):
        self.login_skipped()
        self.c.post("/admin/erscheinungsbild", data={"action": "save", "brand_accent": "#123456"}, environ_base=LAN)
        self.assertEqual(get_setting("brand_accent"), "#123456")


if __name__ == "__main__":
    unittest.main()
