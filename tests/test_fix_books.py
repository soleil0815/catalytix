"""Nachbesserungen aus der Überprüfung: Katalog, Ausleihe, Theke, Standorte, ISBN, Cover."""
import io
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from PIL import Image  # noqa: E402
from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from catalytix import create_app  # noqa: E402
from catalytix import isbn as isbnlib  # noqa: E402
from catalytix.models import Book, Group, Loan, Location, User, db, ensure_indexes, set_setting  # noqa: E402
from catalytix.views import books as books_view  # noqa: E402
from catalytix.views import loans as loans_view  # noqa: E402

PW = "geheim12345"
NO_PROXY = {"NO_PROXY": "*", "no_proxy": "*"}


def _image(fmt="PNG", size=(300, 300)):
    buf = io.BytesIO()
    Image.effect_noise(size, 80).convert("RGB").save(buf, fmt)
    return buf.getvalue()


PNG = _image()


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve(handler):
    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    db_uri = "sqlite://"

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": self.db_uri,
                               "PROPAGATE_EXCEPTIONS": False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from flask import g

        def _fresh():
            db.session.rollback()
            g.pop("_login_user", None)
            g.pop("_catalytix_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        self.ga, self.gb = Group(name="A"), Group(name="B")
        db.session.add_all([self.ga, self.gb])
        db.session.commit()
        self.admin = self.make("admin", "admin")
        self.biba = self.make("biba", "librarian", [self.ga])
        self.leser = self.make("leser", "reader", [self.ga])
        self.lb = self.make("lb", "reader", [self.gb])
        self.c = self.app.test_client()
        books_view._COVER_FAILED.clear()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, groups=()):
        u = User(username=name, display_name=name.title(), role=role)
        u.set_password(PW)
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        db.session.rollback()
        self.c = self.app.test_client()
        r = self.c.post("/login", data={"username": name, "password": PW})
        self.assertEqual(r.status_code, 302)
        return self.c

    def client_for(self, name):
        c = self.app.test_client()
        c.post("/login", data={"username": name, "password": PW})
        return c

    def book(self, **kw):
        b = Book(title=kw.pop("title", "T"), **kw)
        db.session.add(b)
        db.session.commit()
        return b

    def loan(self, book, user, returned=False):
        l = Loan(book_id=book.id, user_id=user.id, due_at=date.today() + timedelta(days=14),
                 returned_at=datetime.now() if returned else None)
        db.session.add(l)
        db.session.commit()
        return l


# ---------------------------------------------------------------- 1. Gleichzeitige Ausleihen (Datei-SQLite)

class RaceTests(Base):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cx-race-")
        self.db_uri = f"sqlite:///{self.dir}/race.db"
        super().setUp()

    def run_parallel(self, jobs, patch_target, patch_attr, key=None):
        """Alle Anfragen am gleichen Punkt (vor der Prüfung) zusammentreffen lassen."""
        barrier = threading.Barrier(len(jobs), timeout=3)
        real = getattr(patch_target, patch_attr)

        def waiting(*a, **k):
            result = real(*a, **k)
            if key is None or key in a:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return result
        out = []
        with mock.patch.object(patch_target, patch_attr, waiting):
            threads = [threading.Thread(target=lambda j=j: out.append(j())) for j in jobs]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
        db.session.rollback()
        db.session.expire_all()
        return out

    def test_last_copy_is_lent_only_once(self):
        b = self.book(title="Letztes", copies=1)
        users = [self.make(f"u{i}", "reader") for i in range(4)]
        clients = [self.client_for(u.username) for u in users]
        bid = b.id
        out = self.run_parallel([lambda c=c: c.post(f"/buecher/{bid}/ausleihen").status_code for c in clients],
                                User, "can_borrow")
        self.assertEqual(out, [302] * 4)
        self.assertEqual(Loan.query.filter_by(book_id=b.id, returned_at=None).count(), 1)

    def test_same_user_same_book_only_once(self):
        b = self.book(title="Viele", copies=5)
        c1, c2, c3 = (self.client_for("leser") for _ in range(3))
        bid = b.id
        self.run_parallel([lambda c=c: c.post(f"/buecher/{bid}/ausleihen").status_code for c in (c1, c2, c3)],
                          User, "can_borrow")
        self.assertEqual(Loan.query.filter_by(book_id=b.id, user_id=self.leser.id, returned_at=None).count(), 1)

    def test_max_loans_not_exceeded(self):
        set_setting("max_loans", "1")
        db.session.commit()
        b1, b2, b3 = (self.book(title=f"B{i}", copies=3) for i in range(3))
        clients = [self.client_for("leser") for _ in range(3)]
        ids = [b1.id, b2.id, b3.id]
        self.run_parallel([lambda c=c, i=i: c.post(f"/buecher/{i}/ausleihen").status_code
                           for c, i in zip(clients, ids)], User, "can_borrow")
        self.assertEqual(Loan.query.filter_by(user_id=self.leser.id, returned_at=None).count(), 1)

    def test_self_extend_respects_maximum(self):
        set_setting("self_extend", "1")
        set_setting("max_extensions", "1")
        db.session.commit()
        b = self.book(copies=1)
        l = self.loan(b, self.leser)
        clients = [self.client_for("leser") for _ in range(3)]
        lid = l.id
        self.run_parallel([lambda c=c: c.post(f"/ausleihen/{lid}/verlaengern").status_code for c in clients],
                          loans_view, "get_bool", key="self_extend")
        self.assertEqual(db.session.get(Loan, l.id).extensions, 1)

    def test_add_copy_counts_every_click(self):
        b = self.book(copies=1)
        clients = [self.client_for("admin") for _ in range(3)]
        bid = b.id
        self.run_parallel([lambda c=c: c.post(f"/api/buecher/{bid}/exemplar").status_code for c in clients],
                          books_view, "_get_book_or_404")
        self.assertEqual(db.session.get(Book, b.id).copies, 4)


