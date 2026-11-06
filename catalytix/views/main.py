"""Login, initial setup, overview, profile."""
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

# Simple protection against password guessing: 8 failed attempts per user/IP -> 5 minute lockout
# Protection against password guessing: see lockout.py (lockout stored in the database)


class AccountMismatch(Exception):
    """According to the immutable AD ID the account belongs to another person – do not overwrite."""


def same_person(user: User, info) -> bool:
    """Does the account belong to this AD person? Without a stored ID (older accounts, OpenLDAP) the name counts."""
    return not (user.ldap_guid and info.guid and user.ldap_guid != info.guid)


def account_for(info, directory=None) -> User | None:
    """Account for an AD person – via the immutable AD ID (finds it even after a rename in AD), otherwise
    via the username.

    If an AD account with the same name belongs to another person, AD is consulted: if that person was only
    renamed, their account moves to the new name; if they no longer exist, it is retired under a substitute name.
    Only then is the name free – nobody takes over someone else's loans. If this cannot be resolved (AD not
    reachable, new name of the previous person already taken), AccountMismatch is raised – nothing is created."""
    if info.guid:
        user = User.query.filter_by(ldap_guid=info.guid).first()
        if user is not None:
            return user
    user = User.query.filter(func.lower(User.username) == (info.username or "").lower()).first()
    if user is None or user.auth_source != "ldap" or same_person(user, info):
        return user
    # same name, different person
    from ..ldap_auth import Directory, LDAPError
    directory = directory or Directory()
    try:
        other = directory.find_by_guid(user.ldap_guid)
    except LDAPError as exc:
        log.warning("Account %s: previous owner cannot be checked in AD (%s) – nothing changed", user.username, exc)
        raise AccountMismatch(user.username) from exc
    if other is not None and other.username:
        # The previous person still exists, just under another name: their account moves
        sync_ldap_user(other, user, active_check=False)
        db.session.flush()
        if user.username.lower() == (info.username or "").lower():
            # New name still taken – by a retired account of a person who has left? Then release it.
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
            raise AccountMismatch(user.username)   # move not possible – name stays taken
        return None
    retire_account(user)
    return None


