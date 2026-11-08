"""Tests of the automatic reverse proxy and HTTPS detection."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-proxy-"))

from flask import Flask, jsonify, make_response, request  # noqa: E402

from catalytix.proxy import install, parse_networks  # noqa: E402


def make_app(env):
    app = Flask(__name__)

    @app.route("/")
    def index():
        resp = make_response(jsonify(ip=request.remote_addr, scheme=request.scheme, host=request.host))
        resp.set_cookie("session", "x", httponly=True)
        return resp

    install(app, env)
    return app.test_client()


FWD = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "bib.example.de"}


class ProxyTests(unittest.TestCase):
    def test_default_trusts_private_proxy(self):
        c = make_app({})
        r = c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "172.18.0.5"})
        # X-Forwarded-Host is deliberately not used (otherwise links in e-mails could be redirected);
        # reverse proxies like NPM, Caddy and Traefik pass the Host header through unchanged anyway.
        self.assertEqual(r.json, {"ip": "203.0.113.7", "scheme": "https", "host": "localhost"})
        self.assertIn("Secure", r.headers["Set-Cookie"])

    def test_public_client_cannot_spoof(self):
        c = make_app({})
        r = c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(r.json["ip"], "198.51.100.9")
        self.assertEqual(r.json["scheme"], "http")
        self.assertNotIn("Secure", r.headers["Set-Cookie"])

    def test_plain_http_in_lan_keeps_working(self):
        c = make_app({})
        r = c.get("/", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(r.json["scheme"], "http")
        self.assertNotIn("Secure", r.headers["Set-Cookie"])   # login via http://IP:9300 still works

    def test_only_last_forwarded_hop_is_used(self):
        c = make_app({})
        r = c.get("/", headers={**FWD, "X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
                  environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "203.0.113.7")

    def test_disabled_and_custom(self):
        r = make_app({"CATALYTIX_TRUSTED_PROXIES": "none"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "10.0.0.2")
        r = make_app({"CATALYTIX_BEHIND_PROXY": "0"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "10.0.0.2")
        c = make_app({"CATALYTIX_TRUSTED_PROXIES": "192.168.0.11"})
        self.assertEqual(c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "192.168.0.11"}).json["ip"], "203.0.113.7")
        self.assertEqual(c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "192.168.0.12"}).json["ip"], "192.168.0.12")

    def test_secure_cookie_modes(self):
        r = make_app({"CATALYTIX_SECURE_COOKIES": "1"}).get("/", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertIn("Secure", r.headers["Set-Cookie"])
        r = make_app({"CATALYTIX_SECURE_COOKIES": "0"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertNotIn("Secure", r.headers["Set-Cookie"])

    def test_parse_networks(self):
        self.assertEqual(len(parse_networks("private")), 6)
        self.assertEqual(parse_networks("none"), [])
        self.assertEqual(len(parse_networks("10.0.0.0/8, kaputt, ::1")), 2)

    def test_full_app_login_behind_proxy(self):
        from catalytix import create_app
        from catalytix.models import User, db
        app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        with app.app_context():
            db.drop_all(); db.create_all()
            u = User(username="admin", role="admin"); u.set_password("geheim123")
            db.session.add(u); db.session.commit()
            c = app.test_client()
            r = c.post("/login", data={"username": "admin", "password": "geheim123"}, headers=FWD,
                       environ_base={"REMOTE_ADDR": "172.18.0.5"})
            self.assertEqual(r.status_code, 302)
            self.assertTrue(all("Secure" in v for v in r.headers.getlist("Set-Cookie")))
            db.session.remove()



class ForceHttpsTests(unittest.TestCase):
    """3.1.1: CATALYTIX_FORCE_HTTPS=1 – for proxies that can neither redirect nor send HSTS (Synology)."""
    PROXY = {"REMOTE_ADDR": "172.17.0.1"}         # Docker bridge: the reverse proxy on the NAS

    def client(self, force="1"):
        return make_app({"CATALYTIX_FORCE_HTTPS": force})

    def test_http_through_proxy_is_redirected(self):
        r = self.client().get("/buecher?q=a b", headers={"X-Forwarded-Proto": "http", "Host": "bib.firma.local"},
                              environ_base=self.PROXY)
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers["Location"], "https://bib.firma.local/buecher?q=a%20b")

    def test_form_post_keeps_method(self):
        r = self.client().post("/login", data={"a": "b"},
                               headers={"X-Forwarded-Proto": "http", "Host": "bib.firma.local:80"},
                               environ_base=self.PROXY)
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r.headers["Location"], "https://bib.firma.local/login")

    def test_https_gets_hsts_for_this_host_only(self):
        r = self.client().get("/", headers={"X-Forwarded-Proto": "https"}, environ_base=self.PROXY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertNotIn("includeSubDomains", r.headers["Strict-Transport-Security"])

    def test_direct_requests_are_left_alone(self):
        # Docker health check and http://IP:9300 on the LAN: no proxy header – no redirect, no HSTS
        for env in ({"REMOTE_ADDR": "127.0.0.1"}, {"REMOTE_ADDR": "10.0.10.25"}):
            r = self.client().get("/", environ_base=env)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_untrusted_sender_cannot_trigger_a_redirect(self):
        r = self.client().get("/", headers={"X-Forwarded-Proto": "http", "Host": "evil.example"},
                              environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(r.status_code, 200)

    def test_bad_host_is_not_used(self):
        r = self.client().get("/", headers={"X-Forwarded-Proto": "http", "Host": "a b<script>"},
                              environ_base=self.PROXY)
        self.assertEqual(r.status_code, 200)

    def test_off_by_default(self):
        for force in ("", "0"):
            r = self.client(force).get("/", headers={"X-Forwarded-Proto": "http"}, environ_base=self.PROXY)
            self.assertEqual(r.status_code, 200)
            r = self.client(force).get("/", headers={"X-Forwarded-Proto": "https"}, environ_base=self.PROXY)
            self.assertNotIn("Strict-Transport-Security", r.headers)

if __name__ == "__main__":
    unittest.main()
