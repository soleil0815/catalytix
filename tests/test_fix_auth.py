"""Fixed findings from the security review: login, two-factor, lockouts, network zones."""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

import catalytix  # noqa: E402
from catalytix import create_app, lockout, netzone, twofa  # noqa: E402
from catalytix.ldap_auth import LDAPError, LDAPUser  # noqa: E402
from catalytix.models import LoginAttempt, User, WebAuthnCredential, db, get_setting, set_setting  # noqa: E402

PW = "geheim12345"
LAN = {"REMOTE_ADDR": "192.168.1.77"}
EXT = {"REMOTE_ADDR": "203.0.113.50"}


class Base(unittest.TestCase):
    DB = "sqlite://"

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": self.DB})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        from flask import g

        def _fresh():
            g.pop("_login_user", None)
            g.pop("_catalytix_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        for name, role in (("admin", "admin"), ("kasse", "librarian"), ("leser", "reader")):
            u = User(username=name, display_name=name, role=role, auth_source="local")
            u.set_password(PW)
            db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def user(self, name):
        db.session.expire_all()
        return User.query.filter_by(username=name).one()

    def give_totp(self, name):
        u = self.user(name)
        secret = twofa.new_totp_secret()
        u.totp_secret = twofa.encrypt(secret)
        u.totp_enabled = True
        db.session.commit()
        return secret

    def settings(self, **kw):
        for k, v in kw.items():
            set_setting(k, v)
        db.session.commit()


# ---------------------------------------------------------------- 1: skipped 2FA
class SkippedSessionTests(Base):
    def login_skipped(self):
        self.settings(mfa_internal="skip")
        secret = self.give_totp("kasse")
        r = self.c.post("/login", data={"username": "kasse", "password": PW}, environ_base=LAN)
        self.assertEqual(r.headers["Location"], "/")       # logged in without a second factor
        return secret

    def test_skipped_session_cannot_manage_factors(self):
        secret = self.login_skipped()
        before = self.user("kasse").recovery_codes
        r = self.c.post("/profil/sicherheit/codes", environ_base=LAN)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.user("kasse").recovery_codes, before)
        r = self.c.post("/profil/sicherheit/app/einrichten", environ_base=LAN)
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("totp_setup", self._session())
        r = self.c.post("/profil/sicherheit/app/bestaetigen", data={"code": "123456"}, environ_base=LAN)
        self.assertEqual(twofa.decrypt(self.user("kasse").totp_secret), secret)
        r = self.c.post("/profil/sicherheit/schluessel/start", environ_base=LAN)
        self.assertEqual(r.status_code, 403)
        self.assertIn("zweiten Faktor", r.get_json()["error"])
        r = self.c.post("/profil/sicherheit/app/entfernen", data={"code": twofa.totp_now(secret)}, environ_base=LAN)
        self.assertTrue(self.user("kasse").totp_enabled)
        page = self.c.get("/profil/sicherheit", environ_base=LAN).get_data(as_text=True)
        self.assertIn("Zweiten Faktor bestätigen", page)

    def test_stepup_then_manage(self):
        secret = self.login_skipped()
        r = self.c.post("/profil/sicherheit/bestaetigen", environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=LAN)
        self.assertEqual(r.headers["Location"], "/profil/sicherheit")
        html = self.c.post("/profil/sicherheit/codes", environ_base=LAN).get_data(as_text=True)
        self.assertEqual(len(re.findall(r">([a-z2-9]{4}-[a-z2-9]{4})<", html)), 10)
        # after the second factor the session is also valid from outside
        self.assertEqual(self.c.get("/", environ_base=EXT).status_code, 200)

    def test_stepup_fail_cap_ends_session(self):
        secret = self.login_skipped()
        self.c.post("/profil/sicherheit/bestaetigen", environ_base=LAN)
        for i in range(10):
            self.c.post("/login/2fa", data={"code": "000000"}, environ_base={"REMOTE_ADDR": f"192.168.1.{i + 10}"})
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=LAN)
        self.assertEqual(r.headers["Location"], "/login")
        self.assertIn("/login", self.c.get("/", environ_base=LAN).headers["Location"])

    def test_full_2fa_login_may_manage(self):
        secret = self.give_totp("kasse")
        self.c.post("/login", data={"username": "kasse", "password": PW}, environ_base=EXT)
        self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        r = self.c.post("/profil/sicherheit/app/einrichten", environ_base=EXT)
        self.assertEqual(r.status_code, 200)
        self.assertIn("secret=", r.get_data(as_text=True))

    def test_first_time_setup_still_works_in_skipped_session(self):
        self.settings(mfa_internal="skip", mfa_policy="all")
        r = self.c.post("/login", data={"username": "leser", "password": PW}, environ_base=LAN)
        self.assertTrue(self._session().get("mfa_skipped"))
        html = self.c.post("/profil/sicherheit/app/einrichten", environ_base=LAN).get_data(as_text=True)
        sec = re.search(r"secret=([A-Z2-7]+)", html).group(1)
        r = self.c.post("/profil/sicherheit/app/bestaetigen", data={"code": twofa.totp_now(sec)}, environ_base=LAN)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.user("leser").totp_enabled)

    def test_logout_forgets_mfa_ok(self):
        secret = self.give_totp("kasse")
        self.c.post("/login", data={"username": "kasse", "password": PW}, environ_base=EXT)
        self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        self.assertTrue(self._session().get("mfa_ok"))
        self.c.post("/logout", environ_base=EXT)
        self.assertNotIn("mfa_ok", self._session())

    def _session(self):
        cookie = self.c.get_cookie("session")
        if cookie is None:
            return {}
        return self.app.session_interface.get_signing_serializer(self.app).loads(cookie.value)