class ActiveLoanIndexTests(Base):
    def test_second_open_loan_rejected_by_database(self):
        b = self.book(copies=3)
        self.loan(b, self.leser)
        self.loan(b, self.leser, returned=True)   # abgeschlossene zählen nicht
        with self.assertRaises(IntegrityError):
            self.loan(b, self.leser)
        db.session.rollback()

    def test_existing_duplicates_only_warn(self):
        db.session.execute(text("DROP INDEX uq_loans_active_book_user"))
        db.session.commit()
        b = self.book(copies=3)
        self.loan(b, self.leser)
        self.loan(b, self.leser)
        with self.assertLogs("catalytix.models", "WARNING"):
            self.assertFalse(ensure_indexes())
        Loan.query.filter_by(book_id=b.id).first().returned_at = datetime.now()
        db.session.commit()
        self.assertTrue(ensure_indexes())

    def test_lend_message_when_already_lent(self):
        b = self.book(copies=3)
        self.loan(b, self.leser)
        self.login("leser")
        r = self.c.post(f"/buecher/{b.id}/ausleihen", follow_redirects=True)
        self.assertIn("bereits ausgeliehen", r.get_data(as_text=True))


# ---------------------------------------------------------------- 2./3. Cover: DNS-Rebinding, Fristen, kaputte Bilder