def retire_account(user: User) -> None:
    """Retire the AD account of a person who has left and release the name (loans are preserved).
    A manually locked account stays manually locked (it is not automatically re-enabled later)."""
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
    """Apply AD data to an account. active_check=False: only update name/details (person exists but may no
    longer be authorized) – role, groups and lock status then remain untouched."""
    user = existing
    if user is not None and not same_person(user, info):
        raise AccountMismatch(user.username)
    if user is None:
        user = User(username=info.username, auth_source="ldap")
        db.session.add(user)
    elif (rename and user.auth_source == "ldap" and info.username
          and user.username.lower() != info.username.lower()):
        # renamed in AD: account (with all loans) follows, provided the new name is free
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
        user.ldap_guid = info.guid          # never overwrite a different ID (see same_person)
    if not active_check:
        return user
    user.role = info.role
    if user.ldap_gone_at is not None and info.role is not None:
        # locked by the sync, but active and authorized in AD again → re-enable
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
    from werkzeug.security import check_password_hash
    from ..models import hash_secret
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_secret("timing-equaliser")
    check_password_hash(_DUMMY_HASH, password or "")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = form_str("username", 200)
        password = request.form.get("password") or ""
        # Lockout key from the normalized name: "DOMAIN\\anna" and "anna@x" count as "anna"
        key = f"{lockout.norm(username)}|{request.remote_addr}"
        from .. import netzone
        if (lockout.throttled(key) or lockout.user_throttled(username, internal=netzone.is_internal())
                or lockout.ip_throttled()):
            flash(_("Zu viele Fehlversuche. Bitte warten Sie einige Minuten."), "error")
            return render_template("login.html", username=username), 429

        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        error = _("Benutzername oder Passwort ist falsch.")
        ok_user = None
        mismatch = False      # password was correct, but the account cannot be resolved – not a failed attempt

        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                ok_user = user
        elif not ldap_enabled():
            # Spend as long computing as for a real account – otherwise the response time reveals
            # whether the username exists.
            _dummy_password_check(password)
        elif ldap_enabled() and username and password:
            try:
                info = authenticate(username, password)
                if info.role is None:
                    # Deliberately the same message as for a wrong password: from outside it must not
                    # be discernible whether an account exists or the password was correct.
                    log.info("LDAP sign-in %s refused: no rule matches (%s)", username, info.dn)
                else:
                    if user is None or user.auth_source == "ldap":
                        # via the immutable AD ID: same account even after a rename in AD
                        user = account_for(info)
                        db.session.commit()
                    if user is not None and user.auth_source != "ldap":
                        # A local account is never taken over via AD login (e.g. "DOMAIN\\kasse"
                        # for the local account "kasse") – same message as for a wrong password.
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
                error = _("Anmeldung derzeit nicht möglich. Bitte versuchen Sie es später erneut.")
            except LDAPError as exc:
                log.warning("LDAP sign-in %s failed: %s", username, exc)
            except Exception:  # noqa: BLE001 – never give details to the client
                log.exception("Unexpected error during LDAP sign-in")
                error = _("Anmeldung derzeit nicht möglich. Bitte versuchen Sie es später erneut.")

        # Self-registered accounts: password is correct, but not yet confirmed/approved.
        # Only someone who knows the correct password sees these notices.
        if ok_user is not None and ok_user.self_registered and not ok_user.email_verified:
            session["resend_uid"] = ok_user.id
            return render_template("login.html", username=username, unverified=True), 403
        if ok_user is not None and ok_user.pending_approval:
            flash(_("Ihr Konto wartet noch auf die Freigabe durch die Bibliothek."), "info")
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
            session["ldap_ok_at"] = int(time.time())   # just verified against AD
        from .security import start_login  # second factor, if set up
        return start_login(ok_user, bool(request.form.get("remember")), request.args.get("next"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    for k in ("mfa_ok", "mfa_skipped", "mfa_setup", "mfa_pending", "show_new_codes", "totp_setup",
              "webauthn_state", "webauthn_reg_state", "ldap_ok_at"):
        session.pop(k, None)
    logout_user()
    flash(_("Sie wurden abgemeldet."), "info")
    return redirect(url_for("main.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role=ROLE_ADMIN).first() is not None:
        return redirect(url_for("main.login"))
    if get_setting("setup_completed") == "1" or User.query.first() is not None:
        # Once set up: even without an administrator (e.g. accidentally deleted) nobody may create
        # a new admin account here via the web.
        return render_template(
            "error.html", code=403, title=_("Einrichtung bereits abgeschlossen"),
            text=_("Es gibt kein Administratorkonto mehr. Legen Sie auf dem Server eines an: docker compose exec "
                   "catalytix docker-entrypoint.sh flask --app wsgi create-admin NAME")), 403
    from .. import setupcode
    setupcode.ensure()
    if request.method == "POST":
        username = form_str("username", 120)
        password = request.form.get("password") or ""
        key = f"setup|{request.remote_addr}"
        if lockout.throttled(key):
            flash(_("Zu viele Fehlversuche. Bitte warten Sie einige Minuten."), "error")
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
        # A security update always takes precedence – even if it comes from an older, not yet seen version
        pairs = [(e["version"], i) for e in fresh for i in e["items"]]
        version, first = next(((v, i) for v, i in pairs if i["kind"] == "security"), pairs[0])
        news_hint = {"version": version, "first": first, "more": len(pairs) - 1}
    from .. import reservations
    ready = reservations.ready_for(current_user) if reservations.enabled() else []
    from .. import composefile
    compose_old = composefile.outdated() if current_user.is_admin else None
    return render_template("dashboard.html", my_loans=my_loans, newest=newest, stats=stats,
                           overdue=overdue, recent_loans=recent_loans, news_hint=news_hint, ready=ready,
                           compose_old=compose_old, compose_min=composefile.COMPOSE_MIN)


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    action = request.form.get("action") if request.method == "POST" else None
    if action == "data":
        # Change own data – local (registered/created) accounts only; AD accounts come from the directory
        if current_user.auth_source != "local":
            abort(400)
        name = form_str("display_name", 200)
        if not name:
            flash(_("Bitte einen Namen angeben."), "error")
            return redirect(url_for("main.profile"))
        current_user.display_name = name
        if _apply_email(form_str("email", 200)):
            db.session.commit()
            flash(_("Ihre Daten wurden gespeichert."), "success")
        return redirect(url_for("main.profile"))
    if action == "notify":
        current_user.email_notifications = bool(request.form.get("email_notifications"))
        if current_user.auth_source == "local" and "email" in request.form:
            if not _apply_email(form_str("email", 200)):
                return redirect(url_for("main.profile"))
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
            current_user.set_password(new)   # signs out all other devices …
            db.session.commit()
            _stay_signed_in(current_user._get_current_object())      # … this device stays signed in
            flash(_("Passwort geändert. Andere Geräte wurden abgemeldet."), "success")
            return redirect(url_for("main.profile"))
    history = (Loan.query.filter_by(user_id=current_user.id)
               .order_by(Loan.loaned_at.desc()).limit(50).all())
    return render_template("profile.html", history=history,
                           self_extend=get_bool("self_extend"), mail_on=get_bool("mail_enabled"),
                           max_ext=int(get_setting("max_extensions") or 0))


def _apply_email(mail: str) -> bool:
    """Apply a new email address for a local account – with the same rules as for registration.

    False: not applied (error reported or confirmation link sent) – the caller only redirects."""
    from ..mailer import valid_email
    if mail and not valid_email(mail):
        db.session.rollback()
        flash(_("Bitte eine gültige E-Mail-Adresse eingeben."), "error")
        return False
    changed = bool(mail) and mail.lower() != (current_user.email or "").lower()
    if changed and User.query.filter(func.lower(User.email) == mail.lower(),
                                     User.id != current_user.id).first() is not None:
        db.session.rollback()
        flash(_("Diese E-Mail-Adresse wird bereits von einem anderen Konto verwendet."), "error")
        return False
    if current_user.self_registered and changed:
        from .register import _allowed_domain, needs_verification, send_email_change
        if not _allowed_domain(mail):
            # same rule as for registration
            db.session.rollback()
            flash(_("Diese E-Mail-Adresse ist für Ihr Konto nicht erlaubt."), "error")
            return False
        if needs_verification():
            # as with registration: only after confirmation via the new address (other changes apply immediately)
            db.session.commit()
            if send_email_change(current_user._get_current_object(), mail):
                flash(_("Wir haben einen Bestätigungslink an {email} geschickt. Die neue Adresse gilt, sobald Sie "
                        "ihn öffnen.", email=mail), "info")
            else:
                flash(_("Die Bestätigungsmail konnte nicht gesendet werden – die Adresse bleibt unverändert."),
                      "error")
            return False
    current_user.email = mail
    return True


def _stay_signed_in(user: User):
    """Keep the current device signed in after a session generation change."""
    from .. import autologout
    mfa_skipped = session.get("mfa_skipped")
    # "stay signed in" lives in the session (autologout.STAY_KEY) and is kept as it is
    login_user(user, remember=False)
    autologout.touch(force=True)
    if mfa_skipped:
        session["mfa_skipped"] = True


@bp.route("/api/sitzung")
@login_required
def session_status():
    """Seconds until the automatic sign-out – for the warning dialog. Not counted as activity
    (see enforce_idle_signout in __init__.py)."""
    from flask import jsonify
    from .. import autologout, netzone
    left = autologout.seconds_left(current_user, netzone.is_internal())
    return jsonify(ok=True, seconds_left=left, warn_seconds=autologout.WARN_SECONDS)


@bp.route("/api/sitzung/weiter", methods=["POST"])
@login_required
def session_keepalive():
    """"Keep working" in the warning dialog: the request itself counts as activity."""
    from flask import jsonify
    from .. import autologout, netzone
    return jsonify(ok=True, seconds_left=autologout.seconds_left(current_user, netzone.is_internal()),
                   warn_seconds=autologout.WARN_SECONDS)


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
    """Logo, favicon and background image – public, since they are also shown on the login page."""
    from flask import current_app, send_from_directory
    from werkzeug.utils import secure_filename

    from ..branding import brand_dir
    name = secure_filename(filename)
    if not name or not name.split("-", 1)[0] in ("logo", "bg", "favicon"):
        abort(404)
    resp = send_from_directory(brand_dir(), name, max_age=60 * 60 * 24 * 365)
    if name.endswith(".svg"):
        # SVG must never execute scripts – not even when requested directly
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        resp.headers["Content-Type"] = "image/svg+xml"
    return resp


# ---------------------------------------------------------------- About / license (AGPL § 13)

def _license_file():
    from pathlib import Path
    from flask import current_app
    for p in (Path(current_app.root_path).parent / "LICENSE", Path(current_app.root_path) / "LICENSE"):
        if p.is_file():
            return p
    return None


@bp.route("/sprache/<code>", methods=["POST"])
def set_language(code):
    """Switch language – stored in the profile when signed in, otherwise for this session."""
    from ..i18n import normalize
    lang = normalize(code)
    if lang is None:
        abort(404)
    session["lang"] = lang
    from flask import g
    g.pop("_catalytix_locale", None)   # messages of this request already in the new language
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
