"""Anmeldung, Ersteinrichtung, Übersicht, Profil."""
from __future__ import annotations

import logging
from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func

from ..ldap_auth import LDAPError, authenticate, ldap_enabled, normalize_dn, rule_matches
from .. import lockout, news
from ..i18n import _, get_locale
from ..models import (MIN_PASSWORD_LENGTH, ROLE_ADMIN, Book, Group, Loan, User, db, get_bool, get_setting,
                      manageable_books_query, set_setting, visible_books_query)
from . import form_str, safe_next

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

# Einfacher Schutz gegen Passwort-Raten: 8 Fehlversuche pro Benutzer/IP -> 5 Minuten Sperre
# Schutz gegen Passwort-Raten: siehe lockout.py (Sperre in der Datenbank)


class AccountMismatch(Exception):
    """Das Konto gehört laut fester AD-Kennung einer anderen Person – nicht überschreiben."""


def same_person(user: User, info) -> bool:
    """Gehört das Konto zu dieser AD-Person? Ohne gespeicherte Kennung (ältere Konten, OpenLDAP) zählt der Name."""
    return not (user.ldap_guid and info.guid and user.ldap_guid != info.guid)


def account_for(info, directory=None) -> User | None:
    """Konto zu einer AD-Person – über die feste AD-Kennung (findet es auch nach einer Umbenennung im AD), sonst
    über den Benutzernamen.

    Gehört ein gleichnamiges AD-Konto einer anderen Person, wird im AD nachgesehen: Wurde sie nur umbenannt, zieht
    ihr Konto auf den neuen Namen um; gibt es sie nicht mehr, wird es unter einem Ersatznamen stillgelegt. Erst dann
    ist der Name frei – niemand übernimmt fremde Ausleihen. Ist das nicht klärbar (AD nicht erreichbar, neuer Name
    der bisherigen Person schon vergeben), wird AccountMismatch ausgelöst – es wird dann nichts angelegt."""
    if info.guid:
        user = User.query.filter_by(ldap_guid=info.guid).first()
        if user is not None:
            return user
    user = User.query.filter(func.lower(User.username) == (info.username or "").lower()).first()
    if user is None or user.auth_source != "ldap" or same_person(user, info):
        return user
    # gleicher Name, andere Person
    from ..ldap_auth import Directory, LDAPError
    directory = directory or Directory()
    try:
        other = directory.find_by_guid(user.ldap_guid)
    except LDAPError as exc:
        log.warning("Account %s: previous owner cannot be checked in AD (%s) – nothing changed", user.username, exc)
        raise AccountMismatch(user.username) from exc
    if other is not None and other.username:
        # Die bisherige Person gibt es noch, nur unter anderem Namen: ihr Konto zieht um
        sync_ldap_user(other, user, active_check=False)
        db.session.flush()
        if user.username.lower() == (info.username or "").lower():
            # Neuer Name noch belegt – von einem stillgelegten Konto einer ausgeschiedenen Person? Dann freigeben.
            blocker = User.query.filter(func.lower(User.username) == other.username.lower(),
                                        User.id != user.id).first()
            if (blocker is not None and blocker.auth_source == "ldap" and not blocker.active
                    and blocker.ldap_guid and blocker.ldap_guid != other.guid):
                try:
                    still_there = directory.find_by_guid(blocker.ldap_guid)
                except LDAPError as exc:
                    raise AccountMismatch(user.username) from exc
                if still_there is None:
                    retire_account(blocker)
                    sync_ldap_user(other, user, active_check=False)
                    db.session.flush()
        if user.username.lower() == (info.username or "").lower():
            raise AccountMismatch(user.username)   # Umzug nicht möglich – Name bleibt belegt
        return None
    retire_account(user)
    return None


def retire_account(user: User) -> None:
    """AD-Konto einer ausgeschiedenen Person stilllegen und den Namen freigeben (Ausleihen bleiben erhalten).
    Ein von Hand gesperrtes Konto bleibt von Hand gesperrt (wird später nicht automatisch freigegeben)."""
    from datetime import datetime
    old = user.username
    suffix = f"~{user.id}"
    name = old[:120 - len(suffix)] + suffix
    n = 1
    while User.query.filter(func.lower(User.username) == name.lower(), User.id != user.id).first() is not None:
        n += 1
        suffix = f"~{user.id}-{n}"
        name = old[:120 - len(suffix)] + suffix
    user.username = name
    if user.active:
        user.ldap_gone_at = user.ldap_gone_at or datetime.now()
    user.active = False
    user.auth_epoch = (user.auth_epoch or 0) + 1
    db.session.flush()
    log.warning("AD account %s belongs to a different person than before (new AD identifier) – old account "
                "retired as %s", old, user.username)


