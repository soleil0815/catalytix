"""Helper functions for the views."""
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


DB_INT_MAX = 2_147_483_647   # largest value an INTEGER column holds everywhere (including MySQL/PostgreSQL)


def is_digits(value) -> bool:
    """ASCII digits 0–9 only (str.isdigit() also accepts "²" or other scripts – int() fails on those)."""
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def parse_int(value, maximum: int = DB_INT_MAX) -> int | None:
    """Integer from text (ASCII, optional minus sign) or int; otherwise None. Values too large → None."""
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
    """Database ID (positive, within INTEGER range) from form/URL text; otherwise None."""
    val = parse_int(value)
    return val if val is not None and val > 0 else None


def get_by_id(model, ident):
    """db.session.get with range check: huge IDs from URL/form → None (otherwise OverflowError → 500)."""
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
    """Allow only relative redirects (no open redirect)."""
    if not target or any(ord(ch) < 0x21 or ch == "\x7f" for ch in target):
        return None   # browsers strip tab/newline – "/\t/evil" would otherwise become "//evil"
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None
