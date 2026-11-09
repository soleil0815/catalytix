"""3.1.5: reading samples from Google Books (catalytix/reading_sample.py) – optional, two clicks."""
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import CSP, reading_sample  # noqa: E402
from catalytix import isbn as isbnlib  # noqa: E402
from catalytix.models import Book, Group, db, get_setting, set_setting  # noqa: E402

ISBN = "9783462045277"      # any valid ISBN-13
VOLUME = "AbC_12-xyZ"


def answer(viewability="PARTIAL", embeddable=True, volume=VOLUME):
    return {"items": [{"id": "noSample01", "accessInfo": {"embeddable": False, "viewability": "NO_PAGES"}},
                      {"id": volume, "accessInfo": {"embeddable": embeddable, "viewability": viewability}}]}


class LookupTests(unittest.TestCase):
    def setUp(self):
        reading_sample.clear_cache()

    def test_pick_any_known_volume(self):
        # 3.1.8: accessInfo said NO_PAGES / embeddable false for a book whose preview the viewer shows – not trusted
        self.assertEqual(reading_sample.pick(answer()), "noSample01")
        self.assertEqual(reading_sample.pick({"items": [{"id": VOLUME, "accessInfo": {
            "embeddable": False, "viewability": "NO_PAGES"}}]}), VOLUME)
        self.assertEqual(reading_sample.pick({"totalItems": 0}), "")
        self.assertEqual(reading_sample.pick({}), "")
        self.assertEqual(reading_sample.pick(None), "")
        self.assertEqual(reading_sample.pick({"items": ["x", {"accessInfo": "y"}]}), "")
        # the id goes into a URL: anything but a plain Google volume id is ignored
        for bad in ('x"><script>', "../../evil", "a b c d e f", "kurz"):
            self.assertEqual(reading_sample.pick({"items": [{"id": bad}]}), "", bad)

    def test_check_cached_found_none_and_errors(self):
        calls = []

        def fetch(isbn):
            calls.append(isbn)
            return VOLUME
        self.assertEqual(reading_sample.check("978-3-462-04527-7", now=1000, fetch=fetch), VOLUME)
        self.assertEqual(reading_sample.check(ISBN, now=2000, fetch=fetch), VOLUME)
        self.assertEqual(calls, [ISBN])                                  # cached, one question to Google
        self.assertEqual(reading_sample.check(ISBN, now=1000 + reading_sample.TTL_FOUND + 1, fetch=fetch), VOLUME)
        self.assertEqual(len(calls), 2)                                  # asked again after a week
        reading_sample._cache.clear()
        self.assertEqual(reading_sample.check(ISBN, now=1000, fetch=lambda i: ""), "")       # Google: no sample

        def down(isbn):
            raise isbnlib.GoogleBooksError(None)
        reading_sample._cache.clear()
        self.assertEqual(reading_sample.check(ISBN, now=1000, fetch=down), reading_sample.UNKNOWN)
        self.assertEqual(reading_sample.check(ISBN, now=1000 + 60, fetch=fetch), reading_sample.UNKNOWN)   # 15 min
        self.assertEqual(reading_sample.check(ISBN, now=1000 + reading_sample.TTL_ERROR + 1, fetch=fetch), VOLUME)

    def test_quota_is_remembered_for_the_settings_page(self):
        def quota(isbn):
            raise isbnlib.GoogleBooksError(429)
        with self.assertLogs("catalytix.reading_sample", "WARNING") as logs:
            self.assertEqual(reading_sample.check(ISBN, now=5000, fetch=quota), reading_sample.UNKNOWN)
        self.assertIn("daily limit reached (HTTP 429)", logs.output[0])
        self.assertTrue(reading_sample.quota_problem(now=5000 + 3600))
        self.assertFalse(reading_sample.quota_problem(now=5000 + 25 * 3600))
        reading_sample.clear_cache()
        self.assertFalse(reading_sample.quota_problem(now=5000 + 60))

    def test_isbn10_and_invalid(self):
        seen = []
        self.assertEqual(reading_sample.check("3-462-04527-X", fetch=lambda i: seen.append(i) or VOLUME), VOLUME)
        self.assertEqual(seen, [ISBN])                                   # asked with the ISBN-13
        self.assertIsNone(reading_sample.check("", fetch=lambda i: 1 / 0))
        self.assertIsNone(reading_sample.check("12345", fetch=lambda i: 1 / 0))


