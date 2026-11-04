"""Multilingual support (German / English) without additional dependencies.

Principles
- The German text is also the key: ``_("Buch erfassen")``. If a translation is missing,
  the German text is shown – nothing breaks.
- Placeholders in curly braces: ``_("{n} Bücher angepasst", n=3)``.
- Singular/plural: ``_n("{n} Buch", "{n} Bücher", n)``.
- In templates ``_`` returns safe markup: the text (written by us) may contain simple
  markup such as <b>; inserted values are always escaped.
- For texts created at import time (e.g. role names) there is ``_l`` (translated only when
  rendered).

Language of a request: the user's choice (profile) → choice on the login page (session) →
browser language → default language from the settings → German.
"""
from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from pathlib import Path

from markupsafe import Markup, escape

LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT = "de"

_CATALOG_DIR = Path(__file__).parent / "translations"
_catalogs: dict[str, dict[str, str]] = {}
_forced: contextvars.ContextVar[str | None] = contextvars.ContextVar("catalytix_forced_locale", default=None)


def catalog(lang: str) -> dict[str, str]:
    if lang not in _catalogs:
        path = _CATALOG_DIR / f"{lang}.json"
        try:
            _catalogs[lang] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            _catalogs[lang] = {}
        # Development only: merge in additional catalog parts from a folder
        import os
        extra = os.environ.get("CATALYTIX_I18N_PARTS")
        if extra and lang == "en":
            for part in sorted(Path(extra).glob("*.json")):
                try:
                    _catalogs[lang].update(json.loads(part.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
    return _catalogs[lang]


def normalize(lang: str | None) -> str | None:
    lang = (lang or "").strip().lower()[:2]
    return lang if lang in LANGUAGES else None


def default_language() -> str:
    try:
        from .models import get_setting
        return normalize(get_setting("default_language")) or DEFAULT
    except Exception:  # noqa: BLE001 – e.g. without an app context
        return DEFAULT


def browser_language() -> str | None:
    from flask import has_request_context, request
    if not has_request_context():
        return None
    return normalize(request.accept_languages.best_match(list(LANGUAGES)))


def get_locale() -> str:
    forced = _forced.get()
    if forced:
        return forced
    from flask import g, has_request_context, session
    if not has_request_context():
        return default_language()
    cached = g.get("_catalytix_locale")
    if cached:
        return cached
    lang = None
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            lang = normalize(getattr(current_user, "language", ""))
    except Exception:  # noqa: BLE001
        lang = None
    lang = lang or normalize(session.get("lang")) or browser_language() or default_language()
    g._catalytix_locale = lang
    return lang


def user_language(user) -> str:
    """Language for emails to a user (outside of a request)."""
    return normalize(getattr(user, "language", "")) or default_language()


@contextmanager
def force_locale(lang: str | None):
    token = _forced.set(normalize(lang) or default_language())
    try:
        yield
    finally:
        _forced.reset(token)


def _lookup(msg: str) -> str:
    lang = get_locale()
    if lang == DEFAULT:
        return msg
    return catalog(lang).get(msg) or msg


def _fmt(text: str, kw: dict) -> str:
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def _lookup_ctx(ctx: str, msg: str) -> str:
    lang = get_locale()
    if lang == DEFAULT:
        return msg
    cat = catalog(lang)
    return cat.get(f"{msg}|{ctx}") or cat.get(msg) or msg


def pgettext(ctx: str, msg: str, /, **kw) -> str:
    """Same German word, different meaning (e.g. "Benutzer" as a column and as a page title).
    Catalog key: "Benutzer|plural"."""
    return _fmt(_lookup_ctx(ctx, msg), kw)


def gettext(msg: str, /, **kw) -> str:
    return _fmt(_lookup(msg), kw)


def ngettext(singular: str, plural: str, n: int, /, **kw) -> str:
    kw.setdefault("n", n)
    return _fmt(_lookup(singular if n == 1 else plural), kw)


_ = gettext
_n = ngettext


class LazyString:
    """Translated only when rendered (for module-level texts)."""

    __slots__ = ("msg", "kw")

    def __init__(self, msg: str, /, **kw):
        self.msg = msg
        self.kw = kw

    def __str__(self):
        return gettext(self.msg, **self.kw)

    def __html__(self):
        return Markup(escape(str(self)))

    def __repr__(self):
        return f"_l({self.msg!r})"

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):   # consistent with __eq__ (compares the translated text)
        return hash(str(self))

    def __len__(self):
        return len(str(self))

    def __add__(self, other):
        return str(self) + str(other)

    def __radd__(self, other):
        return str(other) + str(self)

    def __format__(self, spec):
        return format(str(self), spec)

    def __getattr__(self, name):   # .lower(), .startswith() …
        return getattr(str(self), name)


def lazy_gettext(msg: str, /, **kw) -> LazyString:
    return LazyString(msg, **kw)


_l = lazy_gettext


# ---------------------------------------------------------------- Templates

def template_gettext(msg: str, /, **kw) -> Markup:
    text = _lookup(msg)
    if not kw:
        return Markup(text)
    try:
        return Markup(text).format(**kw)
    except (KeyError, IndexError, ValueError):
        return Markup(text)


def template_pgettext(ctx: str, msg: str, /, **kw) -> Markup:
    text = _lookup_ctx(ctx, msg)
    return Markup(text).format(**kw) if kw else Markup(text)


def template_ngettext(singular: str, plural: str, n: int, /, **kw) -> Markup:
    kw.setdefault("n", n)
    return template_gettext(singular if n == 1 else plural, **kw)


MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_day(value) -> str:
    if not value:
        return "–"
    if get_locale() == "en":
        return f"{value.day} {MONTHS_EN[value.month - 1]} {value.year}"
    return value.strftime("%d.%m.%Y")


def format_datetime(value) -> str:
    from datetime import datetime
    if not value:
        return "–"
    if isinstance(value, datetime):
        return f"{format_day(value)} {value.strftime('%H:%M')}"
    return format_day(value)


def js_strings() -> dict[str, str]:
    """Translations for app.js (only the texts used there)."""
    lang = get_locale()
    if lang == DEFAULT:
        return {}
    cat = catalog(lang)
    return {k: cat[k] for k in JS_MESSAGES if k in cat}


# Texts shown by app.js – app.js calls T("…") with exactly these German texts.
JS_MESSAGES: list[str] = []


def _collect_js_messages():
    import re
    js = Path(__file__).parent / "static" / "js" / "app.js"
    try:
        text = js.read_text(encoding="utf-8")
    except OSError:
        return
    JS_MESSAGES[:] = sorted(set(re.findall(r'\bT\("((?:[^"\\]|\\.)*)"', text)))


def _reset_locale_on_login(sender, user=None, **_kw):
    """After login_user: determine the language again (the user's), not the previously cached one."""
    from flask import g, has_app_context
    if has_app_context():
        g.pop("_catalytix_locale", None)


def init_app(app):
    _collect_js_messages()
    from flask_login import user_logged_in
    user_logged_in.connect(_reset_locale_on_login, app)
    app.jinja_env.globals.update(_=template_gettext, _n=template_ngettext, _p=template_pgettext, get_locale=get_locale,
                                 LANGUAGES=LANGUAGES, js_strings=js_strings)
    app.jinja_env.filters["de_date"] = format_datetime
    app.jinja_env.filters["de_day"] = format_day
    app.jinja_env.filters["datetime"] = format_datetime
    app.jinja_env.filters["day"] = format_day
