"""Zwei-Faktor-Anmeldung: Anmeldeschritt 2 und Verwaltung im Profil."""
from __future__ import annotations

import logging
import time

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user, login_required, login_user

from .. import lockout, twofa
from ..i18n import _
from ..models import User, WebAuthnCredential, db
from . import form_str, safe_next

bp = Blueprint("security", __name__)
log = logging.getLogger(__name__)

PENDING_TTL = 300  # Sekunden für den zweiten Schritt


# ---------------------------------------------------------------- Anmeldung

def start_login(user: User, remember: bool, next_url: str | None):
    """Nach erfolgreicher Passwortprüfung: entweder zweiter Faktor oder direkt anmelden."""
    from datetime import datetime
    from .. import netzone
    session.pop("mfa_pending", None)
    session.pop("mfa_skipped", None)
    session.pop("mfa_ok", None)
    internal = netzone.is_internal()
    if twofa.should_challenge(user, internal):
        session["mfa_pending"] = {"uid": user.id, "remember": bool(remember), "next": next_url or "",
                                  "ts": time.time()}
        return redirect(url_for("security.challenge"))
    # Zweiter Faktor nur wegen „internes Netz“ übersprungen? Dann gilt die Sitzung nur intern:
    # kein „Angemeldet bleiben“, und von außen ist eine neue Anmeldung nötig.
    skipped = internal and twofa.needed_outside(user)
    if skipped:
        remember = False
    user.last_login = datetime.now()
    db.session.commit()
    login_user(user, remember=remember)
    if skipped:
        session["mfa_skipped"] = True
    if twofa.must_set_up(user, internal):
        session["mfa_setup"] = True
        flash(_("Bitte richte jetzt einen zweiten Faktor ein (Authenticator-App oder Sicherheitsschlüssel)."), "warning")
        return redirect(url_for("security.settings"))
    return redirect(safe_next(next_url) or url_for("main.dashboard"))


def _pending_user() -> User | None:
    p = session.get("mfa_pending")
    if not p or time.time() - p.get("ts", 0) > PENDING_TTL:
        session.pop("mfa_pending", None)
        return None
    user = db.session.get(User, p["uid"])
    return user if user is not None and user.active else None


def _finish(user: User):
    from datetime import datetime
    p = session.pop("mfa_pending", {})
    session.pop("webauthn_state", None)
    session.pop("mfa_skipped", None)
    user.last_login = datetime.now()
    db.session.commit()
    login_user(user, remember=p.get("remember", False))
    session["mfa_ok"] = True   # zweiter Faktor in dieser Anmeldung bestätigt (für die Verwaltung der Faktoren)
    return safe_next(p.get("next")) or url_for("main.dashboard")


MFA_MAX_FAILS = 10   # Fehlversuche beim zweiten Faktor je Anmeldung, dann Passwort erneut nötig


def _account_throttled(user: User) -> bool:
    """Konto-weite Bremse (viele Fehlversuche von vielen Adressen): nur abweisen, die offene Anmeldung
    bzw. laufende Sitzung bleibt bestehen – sonst könnte ein Angreifer sie durch Fehlversuche beenden."""
    from .. import netzone
    return (lockout.user_throttled(user.username, internal=netzone.is_internal()) or lockout.ip_throttled()
            or lockout.mfa_daily_exceeded(user.username))


def _too_many_mfa_fails(user: User) -> bool:
    """Zu viele falsche Codes für diese (noch offene) Anmeldung oder für das Konto insgesamt?

    Gezählt wird in der Datenbank – ein zurückgespieltes altes Sitzungs-Cookie hilft also nicht.
    Bei Überschreitung wird die offene Anmeldung verworfen."""
    p = session.get("mfa_pending") or {}
    if lockout.mfa_fails_since(user.username, p.get("ts", 0)) >= MFA_MAX_FAILS:
        session.pop("mfa_pending", None)
        session.pop("webauthn_state", None)
        if current_user.is_authenticated:
            # Nachträgliche Bestätigung (stepup) aus einer laufenden Sitzung: die Sitzung endet auch,
            # sonst ließe sich der Zähler durch erneutes Starten immer wieder zurücksetzen.
            from flask_login import logout_user
            session.clear()
            logout_user()
        log.warning("Too many failed second-factor attempts for %s – sign-in discarded", user.username)
        return True
    return False


def _fail_key(user: User) -> str:
    return f"2fa|{user.username.lower()}|{request.remote_addr}"