class GoogleKeyTests(BaseCase):
    """Without an own key Google answers no queries (HTTP 429, daily limit 0) – 3.1.6."""

    def setUp(self):
        super().setUp()
        reading_sample.clear_cache()

    def test_without_key_no_query_and_the_viewer_opens_by_isbn(self):
        with mock.patch("catalytix.isbn.requests.get") as get:
            self.assertEqual(reading_sample.check(ISBN), reading_sample.UNKNOWN)
            self.assertTrue(reading_sample.available(ISBN))
            self.assertEqual(reading_sample.target("3-462-04527-X"),
                             f"https://books.google.com/books?vid=ISBN{ISBN}&printsec=frontcover&output=embed")
        get.assert_not_called()
        self.assertFalse(reading_sample.available(""))
        self.assertIsNone(reading_sample.target("12345"))

    def test_with_key_only_isbn_and_key_go_to_google(self):
        set_setting("google_api_key", "AIzaSyTestKey_0123456789abcdef")
        db.session.commit()
        resp = mock.Mock(status_code=200, json=lambda: answer())
        with mock.patch("catalytix.isbn.requests.get", return_value=resp) as get:
            self.assertEqual(reading_sample.target(ISBN),
                             f"https://books.google.com/books?vid=ISBN{ISBN}&printsec=frontcover&output=embed")
        self.assertEqual(get.call_args.args[0], "https://www.googleapis.com/books/v1/volumes")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["q"], f"isbn:{ISBN}")
        self.assertEqual(params["key"], "AIzaSyTestKey_0123456789abcdef")
        self.assertNotIn("cookies", get.call_args.kwargs)
        reading_sample.clear_cache()
        not_embeddable = mock.Mock(status_code=200, json=lambda: {"items": [{"id": VOLUME, "accessInfo": {
            "embeddable": False, "viewability": "NO_PAGES"}}]})
        with mock.patch("catalytix.isbn.requests.get", return_value=not_embeddable):
            self.assertTrue(reading_sample.available(ISBN))            # the viewer decides, not accessInfo
        reading_sample.clear_cache()
        unknown = mock.Mock(status_code=200, json=lambda: {"totalItems": 0})
        with mock.patch("catalytix.isbn.requests.get", return_value=unknown):
            self.assertFalse(reading_sample.available(ISBN))           # Google does not know the book → no button
            self.assertIsNone(reading_sample.target(ISBN))

    def test_key_never_in_errors_or_logs(self):
        key = "AIzaSySecretKey_0123456789abcdef"
        set_setting("google_api_key", key)
        db.session.commit()
        err = requests.ConnectionError(f"Max retries exceeded with url: /books/v1/volumes?q=isbn:1&key={key}")
        with mock.patch("catalytix.isbn.requests.get", side_effect=err), self.assertLogs("catalytix", "INFO") as logs:
            self.assertEqual(reading_sample.check(ISBN), reading_sample.UNKNOWN)
            result = isbnlib.lookup(ISBN, ["google"])
        self.assertNotIn(key, "\n".join(logs.output) + repr(result))
        with mock.patch("catalytix.isbn.requests.get", side_effect=err):
            with self.assertRaises(isbnlib.GoogleBooksError) as cm:
                isbnlib.google_get({"q": "isbn:1"})
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)              # no traceback with the URL
        quota = mock.Mock(status_code=429, json=lambda: {})
        with mock.patch("catalytix.isbn.requests.get", return_value=quota):
            with self.assertRaises(isbnlib.GoogleBooksError) as cm:
                isbnlib.google_get({"q": "isbn:1"})
        self.assertTrue(cm.exception.quota)
        self.assertEqual(str(cm.exception), "HTTP 429")

    def test_key_stored_encrypted(self):
        set_setting("google_api_key", "AIzaSyTestKey_0123456789abcdef")
        db.session.commit()
        from catalytix.models import Setting
        raw = db.session.get(Setting, "google_api_key").value
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("AIzaSyTestKey", raw)
        self.assertEqual(get_setting("google_api_key"), "AIzaSyTestKey_0123456789abcdef")


    def test_policy(self):
        policy = reading_sample.csp(CSP)
        self.assertIn("script-src 'self';", policy)                     # no third-party script in our pages
        self.assertIn("frame-src https://books.google.com", policy)
        self.assertNotIn("frame-src", CSP)                                # all other pages: no frames from outside


