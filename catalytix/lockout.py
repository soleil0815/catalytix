"""Anmeldesperre nach Fehlversuchen – in der Datenbank, damit sie für alle Server-Prozesse gilt
und ein Administrator sie in der Oberfläche aufheben kann.

8 Fehlversuche je Benutzername und Adresse innerhalb von 5 Minuten sperren diese Kombination für
5 Minuten (Passwort und zweiter Faktor werden getrennt gezählt).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

from .models import LoginAttempt, db

MAX_FAILS = 8
WINDOW = timedelta(minutes=5)


def norm(username: str) -> str:
    """Benutzername für Sperrschlüssel: wie bei der AD-Anmeldung ohne „DOMÄNE\\“ und „@…“, klein geschrieben.

    Sonst ergäben „DOMÄNE\\anna“, „anna@x“ und „ANNA“ jeweils eine eigene, frische Sperre."""
    name = (username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    # NFKC + casefold: „ａｎｎａ“ (Vollbreite) oder „ANNA“ zählen wie „anna“ – das AD vergleicht ähnlich großzügig
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _since() -> datetime:
    return datetime.now() - WINDOW


def throttled(key: str) -> bool:
    return LoginAttempt.query.filter(LoginAttempt.key == key, LoginAttempt.created_at >= _since()).count() >= MAX_FAILS


def _origin() -> tuple[str, bool]:
    """Adresse und „intern?“ der laufenden Anfrage (außerhalb einer Anfrage: leer)."""
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
    """Zusätzliche Bremse je Benutzername über alle Adressen (gegen Raten mit wechselnden IPs).

    Im internen Netz zählen nur Fehlversuche aus dem internen Netz (höhere Grenze): Wer von außen
    absichtlich falsche Passwörter eingibt, sperrt das Konto nur für Zugriffe von außen – im Hausnetz
    kann man sich weiter anmelden."""
    since = datetime.now() - USER_WINDOW
    q = LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.created_at >= since)
    if internal:
        return q.filter(LoginAttempt.internal.is_(True)).count() >= USER_MAX_FAILS_INTERNAL
    return q.count() >= USER_MAX_FAILS


IP_MAX_FAILS = 30          # Fehlversuche je Adresse von außen über alle Konten (gegen „ein Passwort für alle“)


def ip_throttled() -> bool:
    ip, internal = _origin()
    if not ip or internal:
        return False
    since = datetime.now() - USER_WINDOW
    return LoginAttempt.query.filter(LoginAttempt.ip == ip, LoginAttempt.created_at >= since).count() >= IP_MAX_FAILS


MFA_DAILY_MAX = 30         # falsche Codes beim zweiten Faktor je Konto und Tag – dann Admin oder morgen


def mfa_daily_exceeded(username: str) -> bool:
    since = datetime.now() - timedelta(days=1)
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                     LoginAttempt.created_at >= since).count() >= MFA_DAILY_MAX


def mfa_fails_since(username: str, ts: float) -> int:
    """Fehlversuche beim zweiten Faktor für diesen Benutzer seit dem Zeitpunkt ts (Unix-Zeit)."""
    since = max(datetime.fromtimestamp(ts or 0), datetime.now() - USER_WINDOW)
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                     LoginAttempt.created_at >= since).count()


def clear(key: str) -> None:
    LoginAttempt.query.filter_by(key=key).delete()
    db.session.commit()


def locked_usernames() -> set[str]:
    """Benutzernamen, die gerade (von mindestens einer Adresse aus) gesperrt sind."""
    from sqlalchemy import func
    rows = (db.session.query(LoginAttempt.username, LoginAttempt.key, func.count(LoginAttempt.id))
            .filter(LoginAttempt.created_at >= _since()).group_by(LoginAttempt.username, LoginAttempt.key).all())
    locked = {u for u, _, n in rows if n >= MAX_FAILS}
    day = (db.session.query(LoginAttempt.username, func.count(LoginAttempt.id))
           .filter(LoginAttempt.key.like("2fa|%"), LoginAttempt.created_at >= datetime.now() - timedelta(days=1))
           .group_by(LoginAttempt.username).all())
    return locked | {u for u, n in day if n >= MFA_DAILY_MAX}


def unlock_user(username: str) -> int:
    n = LoginAttempt.query.filter_by(username=norm(username)).delete()
    db.session.commit()
    return n


def cleanup() -> int:
    # etwas mehr als einen Tag aufheben (Tagesgrenze beim zweiten Faktor)
    n = LoginAttempt.query.filter(LoginAttempt.created_at < datetime.now() - timedelta(days=1, hours=1)).delete()
    db.session.commit()
    return n