class CoverTests(Base):
    def test_ip_checks(self):
        for addr in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "fe80::1%eth0",
                     "64:ff9b::7f00:1", "64:ff9b:1::a00:1", "::ffff:127.0.0.1", "::ffff:93.184.215.14", "::7f00:1",
                     "2002:7f00:1::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "fc00::1", "kaputt"]:
            with self.subTest(addr=addr):
                self.assertFalse(books_view._ip_allowed(addr))
        for addr in ["93.184.215.14", "2606:2800:220:1:248:1893:25c8:1946", "2002:5db8:d70e::1"]:
            with self.subTest(addr=addr):
                self.assertTrue(books_view._ip_allowed(addr))

    def _server(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Host")))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)

            def log_message(self, *a):
                pass
        return serve(H), hits

    def _fake_dns(self, table):
        real = socket.getaddrinfo
        calls = {}

        def fake(host, port, *a, **k):
            if host in table:
                calls[host] = calls.get(host, 0) + 1
                answers = table[host]
                ip = answers[min(calls[host], len(answers)) - 1]
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            return real(host, port, *a, **k)
        return mock.patch("socket.getaddrinfo", side_effect=fake)

    def test_dns_rebinding_is_blocked(self):
        srv, hits = self._server()
        with mock.patch.dict(os.environ, NO_PROXY), \
                self._fake_dns({"rebind.test": ["93.184.215.14", "127.0.0.1"]}):
            self.assertIsNone(books_view._fetch_cover(f"http://rebind.test:{srv.server_port}/intern.png"))
            self.login("biba")
            r = self.c.get(f"/covers/vorschau?u=http://rebind.test:{srv.server_port}/intern.png")
            self.assertEqual(r.status_code, 404)
        self.assertEqual(hits, [])
        srv.shutdown()

    def test_connection_goes_to_checked_address_with_host_header(self):
        srv, hits = self._server()
        with mock.patch.dict(os.environ, NO_PROXY), self._fake_dns({"cover.test": ["127.0.0.1"]}), \
                mock.patch.object(books_view, "_ip_allowed", return_value=True):
            got = books_view._fetch_cover(f"http://cover.test:{srv.server_port}/c.png")
        self.assertEqual(got, (PNG, "image/png"))
        self.assertEqual(hits, [("/c.png", f"cover.test:{srv.server_port}")])
        srv.shutdown()

    def test_slow_server_hits_overall_deadline(self):
        class Drip(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.5)
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        srv = serve(Drip)
        with mock.patch.dict(os.environ, NO_PROXY), mock.patch.object(books_view, "_ip_allowed", return_value=True), \
                mock.patch.object(books_view, "COVER_DEADLINE", 2):
            t = time.monotonic()
            self.assertIsNone(books_view._fetch_cover(f"http://127.0.0.1:{srv.server_port}/x.png"))
            self.assertLess(time.monotonic() - t, 4.5)
        srv.shutdown()

    def test_truncated_image_is_rejected_without_error(self):
        jpg = _image("JPEG", (600, 600))
        self.assertIsNone(books_view._clean_image(jpg[: len(jpg) // 2]))

    def test_broken_remote_cover_gives_404_and_is_not_retried(self):
        class Cut(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b"x" * 10)

            def log_message(self, *a):
                pass
        srv = serve(Cut)
        b = self.book(cover_url=f"http://127.0.0.1:{srv.server_port}/c.jpg")
        self.login("leser")
        with mock.patch.dict(os.environ, NO_PROXY), mock.patch.object(books_view, "_ip_allowed", return_value=True):
            self.assertEqual(self.c.get(f"/covers/buch/{b.id}").status_code, 404)
        jpg = _image("JPEG", (600, 600))
        b2 = self.book(cover_url="https://covers.example/kaputt.jpg")
        with mock.patch.object(books_view, "_fetch_cover", return_value=(jpg[: len(jpg) // 2], "image/jpeg")) as f:
            self.assertEqual(self.c.get(f"/covers/buch/{b2.id}").status_code, 404)
            self.assertEqual(self.c.get(f"/covers/buch/{b2.id}").status_code, 404)
            self.assertEqual(f.call_count, 1)
        with mock.patch.object(books_view, "_fetch_cover", side_effect=RuntimeError("kaputt")):
            self.login("admin")
            r = self.c.post("/buecher/neu", data={"title": "Neu", "copies": "1", "cover_url": "https://x.example/a.jpg"})
            self.assertEqual(r.status_code, 302)
        srv.shutdown()


# ---------------------------------------------------------------- 4. Theke: Signatur vor Buch-Nummer

class DeskSearchTests(Base):
    def test_digits_match_signature_not_internal_id(self):
        target = self.book(title="Alpha")
        other = self.book(title="Beta", signature=str(target.id))
        lower = self.book(title="Gamma", signature="ab-7")
        self.login("admin")
        with self.app.test_request_context():
            from flask_login import login_user
            login_user(self.admin)
            self.assertEqual([b.id for b in loans_view._find_books(str(target.id))], [other.id])
            self.assertEqual([b.id for b in loans_view._find_books("AB-7")], [lower.id])
            self.assertEqual([b.id for b in loans_view._find_books(f"#{target.id}")], [target.id])
            self.assertEqual(loans_view._find_books("987654"), [])
            self.assertEqual(loans_view._find_books("#99999999999999999999"), [])


# ---------------------------------------------------------------- 5. ISBN mit „X“ an falscher Stelle

class IsbnTests(Base):
    def test_x_not_at_end_is_left_unchanged(self):
        self.assertEqual(isbnlib.to_isbn13("12345X7890"), "12345X7890")
        self.assertEqual(isbnlib.to_isbn13("XXXXXXXXXX"), "XXXXXXXXXX")
        self.assertEqual(isbnlib.to_isbn13("3-86680-192-X"), "9783866801929")
        self.assertIsNone(isbnlib.to_isbn10("978XXXXXXXXXX"))

    def test_pages_do_not_crash(self):
        b = self.book(group_id=self.ga.id)
        self.login("leser")
        self.assertEqual(self.c.get("/buecher?q=1X34567890").status_code, 200)
        self.login("biba")
        self.assertEqual(self.c.get("/theke?code=12345X7890").status_code, 200)
        self.assertEqual(self.c.post(f"/buecher/{b.id}/bearbeiten",
                                     data={"title": "x", "isbn": "X23456789X", "copies": "1"}).status_code, 302)
        self.assertEqual(self.c.post("/api/buecher", json={"title": "x", "isbn": "X23456789X"}).status_code, 200)


# ---------------------------------------------------------------- 6. Katalog/Export ohne Abfrage je Buch

class PerformanceTests(Base):
    def setUp(self):
        super().setUp()
        db.session.bulk_save_objects([Book(title=f"Buch {i:03}", copies=1, location="R1" if i % 2 else "R2")
                                      for i in range(120)])
        db.session.commit()
        ids = [b.id for b in Book.query.order_by(Book.title).limit(30)]
        db.session.bulk_save_objects([Loan(book_id=i, user_id=self.leser.id, due_at=date(2030, 1, 1)) for i in ids])
        db.session.bulk_save_objects([Loan(book_id=i, user_id=self.lb.id, due_at=date(2030, 1, 1),
                                           returned_at=datetime.now()) for i in ids[30:] + ids[:5]])
        db.session.commit()
        self.queries = [0]

        @event.listens_for(db.engine, "before_cursor_execute")
        def count(*a):
            self.queries[0] += 1
        self.addCleanup(event.remove, db.engine, "before_cursor_execute", count)

    def get(self, url):
        self.queries[0] = 0
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True), self.queries[0]

    def test_status_filter_and_paging_in_sql(self):
        self.login("admin")
        html, n = self.get("/buecher?status=verfuegbar")
        self.assertIn("90 Titel gefunden", html)
        self.assertLess(n, 60)
        self.assertNotIn(">Buch 000<", html)
        html, n = self.get("/buecher?status=ausgeliehen&seite=2")
        self.assertIn("30 Titel gefunden", html)
        self.assertIn(">Buch 024<", html)
        self.assertNotIn(">Buch 030<", html)
        html, n = self.get("/buecher?sort=title&seite=5")
        self.assertIn(">Buch 119<", html)
        self.assertEqual(html.count('class="book-card"'), 24)

    def test_export_uses_aggregate(self):
        self.login("admin")
        csv_text, n = self.get("/buecher/export.csv")
        self.assertLess(n, 40)
        rows = [r.split(";") for r in csv_text.strip().splitlines()[1:]]
        self.assertEqual(len(rows), 120)
        avail = {r[2]: r[-1] for r in rows}
        self.assertEqual(avail["Buch 000"], "0")
        self.assertEqual(avail["Buch 050"], "1")


# ---------------------------------------------------------------- 7.–10. Sichtbarkeit und Rechte

class VisibilityTests(Base):
    def test_desk_hides_other_groups_loans_and_names(self):
        secret = self.book(title="Secret-B", group_id=self.gb.id)
        mine = self.book(title="Eigenes-A", group_id=self.ga.id)
        self.lb.groups = [self.ga, self.gb]
        db.session.commit()
        self.loan(secret, self.lb)
        self.loan(mine, self.lb)
        self.login("biba")
        html = self.c.get("/theke?benutzer=lb").get_data(as_text=True)
        self.assertNotIn("Secret-B", html)
        self.assertIn("Eigenes-A", html)
        self.assertNotIn("A, B", html)
        self.assertIn("Gruppen: A", html)

    def test_adopt_new_checks_other_groups(self):
        self.book(title="mine", location="Regal 1 ", group_id=self.ga.id)
        theirs = self.book(title="theirs", location="Regal 1 ", group_id=self.gb.id)
        self.login("biba")
        self.c.post("/standorte/bestand", data={"value": "Regal 1 ", "target": "__neu__"})
        db.session.expire_all()
        self.assertEqual(db.session.get(Book, theirs.id).location, "Regal 1 ")
        self.assertEqual(Location.query.count(), 0)
        self.login("admin")
        self.c.post("/standorte/bestand", data={"value": "Regal 1 ", "target": "__neu__"})
        db.session.expire_all()
        self.assertEqual(db.session.get(Book, theirs.id).location, "Regal 1")

    def test_api_isbn_only_visible_books(self):
        self.book(title="Secret-B", group_id=self.gb.id, isbn="9783161484100")
        self.book(title="Eigenes-A", group_id=self.ga.id, isbn="9783161484100")
        self.login("biba")
        data = self.c.get("/api/isbn/9783161484100?lookup=0").get_json()
        self.assertEqual([b["title"] for b in data["existing"]], ["Eigenes-A"])
        self.assertNotIn("Secret-B", str(data))

    def test_no_reminder_for_returned_loan(self):
        b = self.book(group_id=self.ga.id)
        l = self.loan(b, self.leser, returned=True)
        self.login("biba")
        with mock.patch.object(loans_view, "send_manual_reminder", return_value=(True, "")) as send:
            r = self.c.post(f"/ausleihen/{l.id}/erinnern", follow_redirects=True)
        send.assert_not_called()
        self.assertIn("bereits abgeschlossen", r.get_data(as_text=True))


# ---------------------------------------------------------------- 11. Ungültige Eingaben → kein 500

class MalformedInputTests(Base):
    def test_no_server_errors(self):
        b = self.book(group_id=self.ga.id, location="foo")
        Location.query.delete()
        db.session.add(Location(name="²"))
        db.session.commit()
        self.login("leser")
        self.assertEqual(self.c.get("/buecher?gruppe=%C2%B2").status_code, 200)
        self.assertEqual(self.c.get("/buecher/" + "9" * 25).status_code, 404)
        self.login("biba")
        H = "9" * 30
        checks = [
            self.c.post(f"/buecher/{b.id}/bearbeiten", data={"title": "x", "group_id": "²", "copies": "1"}),
            self.c.get("/theke?code=%C2%B2"),
            self.c.post("/standorte/bestand", data={"value": "foo", "target": "²"}),
            self.c.post("/standorte/bestand", data={"value": "foo", "target": H}),
            self.c.post(f"/buecher/{b.id}/ausleihen", data={"user_id": H}),
            self.c.post("/theke", data={"modus": "rueckgabe", "loan_id": H}),
            self.c.post(f"/ausleihen/{H}/erinnern"),
            self.c.get("/standorte"),
        ]
        for r in checks:
            self.assertLess(r.status_code, 500, r.request.path)

    def test_huge_numbers_are_clamped(self):
        b = self.book(group_id=self.ga.id)
        self.login("biba")
        r = self.c.post(f"/buecher/{b.id}/bearbeiten", data={"title": "x", "pages": "9" * 30, "copies": "9" * 30})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        b = db.session.get(Book, b.id)
        self.assertEqual((b.pages, b.copies), (books_view.MAX_PAGES, books_view.MAX_COPIES))
        r = self.c.post("/api/buecher", json={"title": "y", "pages": 10 ** 30, "copies": 10 ** 30})
        self.assertEqual(r.status_code, 200)
        nb = db.session.get(Book, r.get_json()["id"])
        self.assertEqual((nb.pages, nb.copies), (books_view.MAX_PAGES, books_view.MAX_COPIES))

    def test_quick_add_bad_json_is_400(self):
        self.login("biba")
        for body in ([1], "text", {"title": "x", "group_id": [1]}, {"title": "x", "group_id": 1.5},
                     {"title": "x", "subtitle": {"a": 1}}, {"title": ["x"]}, {"title": "x", "group_id": "²"}):
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/api/buecher", json=body).status_code, 400)
        self.assertEqual(self.c.post("/api/buecher", json={"title": "ok", "group_id": self.ga.id}).status_code, 200)


# ---------------------------------------------------------------- 12./13. Blättern

class PagingTests(Base):
    def test_catalogue_pages_keep_location(self):
        db.session.add(Location(name="R1"))
        db.session.commit()
        for i in range(30):
            self.book(title=f"b{i:02}", location="R1")
        self.login("leser")
        html = self.c.get("/buecher?standort=R1").get_data(as_text=True)
        links = re.findall(r'href="(/buecher\?[^"]*seite=2[^"]*)"', html)
        self.assertTrue(links)
        self.assertTrue(all("standort=R1" in l for l in links))

    def test_loan_list_is_paginated(self):
        db.session.bulk_save_objects([Book(title=f"L{i:03}", copies=1) for i in range(120)])
        db.session.commit()
        db.session.bulk_save_objects([Loan(book_id=b.id, user_id=self.leser.id, due_at=date(2030, 1, 1))
                                      for b in Book.query.all()])
        db.session.commit()
        self.login("admin")
        html = self.c.get("/ausleihen?q=L").get_data(as_text=True)
        self.assertEqual(html.count('class="cell-book"'), 50)
        self.assertIn("seite=3", html)
        self.assertIn("q=L", re.search(r'href="([^"]*seite=3[^"]*)"', html).group(1))
        html = self.c.get("/ausleihen?q=L&seite=3").get_data(as_text=True)
        self.assertEqual(html.count('class="cell-book"'), 20)


if __name__ == "__main__":
    unittest.main()
