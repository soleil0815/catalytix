"""3.1.1: HTTPS enforcement in the full app (CATALYTIX_FORCE_HTTPS) and the Cross-Origin-Opener-Policy header."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402

PROXY = {"REMOTE_ADDR": "172.17.0.1"}


def client(force):
    with mock.patch.dict(os.environ, {"CATALYTIX_FORCE_HTTPS": force}):
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    return app.test_client()


class FullAppTests(unittest.TestCase):
    def test_redirect_and_hsts(self):
        c = client("1")
        r = c.get("/login", headers={"X-Forwarded-Proto": "http", "Host": "bib.firma.local"}, environ_base=PROXY)
        self.assertEqual((r.status_code, r.headers["Location"]), (301, "https://bib.firma.local/login"))
        r = c.get("/login", headers={"X-Forwarded-Proto": "https", "Host": "bib.firma.local"}, environ_base=PROXY)
        self.assertEqual(r.headers.get("Strict-Transport-Security"), "max-age=31536000")
        self.assertIn("Secure", r.headers.get("Set-Cookie", "Secure"))

    def test_health_check_unaffected(self):
        r = client("1").get("/login", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertNotIn(r.status_code, (301, 308))
        self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_off_by_default(self):
        r = client("").get("/login", headers={"X-Forwarded-Proto": "http"}, environ_base=PROXY)
        self.assertNotIn(r.status_code, (301, 308))

    def test_cross_origin_opener_policy(self):
        r = client("").get("/login")
        self.assertEqual(r.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertNotIn("Cross-Origin-Resource-Policy", r.headers)   # mails load the logo from here


if __name__ == "__main__":
    unittest.main()