# ---------------------------------------------------------------- 2: takeover of local accounts via AD
class LdapTakeoverTests(Base):
    def test_domain_prefix_does_not_take_over_local_account(self):
        self.settings(ldap_enabled="1")
        info = LDAPUser(username="kasse", dn="CN=kasse,OU=Users,DC=corp", role="reader")
        for name in ("CORP\\kasse", "kasse@corp.local"):
            with mock.patch("catalytix.views.main.authenticate", return_value=info), \
                    self.assertLogs("catalytix.views.main", "WARNING"):
                r = self.c.post("/login", data={"username": name, "password": "AD-Passwort"})
            self.assertEqual(r.status_code, 401)
            self.assertIn("Benutzername oder Passwort ist falsch", r.get_data(as_text=True))
        u = self.user("kasse")
        self.assertEqual(u.auth_source, "local")
        self.assertEqual(u.role, "librarian")
        self.assertTrue(u.check_password(PW))

    def test_new_ad_user_still_created(self):
        self.settings(ldap_enabled="1")
        info = LDAPUser(username="neu", dn="CN=neu,DC=corp", role="reader")
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "CORP\\neu", "password": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.user("neu").auth_source, "ldap")


# ---------------------------------------------------------------- 3: login lockouts
class LockoutTests(Base):
    def test_username_variants_share_lockout(self):
        self.settings(ldap_enabled="1")
        codes = []
        with mock.patch("catalytix.views.main.authenticate", side_effect=LDAPError("bad")):
            for i in range(12):
                v = ("anna", f"D{i}\\anna", f"ANNA@x{i}")[i % 3]
                codes.append(self.c.post("/login", data={"username": v, "password": "x"}, environ_base=EXT).status_code)
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(set(codes[8:]), {429})
        self.assertEqual({a.username for a in LoginAttempt.query.all()}, {"anna"})

    def _fails(self, n, name="leser", internal=False):
        for i in range(n):
            db.session.add(LoginAttempt(key=f"{name}|10.9.{i // 250}.{i % 250}", username=name,
                                        ip=f"10.9.{i // 250}.{i % 250}", internal=internal))
        db.session.commit()

    def test_user_throttle_also_internal(self):
        self._fails(lockout.USER_MAX_FAILS)
        self.assertEqual(self.c.post("/login", data={"username": "leser", "password": PW},
                                     environ_base=EXT).status_code, 429)
        r = self.c.post("/login", data={"username": "leser", "password": PW}, environ_base=LAN)
        self.assertEqual(r.status_code, 302)      # higher limit internally
        self.c.post("/logout")
        self._fails(lockout.USER_MAX_FAILS_INTERNAL, internal=True)
        self.assertEqual(self.app.test_client().post("/login", data={"username": "leser", "password": PW},
                                                     environ_base=LAN).status_code, 429)

    def test_failures_from_outside_never_lock_lan(self):
        self._fails(lockout.USER_MAX_FAILS_INTERNAL + 50)          # attacker from outside
        r = self.c.post("/login", data={"username": "leser", "password": PW}, environ_base=LAN)
        self.assertEqual(r.status_code, 302)

    def test_one_address_cannot_spray_all_accounts(self):
        for i in range(lockout.IP_MAX_FAILS):
            self.c.post("/login", data={"username": f"opfer{i}", "password": "falsch-falsch"}, environ_base=EXT)
        r = self.c.post("/login", data={"username": "leser", "password": PW}, environ_base=EXT)
        self.assertEqual(r.status_code, 429)
        r = self.app.test_client().post("/login", data={"username": "leser", "password": PW}, environ_base=LAN)
        self.assertEqual(r.status_code, 302)                       # other addresses not affected

    def test_2fa_daily_cap(self):
        secret = self.give_totp("admin")
        from catalytix import twofa
        for i in range(lockout.MFA_DAILY_MAX):
            db.session.add(LoginAttempt(key=f"2fa|admin|198.51.100.{i}", username="admin", ip=f"198.51.100.{i}"))
        db.session.commit()
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        self.assertEqual(r.status_code, 429)
        self.assertIn("admin", lockout.locked_usernames())

    def test_2fa_attempts_capped_per_login(self):
        secret = self.give_totp("admin")
        r = self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        self.assertIn("/login/2fa", r.headers["Location"])
        saved = self.c.get_cookie("session").value
        for i in range(10):   # changing addresses: the per-address lockout does not apply
            r = self.c.post("/login/2fa", data={"code": "000000"}, environ_base={"REMOTE_ADDR": f"198.51.100.{i}"})
            self.assertEqual(r.status_code, 200)
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base={"REMOTE_ADDR": "198.51.100.99"})
        self.assertEqual(r.headers["Location"], "/login")
        # replaying the old cookie does not help – counting happens in the database
        self.c.set_cookie("session", saved)
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base={"REMOTE_ADDR": "198.51.100.98"})
        self.assertEqual(r.headers["Location"], "/login")
        # continues with the password entered again
        r = self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        self.assertIn("/login/2fa", r.headers["Location"])
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        self.assertEqual(r.headers["Location"], "/")

    def test_2fa_checks_user_throttle(self):
        secret = self.give_totp("admin")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        self._fails(lockout.USER_MAX_FAILS, "admin")
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        self.assertEqual(r.status_code, 429)       # rejected …
        with self.c.session_transaction() as sess:
            self.assertIn("mfa_pending", sess)     # … but the pending login stays (no kick-out by third parties)