@bp.route("/login/2fa", methods=["GET", "POST"])
def challenge():
    user = _pending_user()
    if user is None:
        flash(_("Die Anmeldung ist abgelaufen. Bitte erneut anmelden."), "info")
        return redirect(url_for("main.login"))
    key = _fail_key(user)
    mode = request.values.get("modus", "")
    if request.method == "POST":
        if _too_many_mfa_fails(user):
            flash(_("Zu viele Fehlversuche beim zweiten Faktor. Bitte melde dich erneut an."), "error")
            return redirect(url_for("main.login"))
        if lockout.throttled(key) or _account_throttled(user):
            flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
            return render_template("security/challenge.html", user=user, mode=mode), 429
        code = form_str("code", 40)
        ok = False
        if mode == "wiederherstellung":
            ok = twofa.use_recovery_code(user, code)
            if ok:
                left = twofa.recovery_codes_left(user)
                flash(_("Mit Wiederherstellungscode angemeldet. Noch {n} Code(s) übrig.", n=left), "warning")
        else:
            ok = twofa.verify_user_totp(user, code)
        if ok:
            lockout.clear(key)
            return redirect(_finish(user))
        lockout.fail(key, user.username)
        log.info("2FA failed for %s", user.username)
        flash(_("Der Code ist ungültig."), "error")
    return render_template("security/challenge.html", user=user, mode=mode,
                           totp_broken=twofa.totp_unreadable(user),
                           has_keys=any(c.rp_id == twofa.rp_id_for(request.host) for c in user.webauthn_credentials))


@bp.route("/login/2fa/schluessel/start", methods=["POST"])
def challenge_key_begin():
    user = _pending_user()
    if user is None:
        return jsonify({"ok": False, "error": _("Anmeldung abgelaufen.")}), 400
    options, state = twofa.webauthn_auth_begin(user, twofa.rp_id_for(request.host))
    if options is None:
        return jsonify({"ok": False, "error": _("Für diese Adresse ist kein Sicherheitsschlüssel registriert.")}), 400
    session["webauthn_state"] = state
    return jsonify({"ok": True, "options": options})


@bp.route("/login/2fa/schluessel/pruefen", methods=["POST"])
def challenge_key_complete():
    user = _pending_user()
    state = session.pop("webauthn_state", None)
    if user is None or state is None:
        return jsonify({"ok": False, "error": _("Anmeldung abgelaufen.")}), 400
    key = _fail_key(user)
    if _too_many_mfa_fails(user):
        flash(_("Zu viele Fehlversuche beim zweiten Faktor. Bitte melde dich erneut an."), "error")
        return jsonify({"ok": False, "error": _("Zu viele Fehlversuche beim zweiten Faktor. Bitte melde dich erneut an."),
                        "redirect": url_for("main.login")}), 429
    if lockout.throttled(key) or _account_throttled(user):
        return jsonify({"ok": False, "error": _("Zu viele Fehlversuche.")}), 429
    try:
        ok = twofa.webauthn_auth_complete(user, twofa.rp_id_for(request.host), state, request.get_json(force=True))
    except Exception as exc:  # noqa: BLE001
        log.info("WebAuthn sign-in %s failed: %s", user.username, exc)
        ok = False
    if not ok:
        lockout.fail(key, user.username)
        return jsonify({"ok": False, "error": _("Sicherheitsschlüssel wurde nicht akzeptiert.")}), 400
    lockout.clear(key)
    return jsonify({"ok": True, "redirect": _finish(user)})


# ---------------------------------------------------------------- Profil: Sicherheit

def _render_settings(**extra):
    rp = twofa.rp_id_for(request.host)
    return render_template("security/settings.html", rp_id=rp, required=twofa.required_for(current_user),
                           totp_broken=twofa.totp_unreadable(current_user),
                           codes_left=twofa.recovery_codes_left(current_user),
                           keys=sorted(current_user.webauthn_credentials, key=lambda c: c.created_at or 0),
                           stepup_needed=not _may_manage(), **extra)


def _may_manage() -> bool:
    """Faktoren hinzufügen, ersetzen, entfernen oder neue Codes erzeugen darf nur, wer in dieser Anmeldung
    einen zweiten Faktor bestätigt hat – oder wer noch gar keinen hat (Ersteinrichtung).

    Sonst könnte eine Sitzung, die im internen Netz ohne zweiten Faktor begonnen wurde, sich einen eigenen
    Faktor anlegen und damit von außen anmelden."""
    return bool(session.get("mfa_ok")) or not current_user.has_2fa


