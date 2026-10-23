"""Erscheinungsbild (Corporate Design): Logo, Hintergrundbild der Anmeldeseite, Farben, Texte.

Bilder werden beim Hochladen geprüft und aufbereitet (Pillow):
  * Logo:        PNG/JPG/WebP/GIF -> PNG (max. 800×300, Transparenz bleibt), oder SVG (geprüft)
  * Hintergrund: PNG/JPG/WebP -> JPEG (max. 2560 px, Metadaten wie GPS werden entfernt)
  * Favicon:     wird automatisch aus dem Logo erzeugt (quadratisch, 128 px)
Dateien liegen in <Datenverzeichnis>/branding und tragen einen Inhalts-Hash im Namen
(Browser-Cache wird bei Änderung automatisch umgangen).
"""
from __future__ import annotations

import hashlib
import io
import os
import re

from flask import current_app, url_for

from .i18n import _
from .models import db, get_setting, set_setting

KINDS = {"logo": "brand_logo", "bg": "brand_bg"}
MAX_PIXELS = 50_000_000
SVG_FORBIDDEN = re.compile(rb"<\s*script|<\s*foreignObject|\bon[a-z]+\s*=|javascript:|<\s*iframe|<\s*embed|"
                           rb"<\s*object|xlink:href\s*=\s*[\"']\s*(?!#|data:image)", re.I)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS = {"accent": "#2f6f62", "brass": "#b0802f", "side": "#17322c"}


class BrandingError(Exception):
    pass


def brand_dir() -> str:
    path = os.path.join(current_app.instance_path, "branding")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- Bilder

def _write(kind: str, data: bytes, ext: str) -> str:
    name = f"{kind}-{hashlib.sha256(data).hexdigest()[:12]}{ext}"
    with open(os.path.join(brand_dir(), name), "wb") as f:
        f.write(data)
    return name


def _delete(name: str):
    if name:
        try:
            os.remove(os.path.join(brand_dir(), os.path.basename(name)))
        except OSError:
            pass


def _open_image(raw: bytes):
    from PIL import Image, ImageOps
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise BrandingError(_("Die Datei ist kein gültiges Bild (erlaubt: PNG, JPG, WebP, GIF, beim Logo auch SVG).")) from exc
    if img.format not in ("PNG", "JPEG", "WEBP", "GIF", "MPO"):
        raise BrandingError(_("Bildformat {format} wird nicht unterstützt.", format=img.format))
    if img.width * img.height > MAX_PIXELS:
        raise BrandingError(_("Das Bild ist zu groß (mehr als 50 Megapixel)."))
    img.seek(0)
    return ImageOps.exif_transpose(img)


def _check_svg(raw: bytes) -> bytes:
    head = raw[:1000].lstrip().lower()
    if not (head.startswith(b"<svg") or head.startswith(b"<?xml")) or b"<svg" not in raw[:5000].lower():
        raise BrandingError(_("Die SVG-Datei ist ungültig."))
    if len(raw) > 512 * 1024:
        raise BrandingError(_("SVG-Logos dürfen höchstens 512 KB groß sein."))
    if SVG_FORBIDDEN.search(raw) or re.search(rb"<!ENTITY", raw, re.I):
        raise BrandingError(_("Das SVG enthält Skripte, eingebettete Inhalte oder externe Verweise und wird aus "
                              "Sicherheitsgründen abgelehnt. Bitte als PNG hochladen."))
    return raw


