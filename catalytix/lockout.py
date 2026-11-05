"""Login lockout after failed attempts – stored in the database so it applies to all server processes
and an administrator can lift it in the UI.

8 failed attempts per username and address within 5 minutes lock that combination for
5 minutes (password and second factor are counted separately).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

from .models import LoginAttempt, db

MAX_FAILS = 8
WINDOW = timedelta(minutes=5)


def norm(username: str) -> str:
    """Username for the lockout key: as with AD login, without "DOMAIN\\" and "@…", lowercased.

    Otherwise "DOMAIN\\anna", "anna@x" and "ANNA" would each get their own fresh lockout."""
    name = (username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    # NFKC + casefold: "ａｎｎａ" (full-width) or "ANNA" count as "anna" – AD compares similarly leniently
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _since() -> datetime:
    return datetime.now() - WINDOW


def throttled(key: str) -> bool:
    n: int = LoginAttempt.query.filter(LoginAttempt.key == key, LoginAttempt.created_at >= _since()).count()
    return n >= MAX_FAILS


def _origin() -> tuple[str, bool]:
    """Address and "internal?" of the current request (outside a request: empty)."""
    from flask import has_request_context, request
    if not has_request_context():
        return "", False
    from . import netzone
    return (request.remote_addr or "")[:64], netzone.is_internal()


def fail(key: str, username: str) -> None:
    ip, internal = _origin()
    db.session.add(LoginAttempt(key=key[:300], username=norm(username)[:200], ip=ip, internal=internal))
    db.session.commit()


USER_MAX_FAILS = 40
USER_MAX_FAILS_INTERNAL = 100
USER_WINDOW = timedelta(minutes=15)


def user_throttled(username: str, internal: bool = False) -> bool:
    """Additional throttle per username across all addresses (against guessing from changing IPs).

    On the internal network only failed attempts from the internal network count (higher limit): anyone
    deliberately entering wrong passwords from outside locks the account only for external access – on the
    internal network people can still log in."""
    since = datetime.now() - USER_WINDOW
    q = LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.created_at >= since)
    if internal:
        n_internal: int = q.filter(LoginAttempt.internal.is_(True)).count()
        return n_internal >= USER_MAX_FAILS_INTERNAL
    n: int = q.count()
    return n >= USER_MAX_FAILS


IP_MAX_FAILS = 30          # failed attempts per external address across all accounts (against "one password for all")


def ip_throttled() -> bool:
    ip, internal = _origin()
    if not ip or internal:
        return False
    since = datetime.now() - USER_WINDOW
    n: int = LoginAttempt.query.filter(LoginAttempt.ip == ip, LoginAttempt.created_at >= since).count()
    return n >= IP_MAX_FAILS


MFA_DAILY_MAX = 30         # wrong second-factor codes per account and day – then admin or tomorrow


def mfa_daily_exceeded(username: str) -> bool:
    since = datetime.now() - timedelta(days=1)
    n: int = LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                       LoginAttempt.created_at >= since).count()
    return n >= MFA_DAILY_MAX


def mfa_fails_since(username: str, ts: float) -> int:
    """Failed second-factor attempts for this user since time ts (Unix time)."""
    since = max(datetime.fromtimestamp(ts or 0), datetime.now() - USER_WINDOW)
    n: int = LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                       LoginAttempt.created_at >= since).count()
    return n


def clear(key: str) -> None:
    LoginAttempt.query.filter_by(key=key).delete()
    db.session.commit()


def locked_usernames() -> set[str]:
    """Usernames that are currently locked (from at least one address)."""
    from sqlalchemy import func
    rows = (db.session.query(LoginAttempt.username, LoginAttempt.key, func.count(LoginAttempt.id))
            .filter(LoginAttempt.created_at >= _since()).group_by(LoginAttempt.username, LoginAttempt.key).all())
    locked = {u for u, _, n in rows if n >= MAX_FAILS}
    day = (db.session.query(LoginAttempt.username, func.count(LoginAttempt.id))
           .filter(LoginAttempt.key.like("2fa|%"), LoginAttempt.created_at >= datetime.now() - timedelta(days=1))
           .group_by(LoginAttempt.username).all())
    return locked | {u for u, n in day if n >= MFA_DAILY_MAX}


def unlock_user(username: str) -> int:
    n: int = LoginAttempt.query.filter_by(username=norm(username)).delete()
    db.session.commit()
    return n


def cleanup() -> int:
    # keep a little more than a day (day boundary for the second factor)
    n: int = LoginAttempt.query.filter(LoginAttempt.created_at < datetime.now() - timedelta(days=1, hours=1)).delete()
    db.session.commit()
    return n