# ---------------------------------------------------------------- 4: network zones
class NetzoneDefaultTests(Base):
    def zone(self, addr):
        with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": addr}):
            return netzone.classify()

    def test_docker_ranges_not_internal_by_default(self):
        self.assertNotIn("172.16", netzone.DEFAULT_INTERNAL)
        self.assertEqual(self.zone("172.17.0.1"), "extern")
        self.assertEqual(self.zone("192.168.1.5"), "lan")
        self.assertEqual(self.zone("10.1.2.3"), "lan")
        self.assertEqual(self.zone("127.0.0.1"), "lan")
        self.settings(internal_networks="172.16.0.0/12")
        self.assertEqual(self.zone("172.17.0.1"), "lan")

    def test_settings_page_shows_new_default(self):
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)
        html = self.c.get("/admin/einstellungen?bereich=sicherheit", environ_base=LAN).get_data(as_text=True)
        self.assertIn("10.0.0.0/8\n192.168.0.0/16", html)
        self.assertNotIn("172.16.0.0/12", html)

    def test_proxy_without_forwarded_for_is_external(self):
        with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.168.1.2"},
                                           headers={"X-Forwarded-Proto": "https"}):
            self.assertEqual(netzone.classify(), "extern")


# ---------------------------------------------------------------- 5: race on one-time codes
class RaceTests(Base):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="catalytix-race-")
        self.DB = f"sqlite:///{d}/t.db"
        super().setUp()

    def _parallel(self, clients, posts):
        res = [None] * len(clients)
        bar = threading.Barrier(len(clients))

        def go(i):
            bar.wait()
            url, data = posts[i]
            res[i] = clients[i].post(url, data=data).headers.get("Location")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(len(clients))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return res

    def _pending_clients(self, n):
        clients = [self.app.test_client() for _ in range(n)]
        for c in clients:
            self.assertIn("/login/2fa", c.post("/login", data={"username": "admin", "password": PW}).headers["Location"])
        return clients

    def test_concurrent_recovery_codes_no_lost_update(self):
        self.give_totp("admin")
        codes = twofa.new_recovery_codes(self.user("admin"))
        url = "/login/2fa?modus=wiederherstellung"
        res = self._parallel(self._pending_clients(2), [(url, {"code": codes[0]}), (url, {"code": codes[1]})])
        self.assertEqual(res, ["/", "/"])
        self.assertEqual(twofa.recovery_codes_left(self.user("admin")), 8)
        u = self.user("admin")
        self.assertFalse(twofa.use_recovery_code(u, codes[0]))
        self.assertFalse(twofa.use_recovery_code(u, codes[1]))

    def test_same_recovery_code_only_once(self):
        self.give_totp("admin")
        codes = twofa.new_recovery_codes(self.user("admin"))
        url = "/login/2fa?modus=wiederherstellung"
        res = self._parallel(self._pending_clients(3), [(url, {"code": codes[0]})] * 3)
        self.assertEqual(res.count("/"), 1)
        self.assertEqual(twofa.recovery_codes_left(self.user("admin")), 9)

    def test_same_totp_code_only_once(self):
        secret = self.give_totp("admin")
        clients = self._pending_clients(3)
        code = twofa.totp_now(secret)
        # check slowed down: all three requests have checked the code before one consumes it
        def slow_check(secret_, c, last_step=0, **_k):
            time.sleep(0.05)
            return int(time.time() // twofa.TOTP_PERIOD) if c == code else None
        with mock.patch("catalytix.twofa.check_totp", side_effect=slow_check):
            res = self._parallel(clients, [("/login/2fa", {"code": code})] * 3)
        self.assertEqual(res.count("/"), 1)


# ---------------------------------------------------------------- 6 + 12: registration
class RegisterTests(Base):
    def test_english_registration_with_ad(self):
        self.settings(registration_mode="open", registration_verify_email="0", ldap_enabled="1",
                      ldap_server="dc1", ldap_base_dn="DC=corp", ldap_bind_dn="CN=svc")

        class Conn:
            response = []

            def search(self, *a, **k):
                return True

            def unbind(self):
                pass
        with mock.patch("catalytix.ldap_auth.Directory.service_connection", return_value=Conn()), \
                mock.patch("catalytix.views.register.MIN_FILL_SECONDS", 0):
            for lang, name in (("de", "neuling1"), ("en", "newbie1")):
                c = self.app.test_client()
                c.post(f"/sprache/{lang}")
                c.get("/registrieren")
                r = c.post("/registrieren", data={"username": name, "display_name": "N", "email": f"{name}@ex.org",
                                                  "password": "abcdefghijk1", "password2": "abcdefghijk1"})
                self.assertEqual(r.status_code, 302, lang)
                self.assertEqual(self.user(name).language, lang)   # Finding 12

    def test_ambiguous_ad_name_is_taken(self):
        from catalytix.views import register

        class Conn:
            response = [{"type": "searchResEntry"}, {"type": "searchResEntry"}]

            def search(self, *a, **k):
                return True

            def unbind(self):
                pass
        self.settings(ldap_enabled="1", ldap_server="dc1", ldap_base_dn="DC=corp", ldap_bind_dn="CN=svc")
        with mock.patch("catalytix.ldap_auth.Directory.service_connection", return_value=Conn()), \
                self.app.test_request_context("/"):
            self.assertTrue(register._exists_in_directory("doppelt"))


# ---------------------------------------------------------------- 7: logout loop
class RememberLoopTests(Base):
    def test_disabled_ad_user_remember_cookie_deleted(self):
        self.settings(ldap_enabled="1", ldap_bind_dn="CN=svc", ldap_server="dc", ldap_base_dn="DC=x")
        info = LDAPUser(username="adu", dn="CN=adu,DC=x", role="reader")
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "adu", "password": "pw", "remember": "1"})
        # 3.0.8: "stay signed in" = long-lived session cookie (autologout.py), no separate remember cookie
        session_cookie = " ".join(h for h in r.headers.getlist("Set-Cookie") if h.startswith("session="))
        self.assertIn("Expires=", session_cookie)
        with mock.patch.object(catalytix, "LDAP_RECHECK_SECONDS", 0), \
                mock.patch("catalytix.ldap_auth.Directory.__init__", return_value=None), \
                mock.patch("catalytix.ldap_auth.Directory.check_account", return_value=None):
            r = self.c.get("/")
            self.assertEqual(r.headers["Location"], "/login")
            self.assertIsNone(self.c.get_cookie("remember_token"))
            self.assertEqual(self.c.get("/login").status_code, 200)
            self.assertIn("/login", self.c.get("/").headers.get("Location", ""))     # really signed out

    def test_skipped_session_outside_remember_cookie_deleted(self):
        self.settings(mfa_internal="skip")
        self.give_totp("kasse")
        self.c.post("/login", data={"username": "kasse", "password": PW}, environ_base=LAN)
        self.c.set_cookie("remember_token", "x")
        r = self.c.get("/", environ_base=EXT)
        self.assertIn("/login", r.headers["Location"])
        self.assertTrue(any(h.startswith("remember_token=;") for h in r.headers.getlist("Set-Cookie")))


