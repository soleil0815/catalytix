"""Zwei-Faktor je nach Netz (intern/extern) und Entsperren durch Admins."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app, lockout, netzone, twofa  # noqa: E402
from catalytix.models import User, db, get_setting, set_setting  # noqa: E402

LAN = "192.168.1.20"
INTERNET = "203.0.113.9"
PROXY = "172.18.0.5"     # Reverse-Proxy im Docker-Netz


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin = self.make("admin", "admin")
        self.anna = self.make("anna", "librarian", totp=True)
        self.ben = self.make("ben", "librarian")
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, totp=False):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password("geheim12345")
        if totp:
            u.totp_secret = twofa.encrypt(twofa.new_totp_secret())
            u.totp_enabled = True
        db.session.add(u)
        db.session.commit()
        return u

    def configure(self, policy="librarians", internal="skip", networks=""):
        set_setting("mfa_policy", policy)
        set_setting("mfa_internal", internal)
        set_setting("internal_networks", networks)
        db.session.commit()

    def env(self, remote, xff=None, proto=None):
        e = {"environ_base": {"REMOTE_ADDR": remote}, "headers": {}}
        if xff:
            e["headers"]["X-Forwarded-For"] = xff
        if proto:
            e["headers"]["X-Forwarded-Proto"] = proto
        return e

    def login(self, name, remote=LAN, remember=False, **kw):
        e = self.env(remote, **kw)
        data = {"username": name, "password": "geheim12345"}
        if remember:
            data["remember"] = "1"
        return self.c.post("/login", data=data, environ_base=e["environ_base"], headers=e["headers"])

    def get(self, url, remote=LAN, **kw):
        e = self.env(remote, **kw)
        return self.c.get(url, environ_base=e["environ_base"], headers=e["headers"])


class ZoneTests(Base):
    def setUp(self):
        super().setUp()

        @self.app.route("/_zone")
        def _zone():
            from flask import jsonify
            return jsonify(internal=netzone.is_internal(), ip=netzone.client_ip())

    def zone(self, remote, **kw):
        e = self.env(remote, **kw)
        return self.c.get("/_zone", environ_base=e["environ_base"], headers=e["headers"]).get_json()

    def test_direct_access(self):
        self.assertTrue(self.zone(LAN)["internal"])
        self.assertFalse(self.zone(INTERNET)["internal"])
        self.assertTrue(self.zone("127.0.0.1")["internal"])

    def test_via_proxy_uses_real_visitor(self):
        self.assertTrue(self.zone(PROXY, xff=LAN, proto="https")["internal"])
        z = self.zone(PROXY, xff=INTERNET, proto="https")
        self.assertFalse(z["internal"])
        self.assertEqual(z["ip"], INTERNET)

    def test_proxy_without_visitor_address_counts_as_external(self):
        self.assertFalse(self.zone(PROXY, proto="https")["internal"])

    def test_spoofed_header_from_internet_is_ignored(self):
        self.assertFalse(self.zone(INTERNET, xff=LAN)["internal"])

    def test_spoofed_header_through_proxy_is_ignored(self):
        # Angreifer schickt "X-Forwarded-For: 192.168.1.20", der Proxy hängt die echte Adresse an
        self.assertFalse(self.zone(PROXY, xff=f"{LAN}, {INTERNET}", proto="https")["internal"])

    def test_custom_networks(self):
        set_setting("internal_networks", "192.168.10.0/24")
        db.session.commit()
        self.assertFalse(self.zone(LAN)["internal"])
        self.assertTrue(self.zone("192.168.10.7")["internal"])


class OwnConnectionTests(ZoneTests):
    """Interne Rechner über die öffentliche Domain: der Router gibt sie als eigene öffentliche IP aus."""
    OWN = "80.151.220.89"

    def setUp(self):
        super().setUp()
        netzone._cache.clear()

    def addrinfo(self, *ips):
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]

    def test_fixed_ip(self):
        set_setting("own_public_hosts", self.OWN)
        db.session.commit()
        self.assertTrue(self.zone(self.OWN)["internal"])
        self.assertTrue(self.zone(PROXY, xff=self.OWN, proto="https")["internal"])
        self.assertFalse(self.zone(INTERNET)["internal"])

    def test_dyndns_name_is_resolved_and_follows_changes(self):
        import time
        from unittest import mock
        set_setting("own_public_hosts", "bibliothek.dyndns.example")
        db.session.commit()
        with mock.patch("socket.getaddrinfo", return_value=self.addrinfo(self.OWN)) as gai:
            self.assertTrue(self.zone(PROXY, xff=self.OWN, proto="https")["internal"])
            self.assertTrue(self.zone(PROXY, xff=self.OWN, proto="https")["internal"])
            self.assertEqual(gai.call_count, 1)          # zwischengespeichert
        new_ip = "80.151.7.7"
        with mock.patch("socket.getaddrinfo", return_value=self.addrinfo(new_ip)), \
                mock.patch("time.time", return_value=time.time() + netzone.RESOLVE_TTL + 1):
            self.assertTrue(self.zone(PROXY, xff=new_ip, proto="https")["internal"])
            self.assertFalse(self.zone(PROXY, xff=self.OWN, proto="https")["internal"])   # alte IP gilt nicht mehr

    def test_dns_failure_keeps_last_address_for_a_while(self):
        import time
        from unittest import mock
        set_setting("own_public_hosts", "bibliothek.dyndns.example")
        db.session.commit()
        with mock.patch("socket.getaddrinfo", return_value=self.addrinfo(self.OWN)):
            self.assertTrue(self.zone(self.OWN)["internal"])
        t = time.time()
        with mock.patch("socket.getaddrinfo", side_effect=OSError("kein DNS")), \
                mock.patch("time.time", return_value=t + 600):
            self.assertTrue(self.zone(self.OWN)["internal"])
        netzone._cache.clear()
        with mock.patch("socket.getaddrinfo", side_effect=OSError("kein DNS")):
            self.assertFalse(self.zone(self.OWN)["internal"])     # nie aufgelöst -> extern

    def test_login_from_own_connection_skips_second_factor(self):
        self.configure(policy="all", internal="skip", networks="192.168.2.0/24")
        set_setting("own_public_hosts", self.OWN)
        db.session.commit()
        self.assertNotIn("/login/2fa", self.login("anna", PROXY, xff=self.OWN, proto="https").headers["Location"])
        self.c.post("/logout")
        self.assertIn("/login/2fa", self.login("anna", PROXY, xff=INTERNET, proto="https").headers["Location"])

    def test_settings_validation_and_display(self):
        from unittest import mock
        self.configure(policy="optional", internal="same")
        self.login("admin", LAN)
        base = {"loan_days": "28", "mfa_policy": "all", "mfa_internal": "skip"}
        with mock.patch("socket.getaddrinfo", return_value=self.addrinfo(self.OWN)):
            self.c.post("/admin/einstellungen", data={**base, "own_public_hosts": "Bibliothek.DynDNS.example\n80.151.220.89"},
                        environ_base={"REMOTE_ADDR": LAN})
            self.assertEqual(get_setting("own_public_hosts"), "bibliothek.dyndns.example\n80.151.220.89")
            html = self.get("/admin/einstellungen", LAN).get_data(as_text=True)
        self.assertIn("derzeit: <code>80.151.220.89</code>", html)
        self.assertIn("Gäste-WLAN", html)
        r = self.c.post("/admin/einstellungen", data={**base, "own_public_hosts": "http://kaputt/"},
                        environ_base={"REMOTE_ADDR": LAN}, follow_redirects=True)
        self.assertIn("ungültige Einträge", r.get_data(as_text=True))
        self.assertEqual(get_setting("own_public_hosts"), "bibliothek.dyndns.example\n80.151.220.89")


class InternalSkipTests(Base):
    def test_internal_login_without_second_factor(self):
        self.configure(internal="skip")
        r = self.login("anna", LAN)
        self.assertNotIn("/login/2fa", r.headers["Location"])
        self.assertEqual(self.get("/", LAN).status_code, 200)

    def test_external_login_asks_second_factor(self):
        self.configure(internal="skip")
        r = self.login("anna", INTERNET)
        self.assertIn("/login/2fa", r.headers["Location"])
        r = self.login("anna", PROXY, xff=INTERNET, proto="https")
        self.assertIn("/login/2fa", r.headers["Location"])

    def test_internal_session_does_not_work_from_outside(self):
        self.configure(internal="skip")
        self.login("anna", LAN)
        r = self.get("/buecher", INTERNET)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertIn("/login", self.get("/", LAN).headers.get("Location", ""))   # wirklich abgemeldet

    def test_no_remember_cookie_when_second_factor_skipped(self):
        self.configure(internal="skip")
        r = self.login("anna", LAN, remember=True)
        self.assertNotIn("remember_token", " ".join(r.headers.getlist("Set-Cookie")))
        r = self.login("ben", LAN, remember=True)  # Pflicht laut Richtlinie, aber noch keine 2FA
        self.assertNotIn("remember_token", " ".join(r.headers.getlist("Set-Cookie")))

    def test_required_setup_only_from_outside(self):
        self.configure(internal="skip")
        r = self.login("ben", LAN)
        self.assertNotIn("sicherheit", r.headers["Location"])
        self.assertEqual(self.get("/buecher", LAN).status_code, 200)
        self.c.post("/logout")
        r = self.login("ben", INTERNET)
        self.assertIn("/profil/sicherheit", r.headers["Location"])
        self.assertIn("/profil/sicherheit", self.get("/buecher", INTERNET).headers["Location"])

    def test_users_without_2fa_obligation_keep_session_outside(self):
        self.configure(policy="admins", internal="skip")
        u = self.make("leser", "reader")
        self.login("leser", LAN)
        self.assertEqual(self.get("/buecher", INTERNET).status_code, 200)
        self.assertIsNotNone(u)


class InternalOptionalAndSameTests(Base):
    def test_optional_still_asks_if_set_up(self):
        self.configure(internal="optional")
        self.assertIn("/login/2fa", self.login("anna", LAN).headers["Location"])
        r = self.login("ben", LAN)     # Pflicht, aber intern nicht erzwungen
        self.assertNotIn("sicherheit", r.headers["Location"])

    def test_same_as_outside(self):
        self.configure(internal="same")
        self.assertIn("/login/2fa", self.login("anna", LAN).headers["Location"])
        self.c.post("/logout")
        self.assertIn("/profil/sicherheit", self.login("ben", LAN).headers["Location"])

    def test_default_is_same(self):
        self.assertEqual(get_setting("mfa_internal"), "same")


class SettingsPageTests(Base):
    def test_save_and_validate(self):
        self.configure(policy="optional", internal="same")
        self.login("admin", LAN)
        html = self.get("/admin/einstellungen", LAN).get_data(as_text=True)
        self.assertIn("gilt als <b>intern</b>", html)
        base = {"loan_days": "28", "mfa_policy": "librarians", "mfa_internal": "skip"}
        self.c.post("/admin/einstellungen", data={**base, "internal_networks": "192.168.1.0/24\n10.8.0.0/16"},
                    environ_base={"REMOTE_ADDR": LAN})
        self.assertEqual(get_setting("mfa_internal"), "skip")
        self.assertEqual(get_setting("internal_networks"), "192.168.1.0/24\n10.8.0.0/16")
        r = self.c.post("/admin/einstellungen", data={**base, "internal_networks": "192.168.1.0/24\nquatsch"},
                        environ_base={"REMOTE_ADDR": LAN}, follow_redirects=True)
        self.assertIn("ungültige Einträge", r.get_data(as_text=True))
        self.assertEqual(get_setting("internal_networks"), "192.168.1.0/24\n10.8.0.0/16")


class UnlockTests(Base):
    def lock(self, name):
        for _ in range(lockout.MAX_FAILS):
            self.c.post("/login", data={"username": name, "password": "falsch"}, environ_base={"REMOTE_ADDR": INTERNET})

    def test_lockout_is_shared_and_admin_can_unlock(self):
        self.lock("ben")
        self.assertIn("ben", lockout.locked_usernames())
        r = self.login("ben", INTERNET)
        self.assertIn("Zu viele Fehlversuche", r.get_data(as_text=True))
        self.login("admin", LAN)
        lst = self.get("/admin/benutzer", LAN).get_data(as_text=True)
        self.assertIn("Anmeldesperre", lst)
        self.assertIn("ben", self.get("/admin/benutzer?status=anmeldesperre", LAN).get_data(as_text=True))
        form = self.get(f"/admin/benutzer/{self.ben.id}", LAN).get_data(as_text=True)
        self.assertIn("Sperre aufheben", form)
        self.c.post(f"/admin/benutzer/{self.ben.id}/entsperren", environ_base={"REMOTE_ADDR": LAN})
        self.assertNotIn("ben", lockout.locked_usernames())
        self.c.post("/logout")
        r = self.login("ben", INTERNET)
        self.assertEqual(r.status_code, 302)

    def test_reset_2fa_in_ui_also_unlocks(self):
        self.configure(policy="optional", internal="same")
        self.login("admin", LAN)
        html = self.get(f"/admin/benutzer/{self.anna.id}", LAN).get_data(as_text=True)
        self.assertIn("Anmeldung und Sicherheit", html)
        self.c.post(f"/admin/benutzer/{self.anna.id}/2fa-zuruecksetzen", environ_base={"REMOTE_ADDR": LAN})
        db.session.refresh(self.anna)
        self.assertFalse(self.anna.has_2fa)
        self.c.post("/logout")
        self.assertNotIn("/login/2fa", self.login("anna", INTERNET).headers["Location"])

    def test_only_admins_can_unlock(self):
        self.lock("ben")
        self.configure(policy="optional", internal="same")
        u = self.make("leser", "reader")
        self.login("leser", LAN)
        self.c.post(f"/admin/benutzer/{self.ben.id}/entsperren", environ_base={"REMOTE_ADDR": LAN})
        self.assertIn("ben", lockout.locked_usernames())
        self.assertIsNotNone(u)


if __name__ == "__main__":
    unittest.main()