def _stepup_msg() -> str:
    return _("Bitte bestätige zuerst deinen zweiten Faktor (Authenticator-App, Sicherheitsschlüssel oder "
             "Wiederherstellungscode) – du hast dich in dieser Sitzung ohne ihn angemeldet.")


def _refuse_manage():
    flash(_stepup_msg(), "warning")
    return redirect(url_for("security.settings"))


@bp.route("/profil/sicherheit/bestaetigen", methods=["POST"])
@login_required
def stepup():
    """Zweiten Faktor für die laufende Sitzung nachträglich abfragen (z. B. nach Anmeldung im internen Netz
    ohne zweiten Faktor). Nutzt den normalen Anmeldeschritt 2."""
    if not current_user.has_2fa or session.get("mfa_ok"):
        return redirect(url_for("security.settings"))
    session["mfa_pending"] = {"uid": current_user.id, "remember": False, "next": url_for("security.settings"),
                              "ts": time.time()}
    return redirect(url_for("security.challenge"))


def _last_factor(kind: str, cred_id: int | None = None) -> bool:
    """Würde das Entfernen den letzten Faktor löschen, obwohl 2FA Pflicht ist?"""
    if not twofa.required_for(current_user):
        return False
    keys = [c for c in current_user.webauthn_credentials if c.id != cred_id]
    totp = current_user.totp_enabled and kind != "totp"
    return not keys and not totp


@bp.route("/profil/sicherheit")
@login_required
def settings():
    return _render_settings()


@bp.route("/profil/sicherheit/app/einrichten", methods=["POST"])
@login_required
def totp_start():
    if not _may_manage():
        return _refuse_manage()
    secret = twofa.new_totp_secret()
    session["totp_setup"] = twofa.encrypt(secret)
    uri = twofa.otpauth_uri(secret, current_user.username)
    return _render_settings(setup_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                            setup_qr=twofa.qr_svg(uri), setup_uri=uri)


@bp.route("/profil/sicherheit/app/bestaetigen", methods=["POST"])
@login_required
def totp_confirm():
    if not _may_manage():
        return _refuse_manage()
    enc = session.get("totp_setup")
    secret = twofa.decrypt(enc) if enc else ""
    if not secret:
        flash(_("Die Einrichtung ist abgelaufen. Bitte neu starten."), "error")
        return redirect(url_for("security.settings"))
    step = twofa.check_totp(secret, form_str("code", 20))
    if step is None:
        flash(_("Der Code stimmt nicht. Uhrzeit von Handy und Server prüfen und den aktuellen Code eingeben."), "error")
        uri = twofa.otpauth_uri(secret, current_user.username)
        return _render_settings(setup_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                                setup_qr=twofa.qr_svg(uri), setup_uri=uri)
    session.pop("totp_setup", None)
    current_user.totp_secret = twofa.encrypt(secret)
    current_user.totp_enabled = True
    current_user.totp_last_step = step
    db.session.commit()
    session.pop("mfa_setup", None)
    session["mfa_ok"] = True   # den neuen Faktor hat er gerade bestätigt
    codes = twofa.new_recovery_codes(current_user) if not twofa.recovery_codes_left(current_user) else None
    flash(_("Authenticator-App ist eingerichtet."), "success")
    return _render_settings(new_codes=codes)


@bp.route("/profil/sicherheit/app/entfernen", methods=["POST"])
@login_required
def totp_disable():
    if not _may_manage():
        return _refuse_manage()
    if _last_factor("totp"):
        flash(_("Ein zweiter Faktor ist Pflicht. Richte zuerst einen Sicherheitsschlüssel ein."), "error")
    elif not twofa.totp_unreadable(current_user) and not twofa.verify_user_totp(current_user, form_str("code", 20)):
        flash(_("Zum Entfernen bitte einen aktuellen Code aus der App eingeben."), "error")
    else:
        current_user.totp_enabled = False
        current_user.totp_secret = ""
        db.session.commit()
        flash(_("Authenticator-App entfernt."), "success")
    return redirect(url_for("security.settings"))


@bp.route("/profil/sicherheit/codes", methods=["POST"])
@login_required
def recovery_regenerate():
    if not current_user.has_2fa:
        abort(400)
    if not _may_manage():
        return _refuse_manage()
    codes = twofa.new_recovery_codes(current_user)
    flash(_("Neue Wiederherstellungscodes erzeugt – die alten sind ungültig."), "success")
    return _render_settings(new_codes=codes)