def sync_ldap_user(info, existing: User | None, ldap_groups: list | None = None, rename: bool = True,
                   active_check: bool = True, group_sync: bool | None = None) -> User:
    """AD-Daten in ein Konto übernehmen. active_check=False: nur Name/Angaben nachziehen (Person existiert, ist aber
    evtl. nicht mehr berechtigt) – Rolle, Gruppen und Sperre bleiben dann unberührt."""
    user = existing
    if user is not None and not same_person(user, info):
        raise AccountMismatch(user.username)
    if user is None:
        user = User(username=info.username, auth_source="ldap")
        db.session.add(user)
    elif (rename and user.auth_source == "ldap" and info.username
          and user.username.lower() != info.username.lower()):
        # im AD umbenannt: Konto (mit allen Ausleihen) zieht mit, sofern der neue Name frei ist
        taken = User.query.filter(func.lower(User.username) == info.username.lower(), User.id != user.id).first()
        if taken is None:
            log.info("AD account renamed: %s → %s", user.username, info.username)
            user.username = info.username
        else:
            log.warning("AD account %s is now called %s in AD – that name is already taken in Catalytix, "
                        "rename skipped", user.username, info.username)
    user.auth_source = "ldap"
    user.password_hash = None
    user.display_name = info.display_name
    user.email = info.email
    user.ldap_dn = info.dn
    if info.guid and not user.ldap_guid:
        user.ldap_guid = info.guid          # nie eine andere Kennung überschreiben (siehe same_person)
    if not active_check:
        return user
    user.role = info.role
    if user.ldap_gone_at is not None and info.role is not None:
        # vom Abgleich gesperrt, im AD aber wieder aktiv und berechtigt → wieder freigeben
        user.active = True
        user.ldap_gone_at = None
    if get_bool("ldap_group_sync") if group_sync is None else group_sync:
        user_groups = {normalize_dn(g) for g in info.groups}
        if ldap_groups is None:
            ldap_groups = Group.query.filter(Group.ldap_dn != "").all()
        for group in ldap_groups:
            member = rule_matches(group.ldap_dn, "", user_groups)
            if member and group not in user.groups:
                user.groups.append(group)
            elif not member and group in user.groups:
                user.groups.remove(group)
    return user


_DUMMY_HASH = None


