"""3.3.0: sign-in with a passkey (no user name, no password, no second step) – off by default; AD accounts are
checked live in AD through the service account on every passkey sign-in."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import lockout, twofa  # noqa: E402
from catalytix.ldap_auth import Directory, LDAPError, LDAPUser  # noqa: E402
from catalytix.models import User, WebAuthnCredential, db, get_setting, set_setting  # noqa: E402


class PasskeyBase(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")

    def enable(self, on=True):
        set_setting("passkey_login", "1" if on else "0")
        db.session.commit()

    def passkey_sign_in(self, user, **extra):
        """Start + finish with the WebAuthn check mocked: returns the response of /login/passkey/pruefen."""
        with mock.patch.object(twofa, "passkey_auth_begin", return_value=({"publicKey": {}}, {"s": 1})):
            r = self.c.post("/login/passkey/start", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with mock.patch.object(twofa, "passkey_auth_complete", return_value=user):
            return self.c.post("/login/passkey/pruefen", json={"credential": {"id": "x"}, **extra})

    def signed_in(self):
        return self.c.get("/profil/sicherheit").status_code == 200


class SettingTests(PasskeyBase):
    def test_off_by_default_404(self):
        self.assertEqual(get_setting("passkey_login"), "0")
        self.assertEqual(self.c.post("/login/passkey/start", json={}).status_code, 404)
        self.assertEqual(self.c.post("/login/passkey/pruefen", json={}).status_code, 404)

    def test_login_page_only_when_enabled(self):
        page = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("data-passkey-login", page)
        self.assertNotIn("webauthn", page)
        self.enable()
        page = self.c.get("/login").get_data(as_text=True)
        self.assertIn("data-passkey-login", page)
        self.assertIn('autocomplete="username webauthn"', page)

    def test_admin_saves_in_security_tab(self):
        self.login("admin")
        page = self.c.get("/admin/einstellungen?bereich=sicherheit").get_data(as_text=True)
        self.assertIn('name="passkey_login"', page)
        form = {"bereich": "sicherheit", "passkey_login": "1"}
        self.c.post("/admin/einstellungen", data=form)
        self.assertEqual(get_setting("passkey_login"), "1")
        self.c.post("/admin/einstellungen", data={"bereich": "sicherheit"})
        self.assertEqual(get_setting("passkey_login"), "0")


class LocalSignInTests(PasskeyBase):
    def setUp(self):
        super().setUp()
        self.enable()

    def test_local_account_signs_in_without_second_step(self):
        r = self.passkey_sign_in(self.admin, next="/buecher")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["redirect"], "/buecher")
        with self.c.session_transaction() as s:
            self.assertTrue(s.get("mfa_ok"))
            self.assertNotIn("mfa_pending", s)
        self.assertTrue(self.signed_in())

    def test_foreign_next_is_ignored(self):
        r = self.passkey_sign_in(self.admin, next="https://example.org/")
        self.assertNotIn("example.org", r.get_json()["redirect"])

    def test_unknown_passkey_refused(self):
        r = self.passkey_sign_in(None)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertFalse(self.signed_in())

    def test_verification_error_refused_without_details(self):
        with mock.patch.object(twofa, "passkey_auth_begin", return_value=({}, {"s": 1})):
            self.c.post("/login/passkey/start", json={})
        with mock.patch.object(twofa, "passkey_auth_complete", side_effect=ValueError("secret detail")):
            r = self.c.post("/login/passkey/pruefen", json={"credential": {}})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("secret detail", r.get_data(as_text=True))

    def test_inactive_account_refused(self):
        self.admin.active = False
        db.session.commit()
        r = self.passkey_sign_in(self.admin)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.signed_in())

    def test_finish_without_start_refused(self):
        with mock.patch.object(twofa, "passkey_auth_complete", return_value=self.admin):
            r = self.c.post("/login/passkey/pruefen", json={"credential": {}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.signed_in())

    def test_state_used_only_once(self):
        self.passkey_sign_in(self.admin)
        self.c.post("/logout")
        with mock.patch.object(twofa, "passkey_auth_complete", return_value=self.admin):
            r = self.c.post("/login/passkey/pruefen", json={"credential": {}})
        self.assertEqual(r.status_code, 400)

    def test_throttled_after_many_failures(self):
        codes = []
        for _ in range(lockout.MAX_FAILS + 2):
            with mock.patch.object(twofa, "passkey_auth_begin", return_value=({}, {"s": 1})):
                start = self.c.post("/login/passkey/start", json={})
            codes.append(start.status_code)
            if start.status_code != 200:
                break
            with mock.patch.object(twofa, "passkey_auth_complete", return_value=None):
                self.c.post("/login/passkey/pruefen", json={"credential": {}})
        self.assertIn(429, codes)


class ADSignInTests(PasskeyBase):
    def setUp(self):
        super().setUp()
        self.enable()
        for k, v in {"ldap_enabled": "1", "ldap_server": "dc1.firma.local", "ldap_base_dn": "DC=firma,DC=local",
                     "ldap_bind_dn": "CN=svc,DC=firma,DC=local"}.items():
            set_setting(k, v)
        self.ad = User(username="mmuster", display_name="Max", role="reader", auth_source="ldap")
        db.session.add(self.ad)
        db.session.commit()

    def info(self, role="reader", guid=""):
        return LDAPUser(username="mmuster", dn="CN=Max,DC=firma,DC=local", display_name="Max Muster", role=role,
                        guid=guid)

    def test_allowed_in_ad(self):
        with mock.patch.object(Directory, "check_for_passkey", return_value=self.info()) as check:
            r = self.passkey_sign_in(self.ad)
        check.assert_called_once_with("mmuster")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with self.c.session_transaction() as s:
            self.assertIn("ldap_ok_at", s)
        self.assertEqual(db.session.get(User, self.ad.id).display_name, "Max Muster")   # synced from AD

    def test_disabled_or_locked_in_ad_refused(self):
        with mock.patch.object(Directory, "check_for_passkey", return_value=None):
            r = self.passkey_sign_in(self.ad)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.signed_in())

    def test_no_longer_entitled_refused(self):
        with mock.patch.object(Directory, "check_for_passkey", return_value=self.info(role=None)):
            r = self.passkey_sign_in(self.ad)
        self.assertEqual(r.status_code, 400)

    def test_other_person_with_same_name_refused(self):
        self.ad.ldap_guid = "aaaa"
        db.session.commit()
        with mock.patch.object(Directory, "check_for_passkey", return_value=self.info(guid="bbbb")):
            r = self.passkey_sign_in(self.ad)
        self.assertEqual(r.status_code, 400)

    def test_ad_unreachable_refused_with_hint(self):
        with mock.patch.object(Directory, "check_for_passkey", side_effect=LDAPError("timeout")):
            r = self.passkey_sign_in(self.ad)
        self.assertEqual(r.status_code, 503)
        self.assertIn("Passwort", r.get_json()["error"])
        self.assertFalse(self.signed_in())

    def test_without_service_account_refused(self):
        set_setting("ldap_bind_dn", "")
        db.session.commit()
        with mock.patch.object(Directory, "check_for_passkey") as check:
            r = self.passkey_sign_in(self.ad)
        check.assert_not_called()
        self.assertEqual(r.status_code, 400)

    def test_local_accounts_do_not_ask_ad(self):
        with mock.patch.object(Directory, "check_for_passkey") as check:
            r = self.passkey_sign_in(self.admin)
        check.assert_not_called()
        self.assertEqual(r.status_code, 200)


class LockedOutParsingTests(unittest.TestCase):
    def conn(self, ok=True, value=None, entries=True):
        c = mock.Mock()
        c.search.return_value = ok
        attrs = {} if value is None else {"msDS-User-Account-Control-Computed": value}
        c.response = [{"type": "searchResEntry", "attributes": attrs}] if entries else []
        return c

    def test_bits(self):
        self.assertTrue(Directory._locked_out(self.conn(value=16), "CN=x"))
        self.assertTrue(Directory._locked_out(self.conn(value=[0x10 | 0x800000]), "CN=x"))
        self.assertFalse(Directory._locked_out(self.conn(value=0x800000), "CN=x"))   # only password expired
        self.assertFalse(Directory._locked_out(self.conn(value=None), "CN=x"))
        self.assertFalse(Directory._locked_out(self.conn(), ""))

    def test_unreadable_raises(self):
        with self.assertRaises(LDAPError):
            Directory._locked_out(self.conn(ok=False), "CN=x")
        with self.assertRaises(LDAPError):
            Directory._locked_out(self.conn(entries=False), "CN=x")


class RegistrationTests(PasskeyBase):
    def setUp(self):
        super().setUp()
        self.login("admin")

    def start(self):
        with mock.patch.object(twofa, "webauthn_register_begin", return_value=({}, {"s": 1})) as begin:
            self.c.post("/profil/sicherheit/schluessel/start", json={"passkey": True})
        return begin.call_args.kwargs.get("passkey")

    def test_passkey_flag_only_when_enabled(self):
        self.assertFalse(self.start())
        self.assertNotIn("data-passkey-register", self.c.get("/profil/sicherheit").get_data(as_text=True))
        self.enable()
        self.assertTrue(self.start())
        self.assertIn("data-passkey-register", self.c.get("/profil/sicherheit").get_data(as_text=True))

    def test_flag_comes_from_start_not_from_browser(self):
        self.enable()
        self.start()
        cred = WebAuthnCredential(user_id=self.admin.id, credential_id="abc", credential_data="x", rp_id="localhost",
                                  name="Passkey", passkey=True)

        def fake_complete(user, rp_id, state, response, name, passkey=False):
            self.assertTrue(passkey)
            db.session.add(cred)
            db.session.commit()
            return cred

        with mock.patch.object(twofa, "webauthn_register_complete", side_effect=fake_complete):
            r = self.c.post("/profil/sicherheit/schluessel/speichern", json={"credential": {}, "name": "Laptop",
                                                                             "passkey": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
