"""3.1.4: no browser/proxy caching of pages and data (shared computers), no file behind a trailing slash."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402


class CacheTests(BaseCase):
    def test_pages_are_not_stored(self):
        self.assertEqual(self.c.get("/login").headers["Cache-Control"], "no-store")
        self.make_user("admin", "admin")
        self.login("admin")
        for url in ("/", "/admin/einstellungen", "/api/sitzung"):
            with self.subTest(url=url):
                self.assertEqual(self.c.get(url).headers["Cache-Control"], "no-store")

    def test_static_files_keep_caching(self):
        r = self.c.get("/static/js/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("no-store", r.headers.get("Cache-Control", ""))
        r.close()

    def test_error_pages_are_not_stored(self):
        self.assertEqual(self.c.get("/gibt-es-nicht").headers["Cache-Control"], "no-store")

    def test_trailing_slash_on_static_file(self):
        self.assertEqual(self.c.get("/static/js/app.js/").status_code, 404)
        self.assertEqual(self.c.get("/static/css/app.css/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
