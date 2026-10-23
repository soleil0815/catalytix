"""Tests für das Erscheinungsbild (Logo, Hintergrund, Farben, Texte)."""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-brand-"))

from PIL import Image  # noqa: E402

from catalytix import branding, create_app  # noqa: E402
from catalytix.models import User, db, get_setting, set_setting  # noqa: E402


def png(size=(400, 120), color=(200, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    buf.seek(0)
    return buf


def jpeg_with_gps(size=(3000, 2000)):
    img = Image.new("RGB", size, (40, 90, 160))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (52.0, 31.0, 12.0)}   # GPSInfo
    exif[0x010F] = "Kamera AG"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    return buf


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.app.instance_path = self.tmp
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        for name, role in (("admin", "admin"), ("lena", "reader")):
            u = User(username=name, role=role)
            u.set_password("geheim123")
            db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def save(self, **data):
        base = {"action": "save", "brand_logo_text": "1", "login_show_shelf": "1", "brand_bg_dim": "45",
                "login_eyebrow": "Willkommen", "login_text": "Hallo", "library_name": "Stadtbücherei"}
        base.update(data)
        return self.c.post("/admin/erscheinungsbild", data=base, content_type="multipart/form-data",
                           follow_redirects=True)

    def files(self):
        return sorted(os.listdir(os.path.join(self.tmp, "branding")))

    def test_logo_upload_favicon_and_display(self):
        r = self.save(logo=(png((2000, 600)), "logo.png"))
        self.assertIn("gespeichert", r.get_data(as_text=True))
        logo, fav = get_setting("brand_logo"), get_setting("brand_favicon")
        self.assertTrue(logo.startswith("logo-") and logo.endswith(".png"))
        self.assertTrue(fav.startswith("favicon-"))
        with Image.open(os.path.join(self.tmp, "branding", logo)) as im:
            self.assertLessEqual(im.width, 800)
        with Image.open(os.path.join(self.tmp, "branding", fav)) as im:
            self.assertEqual(im.size, (128, 128))
        self.c.post("/logout")
        html = self.c.get("/login").get_data(as_text=True)
        self.assertIn(f"/branding/{logo}", html)
        self.assertIn(f'rel="icon" href="/branding/{fav}"', html)
        self.assertEqual(self.c.get(f"/branding/{logo}").status_code, 200)   # öffentlich erreichbar
        # Ersetzen löscht die alte Datei
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})
        self.save(logo=(png(color=(0, 0, 255, 255)), "neu.png"))
        self.assertNotIn(logo, self.files())

    def test_background_resized_and_metadata_removed(self):
        self.save(bg=(jpeg_with_gps(), "foto.jpg"), brand_bg_dim="60")
        name = get_setting("brand_bg")
        with Image.open(os.path.join(self.tmp, "branding", name)) as im:
            self.assertLessEqual(max(im.size), 2560)
            self.assertNotIn(0x8825, im.getexif())
        self.assertEqual(get_setting("brand_bg_dim"), "60")
        self.c.post("/logout")
        html = self.c.get("/login").get_data(as_text=True)
        self.assertIn("has-bg", html)
        self.assertIn("--auth-dim: 0.6", html)

    def test_invalid_files_rejected(self):
        r = self.save(logo=(io.BytesIO(b"kein bild"), "x.png"))
        self.assertIn("kein gültiges Bild", r.get_data(as_text=True))
        evil = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        r = self.save(logo=(io.BytesIO(evil), "evil.svg"))
        self.assertIn("Sicherheitsgründen", r.get_data(as_text=True))
        r = self.save(logo=(io.BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>'), "a.svg"))
        self.assertIn("Sicherheitsgründen", r.get_data(as_text=True))
        self.assertEqual(get_setting("brand_logo"), "")

    def test_clean_svg_served_with_csp(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#c00"/></svg>'
        self.save(logo=(io.BytesIO(svg), "logo.svg"))
        name = get_setting("brand_logo")
        r = self.c.get(f"/branding/{name}")
        self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])

    def test_branding_route_is_restricted(self):
        self.assertEqual(self.c.get("/branding/secret_key").status_code, 404)
        self.assertEqual(self.c.get("/branding/..%2Fsecret_key").status_code, 404)

    def test_colors_and_theme_css(self):
        self.save(brand_accent="#C0FFEE", brand_brass="nope", brand_side="#ffffff")
        self.assertEqual(get_setting("brand_accent"), "#c0ffee")
        self.assertEqual(get_setting("brand_brass"), "")
        css = branding.theme_css()
        self.assertIn("--accent:#c0ffee", css)
        self.assertIn("--accent-ink:#14191a", css)        # heller Hintergrund -> dunkle Schrift
        self.assertIn("--side-ink:#1d2421", css)           # helle Seitenleiste -> dunkle Schrift
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn("--accent:#c0ffee", self.c.get("/").get_data(as_text=True))
        self.assertEqual(branding.ink("#17322c"), "#ffffff")

    def test_texts_and_toggles(self):
        self.save(login_eyebrow="Hallo Welt", login_text="Unsere Mediathek", login_show_shelf="", brand_logo_text="")
        self.c.post("/logout")
        html = self.c.get("/login").get_data(as_text=True)
        self.assertIn("Hallo Welt", html)
        self.assertIn("Unsere Mediathek", html)
        self.assertIn("Stadtbücherei", html)
        self.assertNotIn('class="shelf"', html)

    def test_reset(self):
        self.save(logo=(png(), "l.png"), bg=(jpeg_with_gps((800, 600)), "b.jpg"), brand_accent="#123456")
        self.assertTrue(self.files())
        self.c.post("/admin/erscheinungsbild", data={"action": "reset"})
        self.assertEqual(self.files(), [])
        self.assertEqual(get_setting("brand_accent"), "")
        self.assertEqual(get_setting("login_eyebrow"), "Willkommen")

    def test_reader_has_no_access_and_preview(self):
        self.assertEqual(self.c.get("/admin/erscheinungsbild/vorschau").status_code, 200)
        self.assertEqual(self.c.get("/admin/erscheinungsbild").status_code, 200)
        self.c.post("/logout")
        self.c.post("/login", data={"username": "lena", "password": "geheim123"})
        self.assertEqual(self.c.get("/admin/erscheinungsbild").status_code, 403)
        self.assertEqual(self.c.post("/admin/erscheinungsbild", data={"action": "reset"}).status_code, 403)

    def test_mail_uses_brand(self):
        from catalytix import mailer
        self.save(logo=(png(), "l.png"), brand_accent="#aa2233", brand_side="#102030")
        set_setting("app_base_url", "https://bib.example.de")
        db.session.commit()
        subject, text, html = mailer.render_mail("test", server="x", now="jetzt")
        self.assertIn(f"https://bib.example.de/branding/{get_setting('brand_logo')}", html)
        self.assertIn("#102030", html)


if __name__ == "__main__":
    unittest.main()
