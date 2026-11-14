"""3.3.1: the overview suggests a passkey to accounts without one (the browser decides whether the device can hold
one); "Nicht mehr anzeigen" hides it for the account. The passkey setting sits at the top of the security tab."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix.models import User, WebAuthnCredential, db, set_setting  # noqa: E402


class PasskeyHintTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.user = self.make_user("leser")
        self.login("leser")

    def enable(self, on=True):
        set_setting("passkey_login", "1" if on else "0")
        db.session.commit()

    def overview(self):
        return self.c.get("/").get_data(as_text=True)

    def add_key(self, passkey):
        db.session.add(WebAuthnCredential(user_id=self.user.id, credential_id=f"id-{passkey}", credential_data="x",
                                          rp_id="localhost", passkey=passkey))
        db.session.commit()

    def test_only_when_passkeys_allowed(self):
        self.enable(False)
        self.assertNotIn("data-passkey-hint", self.overview())
        self.enable()
        page = self.overview()
        self.assertIn("data-passkey-hint", page)
        self.assertIn("hidden>", page.split("data-passkey-hint", 1)[1][:200])   # JS shows it on capable devices

    def test_inline_setup_without_second_factor(self):
        self.enable()
        page = self.overview()
        self.assertIn("data-passkey-register-box", page)
        self.assertIn("data-passkey-register>", page)

    def test_gone_once_a_passkey_exists(self):
        self.enable()
        self.add_key(passkey=False)                 # a plain security key is not a passkey
        page = self.overview()
        self.assertIn("data-passkey-hint", page)
        self.assertNotIn("data-passkey-register-box", page)    # 2FA set up, not confirmed here → link
        self.assertIn('/profil/sicherheit#passkey"', page)
        self.add_key(passkey=True)
        self.assertNotIn("data-passkey-hint", self.overview())

    def test_dont_show_again(self):
        self.enable()
        r = self.c.post("/passkey-hinweis/aus")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(db.session.get(User, self.user.id).passkey_hint_off)
        self.assertNotIn("data-passkey-hint", self.overview())

    def test_dont_show_again_needs_sign_in(self):
        self.c.post("/logout")
        self.c.post("/passkey-hinweis/aus")
        self.assertFalse(db.session.get(User, self.user.id).passkey_hint_off)

    def test_ad_account_only_with_service_account(self):
        self.enable()
        self.user.auth_source = "ldap"
        db.session.commit()
        set_setting("ldap_enabled", "1")
        db.session.commit()
        self.assertNotIn("data-passkey-hint", self.overview())      # passkey sign-in would be refused
        set_setting("ldap_bind_dn", "CN=svc,DC=firma,DC=local")
        db.session.commit()
        self.assertIn("data-passkey-hint", self.overview())

    def test_setting_at_top_of_security_tab(self):
        self.login("admin")
        page = self.c.get("/admin/einstellungen?bereich=sicherheit").get_data(as_text=True)
        self.assertLess(page.index('id="passkeys"'), page.index('id="abmeldung"'))


if __name__ == "__main__":
    unittest.main()
