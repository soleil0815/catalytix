"""3.4.2: the second sign-in step names the passkey. Someone with a passkey who signed in with the password (on the
iPhone the saved password is easily picked instead of the passkey) sees "Mit Passkey bestätigen", not only
"Mit Sicherheitsschlüssel bestätigen" with "Schlüssel einstecken"."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix.models import WebAuthnCredential, db  # noqa: E402


class ChallengeLabelTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("leser")
        with self.c.session_transaction() as s:            # password checked, second step pending
            s["mfa_pending"] = {"uid": self.user.id, "ts": time.time()}

    def add(self, passkey, n):
        db.session.add(WebAuthnCredential(user_id=self.user.id, credential_id=f"id-{n}", credential_data="x",
                                          rp_id="localhost", passkey=passkey, name=f"Gerät {n}"))
        db.session.commit()

    def page(self):
        r = self.c.get("/login/2fa")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_passkey_only(self):
        self.add(True, 1)
        page = self.page()
        self.assertIn("Mit Passkey bestätigen", page)
        self.assertIn("Face ID, Fingerabdruck oder Geräte-PIN", page)
        self.assertNotIn("Schlüssel einstecken", page)
        self.assertIn("data-webauthn-login", page)

    def test_security_key_only_unchanged(self):
        self.add(False, 1)
        page = self.page()
        self.assertIn("Mit Sicherheitsschlüssel bestätigen", page)
        self.assertIn("Schlüssel einstecken und berühren, wenn er blinkt.", page)
        self.assertNotIn("Mit Passkey", page)

    def test_both(self):
        self.add(True, 1)
        self.add(False, 2)
        self.assertIn("Mit Passkey oder Sicherheitsschlüssel bestätigen", self.page())

    def test_other_address_not_offered(self):
        db.session.add(WebAuthnCredential(user_id=self.user.id, credential_id="id-x", credential_data="x",
                                          rp_id="bibliothek.firma.local", passkey=True))
        db.session.commit()
        page = self.page()
        self.assertNotIn("data-webauthn-login", page)
        self.assertNotIn("Mit Passkey", page)

    def test_english(self):
        self.add(True, 1)
        with self.c.session_transaction() as s:
            s["lang"] = "en"
        page = self.page()
        self.assertIn("Confirm with passkey", page)
        self.assertIn("Waiting for Face ID", page)


if __name__ == "__main__":
    unittest.main()
