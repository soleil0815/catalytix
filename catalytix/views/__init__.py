"""Hilfsfunktionen für die Views."""
from __future__ import annotations

from functools import wraps

from flask import abort, request
from flask_login import current_user, login_required


def librarian_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_librarian:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def form_str(name: str, maxlen: int = 500) -> str:
    return (request.form.get(name) or "").strip()[:maxlen]


DB_INT_MAX = 2_147_483_647   # größter Wert, den eine INTEGER-Spalte überall (auch MySQL/PostgreSQL) fasst


def is_digits(value) -> bool:
    """Nur ASCII-Ziffern 0–9 (str.isdigit() akzeptiert auch „²“ oder andere Schriften – int() scheitert daran)."""
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def parse_int(value, maximum: int = DB_INT_MAX) -> int | None:
    """Ganze Zahl aus Text (ASCII, optional mit Minus) oder int; sonst None. Zu große Werte → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        val = value
    elif isinstance(value, str):
        value = value.strip()
        sign = -1 if value.startswith("-") else 1
        digits = value[1:] if value[:1] in "+-" else value
        if not is_digits(digits) or len(digits) > 18:
            return None
        val = sign * int(digits)
    else:
        return None
    return val if abs(val) <= maximum else None


def parse_id(value) -> int | None:
    """Datenbank-ID (positiv, im INTEGER-Bereich) aus Formular-/URL-Text; sonst None."""
    val = parse_int(value)
    return val if val is not None and val > 0 else None


def get_by_id(model, ident):
    """db.session.get mit Bereichsprüfung: riesige IDs aus URL/Formular → None (sonst OverflowError → 500)."""
    from ..models import db
    if isinstance(ident, bool) or not isinstance(ident, int) or not 0 < ident <= DB_INT_MAX:
        return None
    return db.session.get(model, ident)


def form_int(name: str, default: int | None = None, minimum: int | None = None,
             maximum: int = DB_INT_MAX) -> int | None:
    raw = (request.form.get(name) or "").strip()
    digits = raw[1:] if raw[:1] in "+-" else raw
    if not is_digits(digits):
        return default
    val = int(raw) if len(digits) <= 18 else (maximum if raw[:1] != "-" else -maximum)
    if minimum is not None and val < minimum:
        return minimum
    return min(val, maximum)


def form_bool(name: str) -> bool:
    return request.form.get(name) in ("1", "on", "true", "yes")


def safe_next(target: str | None) -> str | None:
    """Nur relative Weiterleitungen zulassen (kein Open Redirect)."""
    if not target or any(ord(ch) < 0x21 or ch == "\x7f" for ch in target):
        return None   # Browser entfernen Tab/Zeilenumbruch – „/\t/evil“ würde sonst zu „//evil“
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None