class PageTests(BaseCase):
    def setUp(self):
        super().setUp()
        reading_sample._cache.clear()
        self.make_user("admin", "admin")
        self.make_user("leser")
        self.book = Book(title="Testbuch", isbn=ISBN, copies=1)
        db.session.add(self.book)
        db.session.commit()
        self.login("leser")

    def enable(self, on=True, key="AIzaSyTestKey_0123456789abcdef"):
        set_setting("reading_sample_google", "1" if on else "0")
        set_setting("google_api_key", key)
        db.session.commit()

    def test_off_by_default(self):
        self.assertEqual(get_setting("reading_sample_google"), "0")
        self.assertNotIn("data-reading-sample", self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True))
        self.assertEqual(self.c.get(f"/buecher/{self.book.id}/leseprobe").status_code, 404)
        self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").status_code, 404)

    def test_button_hidden_until_the_server_found_a_sample(self):
        self.enable()
        html = self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True)
        self.assertIn(f'hidden data-reading-sample="/api/leseprobe/{self.book.id}"', html)
        self.assertNotIn("google", html.lower().split("data-reading-sample")[1][:300])   # no Google address
        with mock.patch.object(reading_sample, "_fetch", return_value=VOLUME):
            self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").get_json(), {"available": True})
        reading_sample._cache.clear()
        with mock.patch.object(reading_sample, "_fetch", return_value=""):
            self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").get_json(), {"available": False})

    def test_no_button_without_isbn(self):
        self.enable()
        self.book.isbn = ""
        db.session.commit()
        self.assertNotIn("data-reading-sample", self.c.get(f"/buecher/{self.book.id}").get_data(as_text=True))

    def test_two_clicks(self):
        self.enable()
        with mock.patch.object(reading_sample, "_fetch", return_value=VOLUME) as fetch:
            first = self.c.get(f"/buecher/{self.book.id}/leseprobe")
            self.assertEqual(fetch.call_count, 0)                         # the notice needs no lookup
        html = first.get_data(as_text=True)
        self.assertIn("Die Leseprobe wird direkt von Google Books geladen", html)
        self.assertIn(f"/buecher/{self.book.id}/leseprobe?laden=1", html)
        self.assertNotIn("<iframe", html)
        self.assertNotIn("frame-src", first.headers["Content-Security-Policy"])
        with mock.patch.object(reading_sample, "_fetch", return_value=VOLUME):
            second = self.c.get(f"/buecher/{self.book.id}/leseprobe?laden=1")
        html = second.get_data(as_text=True)
        self.assertEqual(second.status_code, 200)
        self.assertIn(f'src="https://books.google.com/books?vid=ISBN{ISBN}&amp;printsec=frontcover&amp;output=embed"', html)
        self.assertIn('referrerpolicy="no-referrer"', html)
        self.assertIn('sandbox="allow-scripts allow-same-origin allow-popups', html)
        policy = second.headers["Content-Security-Policy"]
        self.assertIn("frame-src https://books.google.com", policy)
        self.assertIn("script-src 'self';", policy)
        self.assertNotIn("googleapis", html)                              # the lookup stays on the server

    def test_no_sample(self):
        self.enable()
        with mock.patch.object(reading_sample, "_fetch", return_value=""):
            r = self.c.get(f"/buecher/{self.book.id}/leseprobe?laden=1")
        self.assertEqual(r.status_code, 404)
        self.assertIn("derzeit keine Leseprobe", r.get_data(as_text=True))
        self.assertNotIn("frame-src", r.headers["Content-Security-Policy"])

    def test_books_of_other_groups_stay_hidden(self):
        self.enable()
        g = Group(name="Team")
        db.session.add(g)
        self.book.group = g
        db.session.commit()
        with mock.patch.object(reading_sample, "_fetch", return_value=VOLUME) as fetch:
            self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").status_code, 404)
            self.assertEqual(self.c.get(f"/buecher/{self.book.id}/leseprobe?laden=1").status_code, 404)
        self.assertEqual(fetch.call_count, 0)

    def test_signed_out(self):
        self.enable()
        self.c.post("/logout")
        self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").status_code, 401)
        self.assertIn("/login", self.c.get(f"/buecher/{self.book.id}/leseprobe").headers["Location"])

    def test_admin_setting(self):
        self.login("admin")
        page = self.c.get("/admin/einstellungen").get_data(as_text=True)
        self.assertIn('name="reading_sample_google"', page)
        self.c.post("/admin/einstellungen", data={"library_name": "Bibliothek", "isbn_order": "dnb",
                                                  "reading_sample_google": "1"})
        self.assertEqual(get_setting("reading_sample_google"), "1")
        self.c.post("/admin/einstellungen", data={"library_name": "Bibliothek"})     # form of another section
        self.assertEqual(get_setting("reading_sample_google"), "1")
        self.c.post("/admin/einstellungen", data={"library_name": "Bibliothek", "isbn_order": "dnb"})
        self.assertEqual(get_setting("reading_sample_google"), "0")

    def test_without_key_button_for_every_isbn_and_viewer_by_isbn(self):
        self.enable(key="")
        with mock.patch("catalytix.isbn.requests.get") as get:
            self.assertEqual(self.c.get(f"/api/leseprobe/{self.book.id}").get_json(), {"available": True})
            r = self.c.get(f"/buecher/{self.book.id}/leseprobe?laden=1")
        get.assert_not_called()                                           # Google answers no anonymous queries
        self.assertIn(f'src="https://books.google.com/books?vid=ISBN{ISBN}&amp;printsec=frontcover', r.get_data(as_text=True))
        self.assertIn("frame-src https://books.google.com", r.headers["Content-Security-Policy"])

    def test_admin_key_saved_kept_cleared_and_never_shown(self):
        self.login("admin")
        form = {"library_name": "Bibliothek", "isbn_order": "dnb", "reading_sample_google": "1"}
        self.c.post("/admin/einstellungen", data={**form, "google_api_key": " AIzaSyTestKey_0123456789abcdef "})
        self.assertEqual(get_setting("google_api_key"), "AIzaSyTestKey_0123456789abcdef")
        page = self.c.get("/admin/einstellungen").get_data(as_text=True)
        self.assertNotIn("AIzaSyTestKey", page)                          # never sent back to the browser
        self.assertIn('name="clear_google_api_key"', page)
        self.c.post("/admin/einstellungen", data=form)                    # empty field: keep
        self.assertEqual(get_setting("google_api_key"), "AIzaSyTestKey_0123456789abcdef")
        r = self.c.post("/admin/einstellungen", data={**form, "google_api_key": "<script>"}, follow_redirects=True)
        self.assertIn("nicht das erwartete Format", r.get_data(as_text=True))
        self.assertEqual(get_setting("google_api_key"), "AIzaSyTestKey_0123456789abcdef")
        self.c.post("/admin/einstellungen", data={**form, "clear_google_api_key": "1"})
        self.assertEqual(get_setting("google_api_key"), "")

    def test_quota_notice_on_the_settings_page(self):
        self.login("admin")
        self.assertNotIn("wegen des Tageslimits", self.c.get("/admin/einstellungen").get_data(as_text=True))
        reading_sample._quota_at = __import__("time").time()
        self.assertIn("wegen des Tageslimits", self.c.get("/admin/einstellungen").get_data(as_text=True))
        reading_sample.clear_cache()

    def test_help_mentions_it_only_when_on(self):
        self.assertNotIn("Leseprobe", self.c.get("/hilfe?thema=catalog").get_data(as_text=True))
        self.enable()
        self.assertIn("Leseprobe laden", self.c.get("/hilfe?thema=catalog").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
