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
from catalytix.models import Book, Group, db, get_setting, set_setting  # noqa: E402

ISBN = "9783462045277"      # any valid ISBN-13
VOLUME = "AbC_12-xyZ"


def answer(viewability="PARTIAL", embeddable=True, volume=VOLUME):
    return {"items": [{"id": "noSample01", "accessInfo": {"embeddable": False, "viewability": "NO_PAGES"}},
                      {"id": volume, "accessInfo": {"embeddable": embeddable, "viewability": viewability}}]}


class LookupTests(unittest.TestCase):
    def setUp(self):
        reading_sample._cache.clear()

    def test_pick_only_embeddable_samples(self):
        self.assertEqual(reading_sample.pick(answer()), VOLUME)
        self.assertEqual(reading_sample.pick(answer(viewability="ALL_PAGES")), VOLUME)
        self.assertEqual(reading_sample.pick(answer(viewability="NO_PAGES")), "")
        self.assertEqual(reading_sample.pick(answer(embeddable=False)), "")
        self.assertEqual(reading_sample.pick({}), "")
        self.assertEqual(reading_sample.pick(None), "")
        self.assertEqual(reading_sample.pick({"items": ["x", {"accessInfo": "y"}]}), "")
        # the id goes into a URL: anything but a plain Google volume id is ignored
        for bad in ('x"><script>', "../../evil", "a b c d e f", "kurz"):
            self.assertEqual(reading_sample.pick(answer(volume=bad)), "", bad)

    def test_lookup_cached_found_none_and_errors(self):
        calls = []

        def fetch(isbn):
            calls.append(isbn)
            return VOLUME
        self.assertEqual(reading_sample.lookup("978-3-462-04527-7", now=1000, fetch=fetch), VOLUME)
        self.assertEqual(reading_sample.lookup(ISBN, now=2000, fetch=fetch), VOLUME)
        self.assertEqual(calls, [ISBN])                                  # cached, one question to Google
        self.assertEqual(reading_sample.lookup(ISBN, now=1000 + reading_sample.TTL_FOUND + 1, fetch=fetch), VOLUME)
        self.assertEqual(len(calls), 2)                                  # asked again after a week

        def down(isbn):
            raise requests.ConnectionError("no internet")
        reading_sample._cache.clear()
        self.assertIsNone(reading_sample.lookup(ISBN, now=1000, fetch=down))
        self.assertIsNone(reading_sample.lookup(ISBN, now=1000 + 60, fetch=fetch))       # error kept 15 minutes
        self.assertEqual(reading_sample.lookup(ISBN, now=1000 + reading_sample.TTL_ERROR + 1, fetch=fetch), VOLUME)

    def test_isbn10_and_invalid(self):
        seen = []
        self.assertEqual(reading_sample.lookup("3-462-04527-X", fetch=lambda i: seen.append(i) or VOLUME), VOLUME)
        self.assertEqual(seen, [ISBN])                                   # asked with the ISBN-13
        self.assertIsNone(reading_sample.lookup("", fetch=lambda i: 1 / 0))
        self.assertIsNone(reading_sample.lookup("12345", fetch=lambda i: 1 / 0))

    def test_only_the_isbn_goes_to_google(self):
        resp = mock.Mock(status_code=200, json=lambda: answer())
        with mock.patch("catalytix.reading_sample.requests.get", return_value=resp) as get:
            self.assertEqual(reading_sample.lookup(ISBN), VOLUME)
        url = get.call_args.args[0]
        self.assertEqual(url, "https://www.googleapis.com/books/v1/volumes")
        self.assertEqual(get.call_args.kwargs["params"]["q"], f"isbn:{ISBN}")
        self.assertNotIn("cookies", get.call_args.kwargs)

    def test_embed_url_and_policy(self):
        self.assertEqual(reading_sample.embed_url(VOLUME),
                         f"https://books.google.com/books?id={VOLUME}&printsec=frontcover&output=embed")
        with self.assertRaises(ValueError):
            reading_sample.embed_url('x"onload=')
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

    def enable(self, on=True):
        set_setting("reading_sample_google", "1" if on else "0")
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
        self.assertIn(f'src="https://books.google.com/books?id={VOLUME}&amp;printsec=frontcover&amp;output=embed"', html)
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

    def test_help_mentions_it_only_when_on(self):
        self.assertNotIn("Leseprobe", self.c.get("/hilfe?thema=catalog").get_data(as_text=True))
        self.enable()
        self.assertIn("Leseprobe laden", self.c.get("/hilfe?thema=catalog").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