# ---------------------------------------------------------------- 8: recovery codes in the cookie
class CodesInCookieTests(Base):
    def test_codes_encrypted_in_session_and_removed(self):
        self.c.post("/login", data={"username": "leser", "password": PW})

        def fake_complete(user, rp_id, state, response, name, passkey=False):
            cred = WebAuthnCredential(user_id=user.id, rp_id=rp_id, name="K", credential_id="abc", credential_data="x")
            db.session.add(cred)
            db.session.commit()
            return cred
        with mock.patch("catalytix.twofa.webauthn_register_begin", return_value=({}, {"s": 1})), \
                mock.patch("catalytix.twofa.webauthn_register_complete", side_effect=fake_complete):
            self.assertEqual(self.c.post("/profil/sicherheit/schluessel/start").status_code, 200)
            r = self.c.post("/profil/sicherheit/schluessel/speichern", json={"credential": {}, "name": "K"})
        self.assertTrue(r.get_json()["ok"])
        ser = self.app.session_interface.get_signing_serializer(self.app)
        raw = ser.loads(self.c.get_cookie("session").value)
        self.assertIsInstance(raw["show_new_codes"], str)
        html = self.c.get("/profil/sicherheit?neu=1").get_data(as_text=True)
        codes = re.findall(r">([a-z2-9]{4}-[a-z2-9]{4})<", html)
        self.assertEqual(len(codes), 10)
        dump = json.dumps(raw)
        self.assertFalse([c for c in codes if c in dump or c.replace("-", "") in dump])
        self.assertNotIn("show_new_codes", ser.loads(self.c.get_cookie("session").value))