def _favicon_from(img) -> bytes:
    from PIL import Image
    src = img.convert("RGBA")
    bbox = src.getbbox()          # transparente Ränder abschneiden
    if bbox:
        src = src.crop(bbox)
    src.thumbnail((120, 120), Image.LANCZOS)
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    canvas.paste(src, ((128 - src.width) // 2, (128 - src.height) // 2), src)
    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def store(kind: str, upload) -> None:
    """Speichert ein hochgeladenes Logo bzw. Hintergrundbild."""
    from PIL import Image
    raw = upload.read()
    if not raw:
        return
    filename = (upload.filename or "").lower()
    old = get_setting(KINDS[kind])

    if kind == "logo" and (filename.endswith(".svg") or raw[:200].lstrip().startswith((b"<svg", b"<?xml"))):
        name = _write("logo", _check_svg(raw), ".svg")
        fav_old = get_setting("brand_favicon")
        set_setting("brand_favicon", "")
        _delete(fav_old)
    elif kind == "logo":
        img = _open_image(raw)
        img = img.convert("RGBA")
        img.thumbnail((800, 300), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        name = _write("logo", out.getvalue(), ".png")
        fav_old = get_setting("brand_favicon")
        set_setting("brand_favicon", _write("favicon", _favicon_from(img), ".png"))
        if fav_old != get_setting("brand_favicon"):
            _delete(fav_old)
    else:
        img = _open_image(raw)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (20, 30, 26))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        img = img.convert("RGB")
        img.thumbnail((2560, 2560), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=84, optimize=True, progressive=True)   # ohne EXIF/GPS
        name = _write("bg", out.getvalue(), ".jpg")

    set_setting(KINDS[kind], name)
    if old and old != name:
        _delete(old)


def remove(kind: str) -> None:
    _delete(get_setting(KINDS[kind]))
    set_setting(KINDS[kind], "")
    if kind == "logo":
        _delete(get_setting("brand_favicon"))
        set_setting("brand_favicon", "")


def reset_all() -> None:
    remove("logo")
    remove("bg")
    for key in ("brand_accent", "brand_brass", "brand_side", "brand_bg_dim", "brand_logo_text", "brand_logo_plate",
                "login_show_shelf", "login_eyebrow", "login_text"):
        from .models import Setting
        row = db.session.get(Setting, key)
        if row is not None:
            db.session.delete(row)


def file_url(name: str) -> str:
    from flask import has_request_context
    if not name:
        return ""
    if has_request_context():
        return url_for("main.branding_file", filename=name)
    return f"/branding/{name}"   # z. B. beim Mailversand im Hintergrund


# ---------------------------------------------------------------- Farben

def clean_hex(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        value = "#" + "".join(c * 2 for c in value[1:])
    return value.lower() if HEX.match(value) else ""


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """t = Anteil von b (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def luminance(h: str) -> float:
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def ink(bg: str) -> str:
    """Gut lesbare Textfarbe (hell oder dunkel) auf der Hintergrundfarbe."""
    return "#ffffff" if contrast(bg, "#ffffff") >= contrast(bg, "#14191a") else "#14191a"


def colors() -> dict:
    return {k: clean_hex(get_setting(f"brand_{k}")) for k in DEFAULTS}


def theme_css() -> str:
    """CSS-Variablen für eigene Farben (nur aus geprüften Hex-Werten zusammengesetzt)."""
    c = colors()
    light, dark = [], []
    if c["accent"]:
        a = c["accent"]
        ad = mix(a, "#ffffff", 0.35) if luminance(a) < 0.25 else a
        light += [f"--accent:{a}", f"--accent-ink:{ink(a)}", f"--accent-soft:{mix(a, '#fffdf8', 0.84)}"]
        dark += [f"--accent:{ad}", f"--accent-ink:{ink(ad)}", f"--accent-soft:{mix(a, '#1a1f1d', 0.72)}"]
    if c["brass"]:
        b = c["brass"]
        bd = mix(b, "#ffffff", 0.25) if luminance(b) < 0.25 else b
        light += [f"--brass:{b}", f"--brass-ink:{ink(b)}", f"--brass-soft:{mix(b, '#fffdf8', 0.82)}",
                  f"--auth-eyebrow:{mix(b, '#ffffff', 0.3)}"]
        dark += [f"--brass:{bd}", f"--brass-ink:{ink(bd)}", f"--brass-soft:{mix(b, '#1a1f1d', 0.72)}"]
    if c["side"]:
        s = c["side"]
        dark_side = luminance(s) < 0.3
        s2 = mix(s, "#ffffff", 0.08) if dark_side else mix(s, "#000000", 0.07)
        sink = "#ece7dc" if dark_side else "#1d2421"
        smuted = mix(sink, s, 0.38)
        auth_a, auth_b = (mix(s, "#ffffff", 0.06), mix(s, "#000000", 0.35))
        common = [f"--side:{s}", f"--side-2:{s2}", f"--side-ink:{sink}", f"--side-muted:{smuted}",
                  f"--auth-a:{auth_a}", f"--auth-b:{auth_b}"]
        light += common
        sd = mix(s, "#000000", 0.4) if not dark_side else mix(s, "#000000", 0.25)
        dark += [f"--side:{sd}", f"--side-2:{mix(sd, '#ffffff', 0.08)}", f"--side-ink:#ece7dc",
                 f"--side-muted:{mix('#ece7dc', sd, 0.38)}"]
    if not light:
        return ""
    lc, dc = ";".join(light), ";".join(dark)
    return (f":root{{{lc}}}"
            f"@media (prefers-color-scheme: dark){{:root:not([data-theme=\"light\"]){{{dc}}}}}"
            f":root[data-theme=\"dark\"]{{{dc}}}")


def mail_colors() -> dict:
    c = colors()
    side = c["side"] or DEFAULTS["side"]
    accent = c["accent"] or DEFAULTS["accent"]
    sink = "#e9e3d4" if luminance(side) < 0.3 else "#1d2421"
    return {"mail_side": side, "mail_side_ink": sink, "mail_side_muted": mix(sink, side, 0.38),
            "mail_accent": accent, "mail_accent_ink": ink(accent)}


# ---------------------------------------------------------------- Für die Vorlagen

def _default_text(key: str) -> str:
    """Unveränderte Standardtexte (deutsch gespeichert) in der Sprache der Oberfläche zeigen."""
    from .models import DEFAULT_SETTINGS
    value = get_setting(key)
    return _(value) if value and value == DEFAULT_SETTINGS.get(key) else value


def library_name() -> str:
    """Name der Bibliothek – der unveränderte Standardname „Bibliothek“ in der aktuellen Sprache."""
    return _default_text("library_name") or _("Bibliothek")


def context() -> dict:
    logo =get_setting("brand_logo")
    bg = get_setting("brand_bg")
    fav = get_setting("brand_favicon")
    try:
        dim = max(0, min(85, int(get_setting("brand_bg_dim") or 45)))
    except ValueError:
        dim = 45
    return {
        "logo_url": file_url(logo),
        "logo_is_svg": logo.endswith(".svg"),
        "bg_url": file_url(bg),
        "bg_dim": dim,
        "favicon_url": file_url(fav) if fav else (file_url(logo) if logo.endswith(".svg") else ""),
        "show_text": get_setting("brand_logo_text") != "0",
        "logo_plate": get_setting("brand_logo_plate") == "1",
        "show_shelf": get_setting("login_show_shelf") != "0",
        "eyebrow": _default_text("login_eyebrow"),
        "text": _default_text("login_text"),
        "theme_css": theme_css(),
    }