def _dummy_password_check(password: str) -> None:
    global _DUMMY_HASH
    from werkzeug.security import check_password_hash, generate_password_hash
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("timing-equaliser")
    check_password_hash(_DUMMY_HASH, password or "")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = form_str("username", 200)
        password = request.form.get("password") or ""
        # Sperrschlüssel aus dem normalisierten Namen: „DOMÄNE\\anna“ und „anna@x“ zählen wie „anna“
        key = f"{lockout.norm(username)}|{request.remote_addr}"
        from .. import netzone
        if (lockout.throttled(key) or lockout.user_throttled(username, internal=netzone.is_internal())
                or lockout.ip_throttled()):
            flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
            return render_template("login.html", username=username), 429

        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        error = _("Benutzername oder Passwort ist falsch.")
        ok_user = None
        mismatch = False      # Passwort stimmte, aber das Konto ist nicht klärbar – kein Fehlversuch

        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                ok_user = user
        elif not ldap_enabled():
            # Gleich lange rechnen wie bei einem echten Konto – sonst verrät die Antwortzeit,
            # ob es den Benutzernamen gibt.
            _dummy_password_check(password)
        elif ldap_enabled() and username and password:
            try:
                info = authenticate(username, password)
                if info.role is None:
                    # Bewusst dieselbe Meldung wie bei falschem Passwort: von außen soll nicht
                    # erkennbar sein, ob ein Konto existiert oder das Passwort stimmte.
                    log.info("LDAP sign-in %s refused: no rule matches (%s)", username, info.dn)
                else:
                    if user is None or user.auth_source == "ldap":
                        # über die feste AD-Kennung: auch nach einer Umbenennung im AD dasselbe Konto
                        user = account_for(info)
                        db.session.commit()
                    if user is not None and user.auth_source != "ldap":
                        # Ein lokales Konto wird nie per AD-Anmeldung übernommen (z. B. „DOMÄNE\\kasse“
                        # für das lokale Konto „kasse“) – gleiche Meldung wie bei falschem Passwort.
                        log.warning("AD sign-in as %s refused: local account %s with the same name "
                                    "is never taken over", username, user.username)
                    else:
                        ok_user = sync_ldap_user(info, user)
                        db.session.commit()
            except AccountMismatch as exc:
                db.session.rollback()
                mismatch = True
                log.warning("LDAP sign-in %s: account %s belongs to a different person by AD identifier and "
                            "cannot be moved – sign-in refused", username, exc)
                error = _("Anmeldung derzeit nicht möglich. Bitte versuche es später erneut.")
            except LDAPError as exc:
                log.warning("LDAP sign-in %s failed: %s", username, exc)
            except Exception:  # noqa: BLE001 – niemals Details an den Client geben
                log.exception("Unexpected error during LDAP sign-in")
                error = _("Anmeldung derzeit nicht möglich. Bitte versuche es später erneut.")

        # Selbst registrierte Konten: Passwort stimmt, aber noch nicht bestätigt/freigegeben.
        # Diese Hinweise sieht nur, wer das richtige Passwort kennt.
        if ok_user is not None and ok_user.self_registered and not ok_user.email_verified:
            session["resend_uid"] = ok_user.id
            return render_template("login.html", username=username, unverified=True), 403
        if ok_user is not None and ok_user.pending_approval:
            flash(_("Dein Konto wartet noch auf die Freigabe durch die Bibliothek."), "info")
            return render_template("login.html", username=username), 403

        if ok_user is not None and not ok_user.active:
            log.info("Sign-in of locked account %s refused", ok_user.username)
            ok_user = None

        if ok_user is None:
            if not mismatch:
                lockout.fail(key, username)
            flash(error, "error")
            return render_template("login.html", username=username), 401

        lockout.clear(key)
        if ok_user.auth_source == "ldap":
            import time
            session["ldap_ok_at"] = int(time.time())   # gerade gegen das AD geprüft
        from .security import start_login  # zweiter Faktor, falls eingerichtet
        return start_login(ok_user, bool(request.form.get("remember")), request.args.get("next"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    for k in ("mfa_ok", "mfa_skipped", "mfa_setup", "mfa_pending", "show_new_codes", "totp_setup",
              "webauthn_state", "webauthn_reg_state", "ldap_ok_at"):
        session.pop(k, None)
    logout_user()
    flash(_("Du wurdest abgemeldet."), "info")
    return redirect(url_for("main.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role=ROLE_ADMIN).first() is not None:
        return redirect(url_for("main.login"))
    if get_setting("setup_completed") == "1" or User.query.first() is not None:
        # Einmal eingerichtet: Auch ohne Administrator (z. B. versehentlich gelöscht) darf hier niemand
        # über das Web ein neues Admin-Konto anlegen.
        return render_template(
            "error.html", code=403, title=_("Einrichtung bereits abgeschlossen"),
            text=_("Es gibt kein Administratorkonto mehr. Lege auf dem Server eines an: "
                   "docker compose exec catalytix docker-entrypoint.sh flask --app wsgi create-admin NAME")), 403
    from .. import setupcode
    setupcode.ensure()
    if request.method == "POST":
        username = form_str("username", 120)
        password = request.form.get("password") or ""
        key = f"setup|{request.remote_addr}"
        if lockout.throttled(key):
            flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
            return render_template("setup.html"), 429
        if not setupcode.check(request.form.get("setup_code", "")):
            lockout.fail(key, "setup")
            flash(_("Der Einrichtungscode stimmt nicht. Er steht im Protokoll des Containers (siehe Hinweis)."), "error")
            return render_template("setup.html"), 403
        if not username or len(password) < MIN_PASSWORD_LENGTH:
            flash(_("Benutzername und ein Passwort mit mindestens {n} Zeichen sind nötig.", n=MIN_PASSWORD_LENGTH), "error")
        elif password != request.form.get("password2"):
            flash(_("Die Passwörter stimmen nicht überein."), "error")
        else:
            admin = User(username=username, display_name=form_str("display_name", 200) or username,
                         email=form_str("email", 200), role=ROLE_ADMIN, auth_source="local")
            admin.set_password(password)
            db.session.add(admin)
            set_setting("library_name", form_str("library_name", 120) or "Bibliothek")
            set_setting("setup_completed", "1")
            db.session.commit()
            setupcode.remove()
            lockout.clear(key)
            login_user(admin)
            flash(_("Willkommen! Die Bibliothek ist eingerichtet."), "success")
            return redirect(url_for("main.dashboard"))
    return render_template("setup.html")


@bp.route("/")
@login_required
def dashboard():
    today = date.today()
    my_loans = (Loan.query.filter_by(user_id=current_user.id, returned_at=None)
                .order_by(Loan.due_at).all())
    newest = visible_books_query(current_user).order_by(Book.created_at.desc()).limit(8).all()
    stats, overdue, recent_loans = None, [], []
    if current_user.is_librarian:
        books_q = manageable_books_query(current_user)
        book_ids = books_q.with_entities(Book.id)
        active_q = Loan.query.filter(Loan.returned_at.is_(None), Loan.book_id.in_(book_ids))
        stats = {
            "titles": books_q.count(),
            "copies": books_q.with_entities(func.coalesce(func.sum(Book.copies), 0)).scalar(),
            "active": active_q.count(),
            "overdue": active_q.filter(Loan.due_at < today).count(),
            "users": User.query.filter_by(active=True).count(),
            "groups": Group.query.count(),
        }
        overdue = active_q.filter(Loan.due_at < today).order_by(Loan.due_at).limit(10).all()
        recent_loans = (Loan.query.filter(Loan.book_id.in_(book_ids))
                        .order_by(Loan.loaned_at.desc()).limit(8).all())
    fresh = news.unseen(current_user, get_locale())
    news_hint = None
    if fresh:
        # Ein Sicherheitsupdate geht immer vor – auch wenn es aus einer älteren, noch nicht gesehenen Version stammt
        pairs = [(e["version"], i) for e in fresh for i in e["items"]]
        version, first = next(((v, i) for v, i in pairs if i["kind"] == "security"), pairs[0])
        news_hint = {"version": version, "first": first, "more": len(pairs) - 1}
    return render_template("dashboard.html", my_loans=my_loans, newest=newest, stats=stats,
                           overdue=overdue, recent_loans=recent_loans, news_hint=news_hint)


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST" and request.form.get("action") == "notify":
        current_user.email_notifications = bool(request.form.get("email_notifications"))
        if current_user.auth_source == "local":
            from ..mailer import valid_email
            mail = form_str("email", 200)
            if mail and not valid_email(mail):
                flash(_("Bitte eine gültige E-Mail-Adresse eingeben."), "error")
                return redirect(url_for("main.profile"))
            changed = bool(mail) and mail.lower() != (current_user.email or "").lower()
            if changed and User.query.filter(func.lower(User.email) == mail.lower(),
                                             User.id != current_user.id).first() is not None:
                flash(_("Diese E-Mail-Adresse wird bereits von einem anderen Konto verwendet."), "error")
                return redirect(url_for("main.profile"))
            if current_user.self_registered and changed:
                from .register import _allowed_domain, needs_verification, send_email_change
                if not _allowed_domain(mail):
                    # gleiche Regel wie bei der Registrierung
                    flash(_("Diese E-Mail-Adresse ist für dein Konto nicht erlaubt."), "error")
                    return redirect(url_for("main.profile"))
                if needs_verification():
                    # wie bei der Registrierung: erst nach Bestätigung über die neue Adresse
                    db.session.commit()
                    if send_email_change(current_user._get_current_object(), mail):
                        flash(_("Wir haben einen Bestätigungslink an {email} geschickt. Die neue Adresse gilt, "
                                "sobald du ihn öffnest.", email=mail), "info")
                    else:
                        flash(_("Die Bestätigungsmail konnte nicht gesendet werden – die Adresse bleibt unverändert."),
                              "error")
                    return redirect(url_for("main.profile"))
            current_user.email = mail
        db.session.commit()
        flash(_("Benachrichtigungen gespeichert."), "success")
        return redirect(url_for("main.profile"))
    if request.method == "POST":
        if current_user.auth_source != "local":
            abort(400)
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        if not current_user.check_password(current):
            flash(_("Das aktuelle Passwort ist falsch."), "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(_("Das neue Passwort muss mindestens {n} Zeichen haben.", n=MIN_PASSWORD_LENGTH), "error")
        elif new != request.form.get("new2"):
            flash(_("Die neuen Passwörter stimmen nicht überein."), "error")
        else:
            current_user.set_password(new)   # meldet alle anderen Geräte ab …
            db.session.commit()
            _stay_signed_in(current_user._get_current_object())      # … dieses Gerät bleibt angemeldet
            flash(_("Passwort geändert. Andere Geräte wurden abgemeldet."), "success")
            return redirect(url_for("main.profile"))
    history = (Loan.query.filter_by(user_id=current_user.id)
               .order_by(Loan.loaned_at.desc()).limit(50).all())
    return render_template("profile.html", history=history,
                           self_extend=get_bool("self_extend"), mail_on=get_bool("mail_enabled"),
                           max_ext=int(get_setting("max_extensions") or 0))


def _stay_signed_in(user: User):
    """Nach einem Wechsel der Sitzungs-Generation das aktuelle Gerät angemeldet lassen."""
    remembered = bool(request.cookies.get("remember_token"))
    mfa_skipped = session.get("mfa_skipped")
    login_user(user, remember=remembered and not mfa_skipped)
    if mfa_skipped:
        session["mfa_skipped"] = True


@bp.route("/abmelden-ueberall", methods=["POST"])
@login_required
def logout_everywhere():
    current_user.end_all_sessions()
    db.session.commit()
    _stay_signed_in(current_user._get_current_object())
    flash(_("Alle anderen Geräte wurden abgemeldet."), "success")
    return redirect(url_for("security.settings"))


@bp.route("/branding/<path:filename>")
def branding_file(filename):
    """Logo, Favicon und Hintergrundbild – öffentlich, da auch auf der Anmeldeseite sichtbar."""
    from flask import current_app, send_from_directory
    from werkzeug.utils import secure_filename

    from ..branding import brand_dir
    name = secure_filename(filename)
    if not name or not name.split("-", 1)[0] in ("logo", "bg", "favicon"):
        abort(404)
    resp = send_from_directory(brand_dir(), name, max_age=60 * 60 * 24 * 365)
    if name.endswith(".svg"):
        # SVG darf nie Skripte ausführen – auch nicht, wenn es direkt aufgerufen wird
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        resp.headers["Content-Type"] = "image/svg+xml"
    return resp


# ---------------------------------------------------------------- Über / Lizenz (AGPL § 13)

def _license_file():
    from pathlib import Path
    from flask import current_app
    for p in (Path(current_app.root_path).parent / "LICENSE", Path(current_app.root_path) / "LICENSE"):
        if p.is_file():
            return p
    return None


@bp.route("/sprache/<code>", methods=["POST"])
def set_language(code):
    """Sprache wechseln – angemeldet dauerhaft im Profil, sonst für diese Sitzung."""
    from ..i18n import normalize
    lang = normalize(code)
    if lang is None:
        abort(404)
    session["lang"] = lang
    from flask import g
    g.pop("_catalytix_locale", None)   # Meldungen dieser Anfrage schon in der neuen Sprache
    if current_user.is_authenticated:
        current_user.language = lang
        db.session.commit()
    return redirect(safe_next(request.form.get("next")) or url_for("main.dashboard"))


@bp.route("/info")
@login_required
def about():
    return render_template("about.html", has_license=_license_file() is not None)


@bp.route("/neuigkeiten")
@login_required
def whats_new():
    entries = news.for_user(current_user, get_locale())
    if news.mark_seen(current_user):
        db.session.commit()
    return render_template("news.html", entries=entries)


@bp.route("/neuigkeiten/gelesen", methods=["POST"])
@login_required
def news_dismiss():
    if news.mark_seen(current_user):
        db.session.commit()
    return redirect(url_for("main.dashboard"))


@bp.route("/info/lizenz")
@login_required
def license_text():
    f = _license_file()
    if f is None:
        abort(404)
    return f.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}