# ---------------------------------------------------------------- 9: e-mail in the profile
class ProfileMailTests(Base):
    def test_self_registered_domain_rule(self):
        self.settings(registration_domains="schule.de")
        u = self.user("leser")
        u.self_registered = True
        u.email_verified = True
        u.email = "leser@schule.de"
        db.session.commit()
        self.c.post("/login", data={"username": "leser", "password": PW})
        self.c.post("/profil", data={"action": "notify", "email": "x@fremd.org"})
        self.assertEqual(self.user("leser").email, "leser@schule.de")
        self.c.post("/profil", data={"action": "notify", "email": "neu@eltern.schule.de"})
        self.assertEqual(self.user("leser").email, "neu@eltern.schule.de")
        # not self-registered: no restriction
        c = self.app.test_client()
        c.post("/login", data={"username": "kasse", "password": PW})
        c.post("/profil", data={"action": "notify", "email": "k@fremd.org"})
        self.assertEqual(self.user("kasse").email, "k@fremd.org")


# ---------------------------------------------------------------- 10: initial setup only once
class SetupOnceTests(Base):
    def test_setup_not_reopened_without_admin(self):
        self.c.get("/login")
        self.assertEqual(get_setting("setup_completed"), "1")   # existing installation: set at start-up
        User.query.filter_by(role="admin").delete()
        db.session.commit()
        self.app.config["_setup_done"] = False                   # as after a restart
        r = self.c.get("/")
        self.assertNotIn("/setup", r.headers.get("Location", ""))
        r = self.c.get("/setup")
        self.assertEqual(r.status_code, 403)
        self.assertIn("create-admin", r.get_data(as_text=True))
        r = self.c.post("/setup", data={"username": "boss", "password": "supersecret1", "password2": "supersecret1"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(User.query.filter_by(username="boss").first())

    def test_fresh_setup_sets_flag(self):
        User.query.delete()
        db.session.commit()
        self.assertIn("/setup", self.c.get("/").headers["Location"])
        from catalytix import setupcode
        self.c.post("/setup", data={"username": "boss", "password": "supersecret1", "password2": "supersecret1",
                                    "setup_code": setupcode.ensure()})
        self.assertEqual(get_setting("setup_completed"), "1")


# ---------------------------------------------------------------- 11: language at login
class LoginLanguageTests(Base):
    def test_flash_after_login_in_user_language(self):
        self.settings(mfa_policy="all")
        u = self.user("leser")
        u.language = "en"
        db.session.commit()
        r = self.c.post("/login", data={"username": "leser", "password": PW}, headers={"Accept-Language": "de"})
        self.assertIn("/profil/sicherheit", r.headers["Location"])
        html = self.c.get("/profil/sicherheit").get_data(as_text=True)
        self.assertIn("Please set up a second factor now", html)

    def test_set_language_flash_in_new_language(self):
        from catalytix import i18n
        with self.app.test_request_context("/"):
            from flask import g
            g._catalytix_locale = "de"
            with mock.patch("catalytix.views.main.current_user") as cu:
                cu.is_authenticated = False
                from catalytix.views.main import set_language
                set_language("en")
            self.assertEqual(i18n.get_locale(), "en")


if __name__ == "__main__":
    unittest.main()
