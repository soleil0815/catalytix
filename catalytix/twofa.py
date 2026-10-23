"""Zwei-Faktor-Anmeldung: TOTP (Authenticator-App), Sicherheitsschlüssel (WebAuthn/FIDO2,
z. B. YubiKey) und Wiederherstellungscodes.

* TOTP nach RFC 6238 (SHA-1, 6 Ziffern, 30 s) – kompatibel mit Google/Microsoft Authenticator,
  Aegis, 2FAS, Bitwarden, Yubico Authenticator usw. Geheimnisse liegen verschlüsselt in der DB.
* WebAuthn über die Bibliothek „fido2“ von Yubico. Funktioniert nur über HTTPS (oder localhost)
  und ist an die Domain gebunden, unter der der Schlüssel registriert wurde.
* Wiederherstellungscodes: 10 Einmal-Codes, nur als Hash gespeichert.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import unicodedata
import json
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from .models import ROLE_ADMIN, ROLE_LIBRARIAN, User, WebAuthnCredential, db, get_setting

TOTP_PERIOD = 30
TOTP_DIGITS = 6


# ---------------------------------------------------------------- Verschlüsselung

def _fernet() -> Fernet:
    key = hashlib.sha256(("totp-secret-key:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


# ---------------------------------------------------------------- TOTP

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at or time.time()) // TOTP_PERIOD))


def check_totp(secret_b32: str, code: str, last_step: int = 0, at: float | None = None, window: int = 1) -> int | None:
    """Prüft einen Code (±window Zeitschritte). Liefert den Zeitschritt oder None.

    Codes eines bereits verwendeten (oder älteren) Zeitschritts werden abgelehnt."""
    # Ziffern aus jeder Schrift (z. B. arabische Tastatur) in 0–9 umwandeln, alles andere ignorieren
    code = "".join(str(unicodedata.decimal(c)) for c in (code or "") if c.isdecimal())
    if len(code) != TOTP_DIGITS or not secret_b32:
        return None
    now = int((at or time.time()) // TOTP_PERIOD)
    for step in range(now - window, now + window + 1):
        if step > last_step and hmac.compare_digest(_hotp(secret_b32, step), code):
            return step
    return None


def otpauth_uri(secret_b32: str, account: str) -> str:
    issuer = get_setting("library_name") or "Bibliothek"
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")


def qr_svg(data: str) -> str:
    import segno
    return segno.make(data, error="m").svg_inline(scale=5, border=2, dark="#1d2421", light="#ffffff")


def totp_unreadable(user: User) -> bool:
    """Authenticator-App eingerichtet, Geheimnis aber nicht mehr lesbar (z. B. nach Schlüsselwechsel)."""
    return bool(user.totp_enabled and user.totp_secret and not decrypt(user.totp_secret))


def verify_user_totp(user: User, code: str) -> bool:
    if not user.totp_enabled:
        return False
    step = check_totp(decrypt(user.totp_secret), code, user.totp_last_step or 0)
    if step is None:
        return False
    # Atomar: Nur wer den Zeitschritt als Erster verbraucht, kommt durch – auch bei gleichzeitigen
    # Anfragen in mehreren Threads/Prozessen mit demselben Code.
    from sqlalchemy import func, update
    res = db.session.execute(
        update(User).where(User.id == user.id, func.coalesce(User.totp_last_step, 0) < step)
        .values(totp_last_step=step).execution_options(synchronize_session=False))
    db.session.commit()
    db.session.refresh(user)
    return res.rowcount == 1


# ---------------------------------------------------------------- Wiederherstellungscodes

def new_recovery_codes(user: User, count: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
             "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(count)]
    user.recovery_codes = json.dumps([generate_password_hash(c) for c in codes])
    db.session.commit()
    return codes


def recovery_codes_left(user: User) -> int:
    try:
        return len(json.loads(user.recovery_codes or "[]"))
    except ValueError:
        return 0


def use_recovery_code(user: User, code: str) -> bool:
    code = (code or "").strip().lower().replace(" ", "")
    if len(code) == 8:
        code = code[:4] + "-" + code[4:]
    from sqlalchemy import update
    for _attempt in range(5):
        old = user.recovery_codes or ""
        try:
            hashes = json.loads(old or "[]")
        except ValueError:
            return False
        match = next((h for h in hashes if check_password_hash(h, code)), None)
        if match is None:
            return False
        hashes.remove(match)
        # Atomar (compare-and-swap): nur übernehmen, wenn die Liste seit dem Lesen unverändert ist –
        # sonst könnte derselbe Code bei gleichzeitigen Anfragen zweimal gelten bzw. ein anderer
        # verbrauchter Code wieder auftauchen.
        res = db.session.execute(
            update(User).where(User.id == user.id, User.recovery_codes == old)
            .values(recovery_codes=json.dumps(hashes)).execution_options(synchronize_session=False))
        db.session.commit()
        db.session.refresh(user)
        if res.rowcount == 1:
            return True
    return False


def reset_2fa(user: User):
    user.totp_enabled = False
    user.totp_secret = ""
    user.totp_last_step = 0
    user.recovery_codes = ""
    WebAuthnCredential.query.filter_by(user_id=user.id).delete()
    user.end_all_sessions()   # wer evtl. noch mit dem verlorenen Gerät angemeldet ist, fliegt raus
    db.session.commit()


# ---------------------------------------------------------------- Richtlinie

def internal_mode() -> str:
    """Regel im internen Netz: same (wie von außen), optional (keine Pflicht), skip (keine Abfrage)."""
    mode = get_setting("mfa_internal")
    return mode if mode in ("same", "optional", "skip") else "same"


def needed_outside(user: User) -> bool:
    """Braucht dieser Benutzer bei Zugriff von außen einen zweiten Faktor?"""
    return bool(user.has_2fa or required_for(user))


def should_challenge(user: User, internal: bool) -> bool:
    """Zweiten Faktor bei der Anmeldung abfragen?"""
    return bool(user.has_2fa) and not (internal and internal_mode() == "skip")


def must_set_up(user: User, internal: bool) -> bool:
    """Muss der Benutzer jetzt einen zweiten Faktor einrichten (Richtlinie)?"""
    if user.has_2fa or not required_for(user):
        return False
    return not (internal and internal_mode() in ("optional", "skip"))


def required_for(user: User) -> bool:
    policy = get_setting("mfa_policy") or "optional"
    if policy == "all":
        return True
    if policy == "librarians":
        return user.role in (ROLE_LIBRARIAN, ROLE_ADMIN)
    if policy == "admins":
        return user.role == ROLE_ADMIN
    return False


# ---------------------------------------------------------------- WebAuthn

def rp_id_for(host: str) -> str:
    return (host or "").split(":")[0].strip("[]").lower()


def _server(rp_id: str):
    from fido2.server import Fido2Server
    from fido2.webauthn import PublicKeyCredentialRpEntity
    return Fido2Server(PublicKeyCredentialRpEntity(id=rp_id, name=get_setting("library_name") or "Bibliothek"))


def _cred_data(cred: WebAuthnCredential):
    from fido2.webauthn import AttestedCredentialData
    return AttestedCredentialData(base64.b64decode(cred.credential_data))


def webauthn_register_begin(user: User, rp_id: str):
    from fido2.webauthn import PublicKeyCredentialUserEntity
    existing = [_cred_data(c) for c in user.webauthn_credentials if c.rp_id == rp_id]
    options, state = _server(rp_id).register_begin(
        PublicKeyCredentialUserEntity(id=f"user-{user.id}".encode(), name=user.username,
                                      display_name=user.name),
        existing, user_verification="discouraged", resident_key_requirement="discouraged")
    return dict(options), state


def webauthn_register_complete(user: User, rp_id: str, state, response: dict, name: str) -> WebAuthnCredential:
    from fido2.utils import websafe_encode
    auth_data = _server(rp_id).register_complete(state, response)
    cd = auth_data.credential_data
    cred = WebAuthnCredential(user_id=user.id, rp_id=rp_id, name=(name or "Sicherheitsschlüssel")[:100],
                              credential_id=websafe_encode(cd.credential_id),
                              credential_data=base64.b64encode(bytes(cd)).decode(),
                              sign_count=auth_data.counter)
    db.session.add(cred)
    db.session.commit()
    return cred


def webauthn_auth_begin(user: User, rp_id: str):
    creds = [_cred_data(c) for c in user.webauthn_credentials if c.rp_id == rp_id]
    if not creds:
        return None, None
    options, state = _server(rp_id).authenticate_begin(creds, user_verification="discouraged")
    return dict(options), state


def webauthn_auth_complete(user: User, rp_id: str, state, response: dict) -> bool:
    from datetime import datetime

    from fido2.utils import websafe_encode
    from fido2.webauthn import AuthenticationResponse

    stored = [c for c in user.webauthn_credentials if c.rp_id == rp_id]
    if not stored:
        return False
    server = _server(rp_id)
    cred_data = server.authenticate_complete(state, [_cred_data(c) for c in stored], response)
    used = next(c for c in stored if c.credential_id == websafe_encode(cred_data.credential_id))
    parsed = AuthenticationResponse.from_dict(response)
    counter = parsed.response.authenticator_data.counter
    # Klon-Erkennung: Zähler muss steigen (0 = Schlüssel ohne Zähler, z. B. manche Passkeys)
    if counter and used.sign_count and counter <= used.sign_count:
        return False
    used.sign_count = counter
    used.last_used_at = datetime.now()
    db.session.commit()
    return True
