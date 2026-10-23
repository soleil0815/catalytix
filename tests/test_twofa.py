"""Tests der Zwei-Faktor-Anmeldung (TOTP, Wiederherstellungscodes, Richtlinie, WebAuthn-Grundlagen)."""
import base64
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-2fa-"))

from catalytix import create_app, twofa  # noqa: E402
from catalytix.models import User, db, set_setting  # noqa: E402

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


class TotpAlgorithmTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        # RFC 6238, Anhang B (SHA-1) – letzte 6 Ziffern
        for t, expected in [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")]:
            self.assertEqual(twofa.totp_now(RFC_SECRET, at=t), expected)

    def test_window_and_replay(self):
        now = 1_700_000_000
        code = twofa.totp_now(RFC_SECRET, at=now - 30)
        step = twofa.check_totp(RFC_SECRET, code, 0, at=now)        # ein Schritt Toleranz
        self.assertIsNotNone(step)
        self.assertIsNone(twofa.check_totp(RFC_SECRET, code, step, at=now))   # keine Wiederverwendung
        self.assertIsNone(twofa.check_totp(RFC_SECRET, twofa.totp_now(RFC_SECRET, at=now - 120), 0, at=now))
        self.assertIsNone(twofa.check_totp(RFC_SECRET, "12345", 0, at=now))
        self.assertIsNotNone(twofa.check_totp(RFC_SECRET, "287 082", 0, at=59))  # Leerzeichen erlaubt


class TwoFaFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.admin = User(username="admin", role="admin", display_name="Admin")
        self.admin.set_password("geheim123")
        self.reader = User(username="lena", role="reader")
        self.reader.set_password("geheim123")
        db.session.add_all([self.admin, self.reader])
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name="admin", pw="geheim123"):
        return self.c.post("/login", data={"username": name, "password": pw})

    def enable_totp(self):
        self.login()
        html = self.c.post("/profil/sicherheit/app/einrichten").get_data(as_text=True)
        self.assertIn("<svg", html)                                # QR-Code
        secret = re.search(r"secret=([A-Z2-7]+)", html).group(1)
        # Code aus dem vorherigen Zeitschritt, damit der aktuelle beim Login frei bleibt
        r = self.c.post("/profil/sicherheit/app/bestaetigen", data={"code": twofa.totp_now(secret, time.time() - 30)})
        html = r.get_data(as_text=True)
        codes = re.findall(r">([a-z2-9]{4}-[a-z2-9]{4})<", html)
        self.assertEqual(len(codes), 10)
        self.c.post("/logout")
        return secret, codes

    def test_enable_and_login_with_totp(self):
        secret, _ = self.enable_totp()
        db.session.refresh(self.admin)
        self.assertTrue(self.admin.totp_enabled)
        self.assertNotIn(secret, self.admin.totp_secret)           # verschlüsselt gespeichert
        r = self.login()
        self.assertIn("/login/2fa", r.headers["Location"])
        self.assertIn("/login", self.c.get("/").headers["Location"])  # noch nicht angemeldet
        r = self.c.post("/login/2fa", data={"code": "000000"})
        self.assertIn("ungültig", r.get_data(as_text=True))
        code = twofa.totp_now(secret)
        r = self.c.post("/login/2fa", data={"code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)
        # gleicher Code ein zweites Mal: abgelehnt (genau dieser Code – nicht neu erzeugt, sonst wackelt der
        # Test an der 30-Sekunden-Grenze)
        self.c.post("/logout")
        self.login()
        r = self.c.post("/login/2fa", data={"code": code})
        self.assertIn("ungültig", r.get_data(as_text=True))

    def test_recovery_code_once(self):
        _, codes = self.enable_totp()
        self.login()
        r = self.c.post("/login/2fa", data={"modus": "wiederherstellung", "code": codes[0].upper()})
        self.assertEqual(r.status_code, 302)
        self.c.post("/logout")
        self.login()
        r = self.c.post("/login/2fa", data={"modus": "wiederherstellung", "code": codes[0]})
        self.assertIn("ungültig", r.get_data(as_text=True))
        db.session.refresh(self.admin)
        self.assertEqual(twofa.recovery_codes_left(self.admin), 9)

    def test_pending_expires_and_throttle(self):
        secret, _ = self.enable_totp()
        self.login()
        with self.c.session_transaction() as s:
            pending = dict(s["mfa_pending"])
            pending["ts"] = time.time() - 600
            s["mfa_pending"] = pending
        r = self.c.get("/login/2fa")
        self.assertIn("/login", r.headers["Location"])
        self.login()
        for _ in range(8):
            self.c.post("/login/2fa", data={"code": "111111"})
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)})
        self.assertEqual(r.status_code, 429)

    def test_policy_forces_setup(self):
        set_setting("mfa_policy", "admins")
        db.session.commit()
        r = self.login()
        self.assertIn("/profil/sicherheit", r.headers["Location"])
        self.assertIn("/profil/sicherheit", self.c.get("/buecher").headers["Location"])
        self.assertEqual(self.c.post("/logout").status_code, 302)
        # Leser ist nicht betroffen
        r = self.login("lena")
        self.assertEqual(r.headers["Location"], "/")

    def test_cannot_remove_last_required_factor(self):
        secret, _ = self.enable_totp()
        set_setting("mfa_policy", "all")
        db.session.commit()
        self.login()
        self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)})
        r = self.c.post("/profil/sicherheit/app/entfernen", data={"code": twofa.totp_now(secret, time.time() + 30)},
                        follow_redirects=True)
        self.assertIn("ist Pflicht", r.get_data(as_text=True))
        db.session.refresh(self.admin)
        self.assertTrue(self.admin.totp_enabled)

    def test_remove_totp_needs_code(self):
        secret, _ = self.enable_totp()
        self.login()
        self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)})
        self.c.post("/profil/sicherheit/app/entfernen", data={"code": "000000"})
        db.session.refresh(self.admin)
        self.assertTrue(self.admin.totp_enabled)
        self.c.post("/profil/sicherheit/app/entfernen", data={"code": twofa.totp_now(secret, time.time() + 30)})
        db.session.refresh(self.admin)
        self.assertFalse(self.admin.totp_enabled)

    def test_admin_reset_and_cli(self):
        self.enable_totp()
        self.login("admin")
        # zweiter Admin setzt zurück
        other = User(username="admin2", role="admin")
        other.set_password("geheim123")
        db.session.add(other)
        db.session.commit()
        c2 = self.app.test_client()
        c2.post("/login", data={"username": "admin2", "password": "geheim123"})
        c2.post(f"/admin/benutzer/{self.admin.id}/2fa-zuruecksetzen")
        db.session.refresh(self.admin)
        self.assertFalse(self.admin.has_2fa)
        runner = self.app.test_cli_runner()
        res = runner.invoke(args=["reset-2fa", "admin"])
        self.assertIn("reset for", res.output)

    def test_security_page_renders(self):
        self.login("lena")
        html = self.c.get("/profil/sicherheit").get_data(as_text=True)
        self.assertIn("Authenticator-App", html)
        self.assertIn("Sicherheitsschlüssel", html)

    def test_webauthn_begin_returns_options(self):
        self.login("lena")
        r = self.c.post("/profil/sicherheit/schluessel/start", json={}, base_url="https://bib.example.de")
        opts = r.json["options"]["publicKey"]
        self.assertEqual(opts["rp"]["id"], "bib.example.de")
        self.assertTrue(opts["challenge"])
        self.assertEqual(opts["user"]["name"], "lena")


if __name__ == "__main__":
    unittest.main()


class UnreadableTotpTests(unittest.TestCase):
    """Authenticator-Geheimnis mit altem Schlüssel: klarer Hinweis statt „Code ungültig“, Neueinrichtung möglich."""

    def setUp(self):
        from catalytix import create_app
        from catalytix.models import User, db
        self.db = db
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username="anna", display_name="Anna", role="reader", auth_source="local")
        u.set_password("geheim12345")
        u.totp_enabled = True
        u.totp_secret = "gAAAAAB-nicht-lesbar"
        admin = User(username="admin", display_name="Admin", role="admin", auth_source="local")
        admin.set_password("geheim12345")
        db.session.add_all([u, admin])
        db.session.commit()
        self.user = u
        self.c = self.app.test_client()

    def tearDown(self):
        self.db.session.remove()
        self.ctx.pop()

    def test_login_shows_hint(self):
        from catalytix import twofa
        self.assertTrue(twofa.totp_unreadable(self.user))
        r = self.c.post("/login", data={"username": "anna", "password": "geheim12345"}, follow_redirects=True)
        self.assertIn("muss neu eingerichtet werden", r.get_data(as_text=True))