@bp.route("/profil/sicherheit/schluessel/start", methods=["POST"])
@login_required
def key_begin():
    if not _may_manage():
        return jsonify({"ok": False, "error": _stepup_msg()}), 403
    options, state = twofa.webauthn_register_begin(current_user, twofa.rp_id_for(request.host))
    session["webauthn_reg_state"] = state
    return jsonify({"ok": True, "options": options})


@bp.route("/profil/sicherheit/schluessel/speichern", methods=["POST"])
@login_required
def key_complete():
    if not _may_manage():
        session.pop("webauthn_reg_state", None)
        return jsonify({"ok": False, "error": _stepup_msg()}), 403
    state = session.pop("webauthn_reg_state", None)
    payload = request.get_json(force=True) or {}
    if state is None:
        return jsonify({"ok": False, "error": _("Registrierung abgelaufen.")}), 400
    try:
        twofa.webauthn_register_complete(current_user, twofa.rp_id_for(request.host), state,
                                         payload.get("credential") or {}, str(payload.get("name") or ""))
    except Exception as exc:  # noqa: BLE001
        log.info("WebAuthn registration failed: %s", exc)
        return jsonify({"ok": False, "error": _("Schlüssel konnte nicht registriert werden ({error}).", error=str(exc))}), 400
    session.pop("mfa_setup", None)
    session["mfa_ok"] = True   # der neue Schlüssel wurde gerade benutzt
    first = not twofa.recovery_codes_left(current_user)
    if first:
        # nur verschlüsselt ins (für den Browser lesbare) Sitzungs-Cookie
        import json
        session["show_new_codes"] = twofa.encrypt(json.dumps(twofa.new_recovery_codes(current_user)))
    flash(_("Sicherheitsschlüssel hinzugefügt."), "success")
    return jsonify({"ok": True, "redirect": url_for("security.settings", neu="1" if first else None)})


@bp.route("/profil/sicherheit/schluessel/<int:cred_id>/entfernen", methods=["POST"])
@login_required
def key_delete(cred_id):
    cred = db.session.get(WebAuthnCredential, cred_id)
    if cred is None or cred.user_id != current_user.id:
        abort(404)
    if not _may_manage():
        return _refuse_manage()
    if _last_factor("key", cred.id):
        flash(_("Ein zweiter Faktor ist Pflicht. Das ist dein letzter – richte zuerst einen anderen ein."), "error")
    else:
        db.session.delete(cred)
        db.session.commit()
        flash(_("„{name}“ entfernt.", name=cred.name if cred.name and cred.name != "Sicherheitsschlüssel"
                                     else _("Sicherheitsschlüssel")), "success")
    return redirect(url_for("security.settings"))


@bp.before_app_request
def enforce_setup():
    """Wer laut Richtlinie 2FA braucht, muss sie einrichten, bevor er weiterarbeiten kann.

    Außerdem: Eine Sitzung, die im internen Netz ohne zweiten Faktor begonnen wurde, gilt nicht von außen."""
    from flask_login import logout_user
    from .. import netzone
    if not current_user.is_authenticated or request.endpoint == "static":
        return None
    internal = netzone.is_internal()
    if session.get("mfa_skipped") and not internal and twofa.needed_outside(current_user):
        log.info("Session of %s without second factor (started internally) used from outside – sign in again",
                 current_user.username)
        session.clear()     # zuerst – logout_user() merkt sich darin, dass das Merk-Cookie gelöscht wird
        logout_user()
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": _("Bitte neu anmelden.")}), 401
        flash(_("Für den Zugriff von außen ist eine neue Anmeldung mit zweitem Faktor nötig."), "warning")
        return redirect(url_for("main.login", next=request.full_path if request.method == "GET" else None))
    if not twofa.must_set_up(current_user, internal):
        return None
    allowed = {"static", "main.logout", "main.set_language", "help.index", "security.settings", "security.totp_start", "security.totp_confirm",
               "security.key_begin", "security.key_complete"}
    if request.endpoint in allowed:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": _("Zweiter Faktor erforderlich.")}), 403
    return redirect(url_for("security.settings"))


@bp.app_context_processor
def inject_codes():
    from flask import has_request_context
    if not has_request_context():  # z. B. Mail-Vorlagen im Hintergrund
        return {}
    enc = session.pop("show_new_codes", None) if request.args.get("neu") else None
    codes = None
    if enc:
        import json
        try:
            codes = json.loads(twofa.decrypt(enc) or "null")
        except ValueError:
            codes = None
    return {"pending_codes": codes} if codes else {}
