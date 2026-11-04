"""Privacy when run on the internet: no third-party servers in the browser, legal links, cover proxy."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import Book, User, db, get_setting, set_setting  # noqa: E402
from catalytix.views import books as books_view  # noqa: E402

PKG = Path(__file__).resolve().parent.parent / "catalytix"
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", re.I)


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        for name, role in (("admin", "admin"), ("bib", "librarian"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local")
            u.set_password("geheim12345")
            db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": "geheim12345"})


class NoThirdPartyTests(Base):
    def test_static_files_reference_no_external_hosts(self):
        for f in list((PKG / "static").rglob("*.js")) + list((PKG / "static").rglob("*.css")):
            text = f.read_text(encoding="utf-8")
            hit = re.search(r"(?:https?:)?//(?!www\.w3\.org)[a-z0-9-]+\.[a-z0-9.-]*[a-z]{2,}", text, re.I)
            with self.subTest(file=f.name):
                self.assertIsNone(hit, f"{f.name} verweist auf {hit.group(0) if hit else ''}")

    def test_pages_load_nothing_from_outside(self):
        set_setting("imprint_url", "https://institut.example/impressum")
        db.session.commit()
        pages = ["/login"]
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("googleapis", html)
        self.login("admin")
        for url in ["/", "/buecher", "/buecher/neu", "/info", "/admin/einstellungen"]:
            page = self.c.get(url).get_data(as_text=True)
            pages.append(url)
            # only outgoing links (imprint etc.) are allowed, no embedded resources
            for m in re.finditer(r"""<(link|script|img)\b[^>]*>""", page, re.I):
                with self.subTest(url=url, tag=m.group(0)[:80]):
                    self.assertIsNone(EXTERNAL.search(m.group(0)))

    def test_fonts_are_bundled(self):
        css = (PKG / "static/css/fonts.css").read_text()
        for name in re.findall(r"url\(\.\./fonts/([^)]+)\)", css):
            self.assertTrue((PKG / "static/fonts" / name).is_file(), name)
        self.assertTrue((PKG / "static/fonts/LICENSE-Inter.txt").is_file())
        self.assertTrue((PKG / "static/fonts/LICENSE-Fraunces.txt").is_file())


class LegalLinkTests(Base):
    def test_hidden_when_not_configured(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("Impressum", html)

    def test_shown_on_login_sidebar_and_registration(self):
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"imprint_url": "https://institut.example/impressum",
                                                 "privacy_url": "https://institut.example/datenschutz",
                                                 "registration_mode": "open", "loan_days": "28"})
        self.assertEqual(get_setting("imprint_url"), "https://institut.example/impressum")
        sidebar = self.c.get("/").get_data(as_text=True)
        self.assertIn("https://institut.example/datenschutz", sidebar)
        self.c.post("/logout")
        login = self.c.get("/login").get_data(as_text=True)
        self.assertIn("https://institut.example/impressum", login)
        self.assertIn("https://institut.example/datenschutz", login)
        reg = self.c.get("/registrieren").get_data(as_text=True)
        self.assertIn("https://institut.example/datenschutz", reg)

    def test_rejects_javascript_urls(self):
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"imprint_url": "javascript:alert(1)", "loan_days": "28"})
        self.assertEqual(get_setting("imprint_url"), "")

    def test_old_registration_privacy_setting_is_taken_over(self):
        d = tempfile.mkdtemp()
        uri = f"sqlite:///{d}/alt.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            set_setting("registration_privacy_url", "https://alt.example/ds")
            db.session.commit()
            db.session.remove()
        app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app2.app_context():
            self.assertEqual(get_setting("privacy_url"), "https://alt.example/ds")
            db.session.remove()

    def test_mail_footer_contains_links(self):
        from catalytix.mailer import render_mail
        set_setting("imprint_url", "https://institut.example/impressum")
        set_setting("privacy_url", "https://institut.example/datenschutz")
        db.session.commit()
        with self.app.test_request_context():
            _, _, html = render_mail("test")
        self.assertIn("https://institut.example/impressum", html)
        self.assertIn("https://institut.example/datenschutz", html)


def _real_png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.effect_noise((120, 160), 80).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


PNG = _real_png()


class CoverProxyTests(Base):
    def setUp(self):
        super().setUp()
        books_view._COVER_FAILED.clear()

    def test_book_without_local_file_uses_own_server(self):
        b = Book(title="T", cover_url="https://covers.example/1.jpg")
        db.session.add(b)
        db.session.commit()
        with self.app.test_request_context():
            self.assertEqual(b.cover, f"/covers/buch/{b.id}")

    def test_remote_cover_is_fetched_once_and_stored(self):
        b = Book(title="T", cover_url="https://covers.example/1.png")
        db.session.add(b)
        db.session.commit()
        self.login("leser")
        with mock.patch.object(books_view, "_fetch_cover", return_value=(PNG, "image/png")) as f:
            r = self.c.get(f"/covers/buch/{b.id}")
            self.assertEqual(r.status_code, 302)
            self.assertIn("/covers/", r.headers["Location"])
            db.session.refresh(b)
            self.assertTrue(b.cover_file)
            self.c.get(f"/covers/buch/{b.id}")
            self.assertEqual(f.call_count, 1)

    def test_failed_remote_cover_is_not_retried_immediately(self):
        b = Book(title="T", cover_url="https://covers.example/kaputt.png")
        db.session.add(b)
        db.session.commit()
        self.login("leser")
        with mock.patch.object(books_view, "_fetch_cover", return_value=None) as f:
            self.assertEqual(self.c.get(f"/covers/buch/{b.id}").status_code, 404)
            self.assertEqual(self.c.get(f"/covers/buch/{b.id}").status_code, 404)
            self.assertEqual(f.call_count, 1)

    def test_preview_only_for_librarians(self):
        self.login("leser")
        self.assertNotEqual(self.c.get("/covers/vorschau?u=https://x.example/a.png").status_code, 200)
        self.login("bib")
        with mock.patch.object(books_view, "_fetch_cover", return_value=(PNG, "image/png")):
            r = self.c.get("/covers/vorschau?u=https://x.example/a.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")   # re-encoded

    def test_internal_addresses_are_never_fetched(self):
        for url in ["http://127.0.0.1/x.png", "https://localhost/x.png", "http://10.0.0.5/x.png",
                    "http://192.168.1.1/x.png", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                    "ftp://example.com/x.png"]:
            with self.subTest(url=url), mock.patch.object(books_view, "_http_get") as get:
                self.assertIsNone(books_view._fetch_cover(url))
                get.assert_not_called()

    def test_redirect_to_internal_address_is_blocked(self):
        resp = mock.Mock(is_redirect=True, headers={"Location": "http://127.0.0.1/geheim"})
        with mock.patch.object(books_view, "_public_host", side_effect=lambda h: h != "127.0.0.1"), \
                mock.patch.object(books_view, "_http_get", return_value=resp) as get:
            self.assertIsNone(books_view._fetch_cover("https://covers.example/1.png"))
            self.assertEqual(get.call_count, 1)

    def test_non_image_content_is_rejected(self):
        resp = mock.Mock(is_redirect=False, status_code=200, headers={"Content-Type": "image/svg+xml"})
        with mock.patch.object(books_view, "_public_host", return_value=True), \
                mock.patch.object(books_view, "_http_get", return_value=resp):
            self.assertIsNone(books_view._fetch_cover("https://covers.example/1.svg"))


@unittest.skipUnless(shutil.which("node"), "Node.js nicht installiert")
class Ean13DecoderTests(unittest.TestCase):
    """Test our own barcode detection (replacement for ZXing) with generated images."""

    L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
    G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"]
    R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"]
    PAR = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]

    def modules(self, code):
        p = self.PAR[int(code[0])]
        m = "101" + "".join((self.L if p[i] == "L" else self.G)[int(c)] for i, c in enumerate(code[1:7]))
        return m + "01010" + "".join(self.R[int(c)] for c in code[7:]) + "101"

    def render(self, code, mw, blur=0.0, rotate=0):
        from PIL import Image, ImageDraw, ImageFilter
        img = Image.new("L", (900, 400), 235)
        d = ImageDraw.Draw(img)
        mods, x0 = self.modules(code), (900 - 95 * mw) / 2
        i = 0
        while i < 95:
            if mods[i] == "1":
                j = i
                while j < 95 and mods[j] == "1":
                    j += 1
                d.rectangle([x0 + i * mw, 100, x0 + j * mw - 1, 300], fill=30)
                i = j
            else:
                i += 1
        if rotate:
            img = img.rotate(rotate, expand=True)
        if blur:
            img = img.filter(ImageFilter.GaussianBlur(blur))
        return img

    def decode(self, img):
        js = (PKG / "static/js/ean13.js").as_posix()
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
            f.write(img.tobytes())
        script = (f"const d=require({json.dumps(js)});const b=require('fs').readFileSync({json.dumps(f.name)});"
                  f"console.log(JSON.stringify(d.decodeImage(b,{img.width},{img.height})));")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        os.unlink(f.name)
        return json.loads(out.stdout.strip())

    def test_reads_isbn_barcodes(self):
        for code, mw, blur, rot in [("9783161484100", 3.5, 0, 0), ("9780306406157", 4.0, 0.8, 0),
                                    ("9791032305690", 5.0, 0.5, 180), ("9783866801929", 4.0, 0.6, 90)]:
            with self.subTest(code=code):
                self.assertEqual(self.decode(self.render(code, mw, blur, rot)), code)

    def test_no_result_without_barcode(self):
        from PIL import Image, ImageDraw
        img = Image.new("L", (900, 400), 235)
        ImageDraw.Draw(img).text((50, 180), "ISBN 978-3-16-148410-0", fill=20)
        self.assertIsNone(self.decode(img))


if __name__ == "__main__":
    unittest.main()
