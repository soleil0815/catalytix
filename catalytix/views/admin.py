"""Administration: users, groups, settings, Active Directory."""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from sqlalchemy import func, or_

from ..i18n import _, _n
from ..isbn import SOURCES
from ..ldap_auth import Directory, LDAPError, group_cn, parse_rules
from ..models import (MIN_PASSWORD_LENGTH, DEFAULT_SETTINGS, ROLE_ADMIN, ROLES, Book, Group, Loan, User, db, get_bool, get_setting,
                      set_setting)
from . import admin_required, form_bool, form_int, form_str, safe_next

bp = Blueprint("admin", __name__, url_prefix="/admin")


# Changes that someone could use to gain access or disable protection
_SECURITY_ENDPOINTS = {"admin.user_new", "admin.user_edit", "admin.user_delete", "admin.user_reset_2fa",
                       "admin.user_unlock", "admin.user_approve", "admin.user_reject", "admin.settings",
                       "admin.ldap", "admin.email", "admin.retention_run", "admin.backups", "admin.backup_now",
                       "admin.backup_delete", "admin.backup_download", "admin.group_delete"}
_RETURN_TO_PAGE = {"admin.user_new", "admin.user_edit", "admin.settings", "admin.ldap", "admin.email",
                   "admin.backups"}


@bp.before_request
def _require_second_factor_for_changes():
    """A session started on the internal network without a second factor may change security-relevant
    settings (users, roles, 2FA, networks, AD) only after confirming the second factor.

    Otherwise someone with the admin password on the LAN could e.g. reset their own 2FA, add a new
    factor and use it to sign in from outside. The same applies to anything that deletes data (retention,
    deleting groups, rejecting registrations) or disables backups. Appearance and creating or editing
    groups need no confirmation."""
    import time
    from flask import session
    if ((request.method == "POST" or request.endpoint == "admin.backup_download")
            and request.endpoint in _SECURITY_ENDPOINTS and current_user.is_authenticated
            and session.get("mfa_skipped") and current_user.has_2fa and not session.get("mfa_ok")):
        flash(_("Bitte bestätigen Sie zuerst Ihren zweiten Faktor – danach die Änderung noch einmal speichern."), "warning")
        if request.endpoint == "admin.settings":
            back = url_for("admin.settings", bereich=_settings_tab(request.form.get("bereich")))
        elif request.endpoint in _RETURN_TO_PAGE:
            back = request.path
        elif request.endpoint.startswith("admin.backup"):
            back = url_for("admin.backups")
        elif request.endpoint == "admin.retention_run":
            back = url_for("admin.settings", bereich="datenschutz")
        elif request.endpoint == "admin.group_delete":
            back = url_for("admin.groups")
        else:
            back = url_for("admin.users")
        session["mfa_pending"] = {"uid": current_user.id, "remember": False, "next": back, "ts": time.time()}
        return redirect(url_for("security.challenge"))
    return None
log = logging.getLogger(__name__)

LDAP_TEXT = ["ldap_server", "ldap_security", "ldap_ca_file", "ldap_bind_dn", "ldap_upn_suffix",
             "ldap_base_dn", "ldap_user_attr", "ldap_user_filter", "ldap_timeout"]
LDAP_MULTI = ["ldap_login_rules", "ldap_librarian_rules", "ldap_admin_rules"]
LDAP_BOOL = ["ldap_enabled", "ldap_verify_cert", "ldap_check_hostname", "ldap_nested_groups", "ldap_group_sync",
             "ldap_cache_enabled"]


def _id(value) -> int | None:
    """Database ID from a form value – ASCII digits only (no "²"), not too large for SQLite."""
    value = (value or "").strip()
    if value.isascii() and value.isdecimal() and len(value) <= 18:
        return int(value)
    return None


def _ids(values) -> set[int]:
    return {i for i in map(_id, values) if i is not None}


def _admin_count(exclude_id: int | None = None) -> int:
    q = User.query.filter_by(role=ROLE_ADMIN, active=True)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.count()


# ---------------------------------------------------------------- Users

@bp.route("/benutzer")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    role = request.args.get("rolle", "")
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.username.ilike(like), User.display_name.ilike(like),
                                 User.email.ilike(like)))
    if role in ROLES:
        query = query.filter(User.role == role)
    status = request.args.get("status", "")
    if status == "neu":
        query = query.filter(User.pending_approval.is_(True))
    elif status == "registriert":
        query = query.filter(User.self_registered.is_(True))
    elif status == "gesperrt":
        query = query.filter(User.active.is_(False), User.pending_approval.is_(False), User.email_verified.is_(True))
    from .. import lockout
    locked = lockout.locked_usernames()
    from sqlalchemy.orm import selectinload
    users = query.options(selectinload(User.groups)).order_by(User.pending_approval.desc(), User.username).all()
    # Lockouts are tracked under the normalized name ("DOMAIN\anna" = "anna")
    locked = {u.username.lower() for u in users if lockout.norm(u.username) in locked}
    if status == "anmeldesperre":
        users = [u for u in users if u.username.lower() in locked]
    active_counts = dict(db.session.query(Loan.user_id, func.count(Loan.id))
                         .filter(Loan.returned_at.is_(None)).group_by(Loan.user_id).all())
    return render_template("admin/users.html", users=users, q=q, role=role, status=status,
                           active_counts=active_counts, locked=locked)


def _save_user(user: User, is_new: bool):
    username = form_str("username", 120)
    if not username:
        return _("Benutzername fehlt.")
    clash = User.query.filter(func.lower(User.username) == username.lower(), User.id != (user.id or 0)).first()
    if clash:
        return _("Dieser Benutzername ist bereits vergeben.")
    local = is_new or user.auth_source == "local"
    role = request.form.get("role", "reader") if local else user.role  # AD role comes from the rules
    if role not in ROLES:
        return _("Unbekannte Rolle.")
    active = form_bool("active")
    if user.id == current_user.id and (role != ROLE_ADMIN or not active):
        return _("Sie können sich selbst die Administratorrechte nicht entziehen.")
    if user.role == ROLE_ADMIN and (role != ROLE_ADMIN or not active) and _admin_count(user.id) == 0:
        return _("Es muss mindestens ein aktiver Administrator bestehen bleiben.")
    password = request.form.get("password") or ""
    if user.auth_source == "local" or is_new:
        if is_new and len(password) < MIN_PASSWORD_LENGTH:
            return _("Bitte ein Passwort mit mindestens {n} Zeichen vergeben.", n=MIN_PASSWORD_LENGTH)
        if password and len(password) < MIN_PASSWORD_LENGTH:
            return _("Das Passwort muss mindestens {n} Zeichen haben.", n=MIN_PASSWORD_LENGTH)
    if is_new or user.auth_source == "local":
        user.username = username
    from ..mailer import valid_email
    email = form_str("email", 200)
    if email and not valid_email(email):
        return _("Bitte eine gültige E-Mail-Adresse eingeben (nur eine Adresse).")
    user.display_name = form_str("display_name", 200)
    user.email = email
    if user.auth_source == "local" or is_new:
        user.role = role
    if user.active != active or not active:
        # Decided manually (saved as approved or locked): that applies – the sync only re-enables accounts
        # that it locked itself
        user.ldap_gone_at = None
    user.active = active
    if active and user.pending_approval:   # "Account active" checked = approved
        user.pending_approval = False
    if not user.email_verified:
        # Admin decides: approved (confirms the address on the user's behalf) or locked – then a confirmation link
        # clicked later must not reactivate the account
        user.email_verified = True
    user.email_notifications = form_bool("email_notifications")
    if password and (user.auth_source == "local" or is_new):
        user.auth_source = "local"
        user.set_password(password)
    ids = _ids(request.form.getlist("groups"))
    user.groups = Group.query.filter(Group.id.in_(ids)).all() if ids else []
    # Without a group a librarian manages the entire collection – for an account that was already group-bound
    # (last group deleted or removed in AD), only after explicit confirmation (see User.group_bound)
    if user.groups:
        user.group_bound = True
    elif form_bool("manage_all"):
        user.group_bound = False
    return None


@bp.route("/benutzer/neu", methods=["GET", "POST"])
@admin_required
def user_new():
    user = User(active=True, role="reader", auth_source="local")
    if request.method == "POST":
        err = _save_user(user, True)
        if err:
            flash(err, "error")
        else:
            db.session.add(user)
            db.session.commit()
            flash(_("Benutzer {name} angelegt.", name=user.username), "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=user, groups=Group.query.order_by(Group.name).all(),
                           is_new=True)


@bp.route("/benutzer/<int:user_id>", methods=["GET", "POST"])
@admin_required
def user_edit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        err = _save_user(user, False)
        if err:
            flash(err, "error")
            db.session.rollback()
        else:
            db.session.commit()
            from .. import reservations
            reservations.revalidate([user.id])   # locked or group removed: release reservations
            flash(_("Benutzer gespeichert."), "success")
            return redirect(url_for("admin.users"))
    from .. import lockout, twofa
    return render_template("admin/user_form.html", user=user, groups=Group.query.order_by(Group.name).all(),
                           is_new=False, locked=lockout.norm(user.username) in lockout.locked_usernames(),
                           totp_broken=twofa.totp_unreadable(user), required=twofa.required_for(user))


@bp.route("/benutzer/<int:user_id>/freigeben", methods=["POST"])
@admin_required
def user_approve(user_id):
    from .register import maybe_activate, send_approved
    user = db.session.get(User, user_id) or abort(404)
    if user.pending_approval:
        user.pending_approval = False
        maybe_activate(user)
        db.session.commit()
        if user.active:
            send_approved(user)
        flash(_("{name} wurde freigegeben.", name=user.name) + ("" if user.active else
              " " + _("Das Konto wird aktiv, sobald die E-Mail-Adresse bestätigt ist.")), "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.users"))


@bp.route("/benutzer/<int:user_id>/ablehnen", methods=["POST"])
@admin_required
def user_reject(user_id):
    user = db.session.get(User, user_id) or abort(404)
    # Only pending or unconfirmed self-registrations – never one's own account or an administrator
    if (not user.self_registered or not (user.pending_approval or not user.email_verified)
            or user.id == current_user.id or user.role == ROLE_ADMIN):
        flash(_("Nur wartende Registrierungen können abgelehnt werden."), "error")
    elif user.active_loans:
        flash(_("Der Benutzer hat noch ausgeliehene Bücher. Sperren Sie das Konto stattdessen."), "error")
    else:
        from ..retention import OpenLoans, delete_user
        name = user.name
        try:
            books = delete_user(user)
        except OpenLoans:
            db.session.rollback()
            flash(_("Der Benutzer hat noch ausgeliehene Bücher. Sperren Sie das Konto stattdessen."), "error")
            return redirect(url_for("admin.users", status="neu"))
        db.session.commit()
        from .. import reservations
        reservations.promote_after_delete(books)
        flash(_("Registrierung von {name} abgelehnt und gelöscht.", name=name), "success")
    return redirect(url_for("admin.users", status="neu"))


@bp.route("/benutzer/<int:user_id>/2fa-zuruecksetzen", methods=["POST"])
@admin_required
def user_reset_2fa(user_id):
    from .. import lockout
    from ..twofa import reset_2fa, required_for
    user = db.session.get(User, user_id) or abort(404)
    reset_2fa(user)
    lockout.unlock_user(user.username)
    log.info("2FA of %s reset by %s", user.username, current_user.username)
    hint = " " + _("Bei der nächsten Anmeldung wird die Einrichtung verlangt.") if required_for(user) else ""
    flash(_("Zwei-Faktor-Anmeldung von {name} zurückgesetzt – Anmeldung jetzt nur mit Passwort.", name=user.username) + hint,
          "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.user_edit", user_id=user.id))


@bp.route("/benutzer/<int:user_id>/entsperren", methods=["POST"])
@admin_required
def user_unlock(user_id):
    from .. import lockout
    user = db.session.get(User, user_id) or abort(404)
    lockout.unlock_user(user.username)
    log.info("Sign-in lock of %s lifted by %s", user.username, current_user.username)
    flash(_("Anmeldesperre von {name} aufgehoben.", name=user.username), "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.user_edit", user_id=user.id))


@bp.route("/benutzer/<int:user_id>/loeschen", methods=["POST"])
@admin_required
def user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash(_("Sie können Ihr eigenes Konto nicht löschen."), "error")
    elif user.active_loans:
        flash(_("Der Benutzer hat noch ausgeliehene Bücher. Sperren Sie das Konto stattdessen."), "error")
    elif user.role == ROLE_ADMIN and _admin_count(user.id) == 0:
        flash(_("Der letzte Administrator kann nicht gelöscht werden."), "error")
    else:
        from ..retention import OpenLoans, delete_user
        name = user.username
        try:
            books = delete_user(user)   # completed loans are only counted from now on – statistics are preserved
        except OpenLoans:
            db.session.rollback()
            flash(_("Der Benutzer hat noch ausgeliehene Bücher. Sperren Sie das Konto stattdessen."), "error")
            return redirect(url_for("admin.users"))
        db.session.commit()
        from .. import reservations
        reservations.promote_after_delete(books)   # copies held for the account: next in line get their turn
        flash(_("Benutzer {name} gelöscht.", name=name), "success")
    return redirect(url_for("admin.users"))


@bp.route("/aufbewahrung/jetzt", methods=["POST"])
@admin_required
def retention_run():
    from ..retention import format_result, run_retention
    from datetime import datetime
    res = run_retention()
    set_setting("retention_last_result", f"{datetime.now():%d.%m.%Y %H:%M}: {format_result(res)}")
    db.session.commit()
    if res.get("off"):
        flash(_("Aufbewahrungsfrist ist ausgeschaltet."), "success")
    else:
        flash(_("Gelöscht: {loans} alte Ausleihe(n), {mails} Einträge im E-Mail-Protokoll.",
                loans=res["loans"], mails=res["mails"]), "success")
    return redirect(url_for("admin.settings", bereich="datenschutz"))


# ---------------------------------------------------------------- Backup

@bp.route("/datensicherung", methods=["GET", "POST"])
@admin_required
def backups():
    from .. import backup
    if request.method == "POST":
        set_setting("backup_enabled", form_bool("backup_enabled"))
        set_setting("backup_keep", min(backup.KEEP_MAX, max(backup.KEEP_MIN, form_int("backup_keep", 10, 1))))
        hour = form_int("backup_hour", 2, 0)
        set_setting("backup_hour", hour if 0 <= hour <= 23 else 2)
        db.session.commit()
        removed = backup.rotate() if backup.supported() else 0
        flash(_("Einstellungen gespeichert.") + (" " + _n("{n} ältere Sicherung entfernt.", "{n} ältere Sicherungen entfernt.",
                                                          removed) if removed else ""), "success")
        return redirect(url_for("admin.backups"))
    supported = backup.supported()
    items = backup.list_backups() if supported else []
    return render_template("admin/backups.html", supported=supported, items=items,
                           folder=str(backup.backup_dir()) if supported else "",
                           custom_dir=bool(os.environ.get("CATALYTIX_BACKUP_DIR")),
                           keep=backup.keep_count(), s=get_setting, warning=backup.warning(),
                           last_result=_stamped(get_setting("backup_last_result"), _backup_message),
                           total=sum(i["size"] for i in items))


def _stamped(value: str, translate) -> str:
    """Show a stored run result "DD.MM.YYYY HH:MM: text" in the UI language."""
    import re
    from datetime import datetime

    from ..i18n import format_datetime
    m = re.match(r"^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}): (.*)$", value or "", re.S)
    if not m:
        return value
    try:
        when = format_datetime(datetime.strptime(m.group(1), "%d.%m.%Y %H:%M"))
    except ValueError:
        when = m.group(1)
    return f"{when}: {translate(m.group(2))}"


def _retention_message(msg: str) -> str:
    from ..retention import display_message
    return display_message(msg)


def _backup_message(msg: str) -> str:
    """Result of backup.run_now (German, stored that way too) in the UI language."""
    from ..backup import display_message
    return display_message(msg)


@bp.route("/datensicherung/jetzt", methods=["POST"])
@admin_required
def backup_now():
    from ..backup import run_now
    ok, msg = run_now("-manuell")
    flash(_backup_message(msg), "success" if ok else "error")
    return redirect(url_for("admin.backups"))


@bp.route("/datensicherung/<name>")
@admin_required
def backup_download(name):
    from ..backup import find_backup
    path = find_backup(name)
    if path is None:
        abort(404)
    log.info("Backup %s downloaded by %s", name, current_user.username)
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/vnd.sqlite3", max_age=0)


@bp.route("/datensicherung/<name>/loeschen", methods=["POST"])
@admin_required
def backup_delete(name):
    from ..backup import delete_backup
    if delete_backup(name):
        flash(_("Sicherung {name} gelöscht.", name=name), "success")
    else:
        abort(404)
    return redirect(url_for("admin.backups"))


# ---------------------------------------------------------------- Groups

@bp.route("/gruppen")
@admin_required
def groups():
    groups = Group.query.order_by(Group.name).all()
    book_counts = dict(db.session.query(Book.group_id, func.count(Book.id)).group_by(Book.group_id).all())
    return render_template("admin/groups.html", groups=groups, book_counts=book_counts,
                           general_books=book_counts.get(None, 0))


def _save_group(group: Group):
    name = form_str("name", 120)
    if not name:
        return _("Name fehlt.")
    # Compare in Python: SQLite lower() does not handle umlauts ("Ärzte" / "ärzte")
    clash = any(g.name.casefold() == name.casefold() for g in Group.query.filter(Group.id != (group.id or 0)))
    if clash:
        return _("Eine Gruppe mit diesem Namen existiert bereits.")
    group.name = name
    group.description = form_str("description", 500)
    group.ldap_dn = form_str("ldap_dn", 500)
    from ..branding import clean_hex
    group.color = clean_hex(form_str("color", 16)) or "#2f6f62"
    was_lendable = group.lendable is not False
    group.lendable = not form_bool("not_lendable")
    if group.id and was_lendable and not group.lendable:
        from .. import reservations
        n = reservations.cancel_for_group(group.id)
        if n:
            flash(_n("{n} offene Vormerkung für Bücher dieser Gruppe wurde gelöscht.",
                     "{n} offene Vormerkungen für Bücher dieser Gruppe wurden gelöscht.", n), "info")
    ids = _ids(request.form.getlist("members"))
    group.users = User.query.filter(User.id.in_(ids)).all() if ids else []
    return None


@bp.route("/gruppen/neu", methods=["GET", "POST"])
@admin_required
def group_new():
    group = Group(color="#2f6f62")
    if request.method == "POST":
        err = _save_group(group)
        if err:
            flash(err, "error")
        else:
            db.session.add(group)
            db.session.commit()
            flash(_("Gruppe {name} angelegt.", name=group.name), "success")
            return redirect(url_for("admin.groups"))
    return render_template("admin/group_form.html", group=group,
                           users=User.query.order_by(User.username).all(), is_new=True)


@bp.route("/gruppen/<int:group_id>", methods=["GET", "POST"])
@admin_required
def group_edit(group_id):
    group = db.session.get(Group, group_id) or abort(404)
    if request.method == "POST":
        err = _save_group(group)
        if err:
            flash(err, "error")
            db.session.rollback()
        else:
            db.session.commit()
            from .. import reservations
            reservations.revalidate()   # Members removed: release their reservations on the group's books
            flash(_("Gruppe gespeichert."), "success")
            return redirect(url_for("admin.groups"))
    return render_template("admin/group_form.html", group=group,
                           users=User.query.order_by(User.username).all(), is_new=False)


@bp.route("/gruppen/<int:group_id>/loeschen", methods=["POST"])
@admin_required
def group_delete(group_id):
    group = db.session.get(Group, group_id) or abort(404)
    n = len(group.books)
    for b in group.books:
        b.group_id = None
    db.session.delete(group)
    db.session.commit()
    flash(_n("Gruppe {name} gelöscht. {n} Buch ist jetzt allgemein verfügbar.",
             "Gruppe {name} gelöscht. {n} Bücher sind jetzt allgemein verfügbar.", n, name=group.name), "success")
    return redirect(url_for("admin.groups"))


# ---------------------------------------------------------------- Settings

# Tabs of the settings page: key (in the address, ?bereich=…) → icon; the labels are in the template. Each tab is its
# own form and saves only its own fields – saving one tab can never change another (a missing checkbox would
# otherwise read as "off").
SETTINGS_TABS = {"allgemein": "settings", "ausleihe": "loan", "katalog": "book", "konten": "user",
                 "sicherheit": "shield", "datenschutz": "lock"}
# Anchors of the old single page (links in notices, help, bookmarks) → tab
SETTINGS_ANCHORS = {"aufbewahrung": "datenschutz", "rechtliches": "datenschutz", "abmeldung": "sicherheit",
                    "zwei-faktor": "sicherheit", "leseproben": "katalog", "isbn": "katalog", "vormerken": "ausleihe",
                    "registrierung": "konten"}


def _settings_tab(value: str | None) -> str:
    return value if value in SETTINGS_TABS else "allgemein"


def _save_general() -> None:
    set_setting("library_name", form_str("library_name", 120) or "Bibliothek")
    from ..i18n import normalize
    set_setting("default_language", normalize(request.form.get("default_language")) or "de")
    vis = request.form.get("catalog_visibility")
    set_setting("catalog_visibility", vis if vis in ("groups", "all") else "groups")


def _save_lending() -> None:
    set_setting("loan_days", min(form_int("loan_days", 28, 1), 3650))
    set_setting("extension_days", min(form_int("extension_days", 14, 1), 3650))
    set_setting("max_extensions", min(form_int("max_extensions", 2, 0), 100))
    set_setting("max_loans", min(form_int("max_loans", 0, 0), 1000))
    for key in ("self_checkout", "self_return", "self_extend"):
        set_setting(key, form_bool(key))
    was_on = get_bool("reservations_enabled")
    set_setting("reservations_enabled", form_bool("reservations_enabled"))
    set_setting("reservation_hold_days", min(form_int("reservation_hold_days", 7, 1), 60))
    set_setting("reservation_max", min(form_int("reservation_max", 5, 0), 100))
    if was_on and not form_bool("reservations_enabled"):
        from .. import reservations
        db.session.commit()
        n = reservations.cancel_all()   # otherwise copies would stay held for reservations
        if n:
            flash(_n("{n} offene Vormerkung gelöscht.", "{n} offene Vormerkungen gelöscht.", n), "info")


def _save_catalog() -> None:
    sources = [s for s in request.form.getlist("isbn_sources") if s in SOURCES]
    order = [s for s in (request.form.get("isbn_order") or "").split(",") if s in sources]
    set_setting("isbn_sources", ",".join(order + [s for s in sources if s not in order]))
    set_setting("reading_sample_google", form_bool("reading_sample_google"))
    _save_google_key()


def _save_accounts() -> None:
    reg = request.form.get("registration_mode")
    set_setting("registration_mode", reg if reg in ("off", "approval", "open") else "off")
    set_setting("registration_verify_email", form_bool("registration_verify_email"))
    set_setting("registration_notify_admins", form_bool("registration_notify_admins"))
    set_setting("registration_domains", form_str("registration_domains", 500))
    gid = _id(request.form.get("registration_group_id"))
    set_setting("registration_group_id", str(gid) if gid is not None and db.session.get(Group, gid) else "")


def _save_security() -> None:
    pol = request.form.get("mfa_policy")
    set_setting("mfa_policy", pol if pol in ("optional", "admins", "librarians", "all") else "optional")
    mode = request.form.get("mfa_internal")
    set_setting("mfa_internal", mode if mode in ("same", "optional", "skip") else "same")
    from ..netzone import parse, parse_own
    nets, bad = parse((request.form.get("internal_networks") or "").strip()[:2000])
    if bad:
        flash(_("Internes Netz: ungültige Einträge {entries} – Netze nicht geändert.", entries=", ".join(bad[:5])), "error")
    else:
        set_setting("internal_networks", "\n".join(str(n) for n in nets))
    entries, bad = parse_own((request.form.get("own_public_hosts") or "")[:1000])
    if bad:
        flash(_("Eigener Anschluss: ungültige Einträge {entries} – nicht geändert.", entries=", ".join(bad[:5])), "error")
    else:
        set_setting("own_public_hosts", "\n".join(entries))
    _save_session_settings()


def _save_privacy() -> None:
    for key in ("imprint_url", "privacy_url"):
        url = form_str(key, 500)
        # "//host/…" and "/\host/…" would be protocol-relative links to another server
        ok = (url.startswith(("https://", "http://", "/")) and not url.startswith(("//", "/\\"))
              and not re.search(r"[\x00-\x20\x7f]", url))   # browsers drop tabs inside URLs: "/<tab>/host"
        set_setting(key, url if ok else "")
    months = request.form.get("retention_months", "12")
    set_setting("retention_months", months if months in ("0", "3", "6", "12", "24", "36") else "12")


_SETTINGS_SAVE = {"allgemein": _save_general, "ausleihe": _save_lending, "katalog": _save_catalog,
                  "konten": _save_accounts, "sicherheit": _save_security, "datenschutz": _save_privacy}


@bp.route("/einstellungen", methods=["GET", "POST"])
@admin_required
def settings():
    tab = _settings_tab(request.values.get("bereich"))
    if request.method == "POST":
        if request.form.get("bereich") not in SETTINGS_TABS:
            abort(400)                       # every form names its tab; nothing is saved by guessing
        _SETTINGS_SAVE[tab]()
        db.session.commit()
        flash(_("Einstellungen gespeichert."), "success")
        if tab == "konten" and get_setting("registration_mode") != "off" and not get_setting("app_base_url"):
            flash(_("Für die Selbstregistrierung fehlt die Basis-Adresse für Links in E-Mails (Administration → "
                    "E-Mail). Ohne sie werden keine Bestätigungs-Links verschickt; mit E-Mail-Bestätigung ist die "
                    "Registrierung so lange gesperrt."), "warning")
        return redirect(url_for("admin.settings", bereich=tab))
    active_sources = [s for s in get_setting("isbn_sources").split(",") if s]
    all_sources = active_sources + [s for s in SOURCES if s not in active_sources]
    from ..mailer import mail_active
    from .. import netzone
    from .. import autologout
    from ..reading_sample import quota_problem as reading_sample_quota
    return render_template("admin/settings.html", s=get_setting, sources=SOURCES, tab=tab, tabs=SETTINGS_TABS,
                           anchors=SETTINGS_ANCHORS,
                           session_hours=autologout.general_hours(),
                           retention_result=_stamped(get_setting("retention_last_result"), _retention_message),
                           connection=netzone.describe(), default_networks=netzone.DEFAULT_INTERNAL,
                           active_sources=active_sources, all_sources=all_sources,
                           google_key_set=bool(get_setting("google_api_key")),
                           google_quota=reading_sample_quota(),
                           groups=Group.query.order_by(Group.name).all(), mail_on=mail_active())


def _save_google_key() -> None:
    """Own Google API key (reading samples, ISBN lookup): stored encrypted, never shown again; empty = keep."""
    from .. import reading_sample
    new = (request.form.get("google_api_key") or "").strip()
    if request.form.get("clear_google_api_key"):
        set_setting("google_api_key", "")
    elif new:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", new):
            flash(_("Der Google-API-Schlüssel hat nicht das erwartete Format – nicht gespeichert."), "error")
            return
        set_setting("google_api_key", new)
    else:
        return
    reading_sample.clear_cache()          # answers given without / with the old key


def _save_session_settings() -> None:
    """Section "Automatische Abmeldung" (see autologout.py)."""
    from .. import autologout
    hours = min(form_int("session_hours", autologout.general_hours(), 1) or autologout.DEFAULT_HOURS,
                autologout.MAX_HOURS)
    set_setting("session_hours", hours)
    too_long = {
        "session_admin_minutes": _("Automatische Abmeldung: Die Frist für Admins ist länger als die für alle – es gilt "
                                   "die kürzere ({hours} Std.)."),
        "session_external_minutes": _("Automatische Abmeldung: Die Frist für Anmeldungen von außen ist länger als die "
                                      "für alle – es gilt die kürzere ({hours} Std.)."),
    }
    for key, message in too_long.items():
        if not (request.form.get(key) or "").strip():
            set_setting(key, "")
            continue
        minutes = min(form_int(key, autologout.MIN_MINUTES, autologout.MIN_MINUTES) or autologout.MIN_MINUTES,
                      autologout.MAX_MINUTES)
        set_setting(key, minutes)
        if minutes > hours * 60:
            flash(message.replace("{hours}", str(hours)), "warning")
    set_setting("session_warning", form_bool("session_warning"))
    set_setting("session_browser_close", form_bool("session_browser_close"))


# ---------------------------------------------------------------- Active Directory / LDAP

def _ldap_form_cfg() -> dict:
    cfg = {k: form_str(k, 500) for k in LDAP_TEXT}
    for k in LDAP_MULTI:
        cfg[k] = "\n".join(parse_rules(request.form.get(k, "")))
    for k in LDAP_BOOL:
        cfg[k] = "1" if form_bool(k) else "0"
    pw = request.form.get("ldap_bind_password") or ""
    same_target = all(cfg[k].strip().lower() == (get_setting(k) or "").strip().lower()
                      for k in ("ldap_server", "ldap_bind_dn"))
    # Reuse the stored password only for the same server and the same service account
    cfg["ldap_bind_password"] = pw if pw else (get_setting("ldap_bind_password") if same_target else "")
    if request.form.get("clear_bind_password"):
        cfg["ldap_bind_password"] = ""
    from ..people import CACHE_HOURS
    hours = request.form.get("ldap_cache_hours", "")
    cfg["ldap_cache_hours"] = hours if hours in {str(h) for h in CACHE_HOURS} else (get_setting("ldap_cache_hours") or "1")
    return cfg


# Role rules where a bare group name would be dangerous: it matches EVERY group of that name in the directory –
# whoever may create groups somewhere (e.g. teachers in their OU) could make themselves administrator
STRICT_RULES = ("ldap_admin_rules", "ldap_librarian_rules")


def bare_names(text: str) -> list[str]:
    from ..ldap_auth import is_group_name
    return [r for r in parse_rules(text) if is_group_name(r)]


def _resolve_rule_names(cfg: dict, target: dict | None = None) -> bool:
    """Replace bare group names in the administrator and librarian rules by the DN of exactly that group.

    Returns False (do not save) if a name is not found or exists more than once. Without a service account or with
    the directory not reachable the names stay as they are, with a warning – saving other settings must stay
    possible. The replaced rules go into `target` (default: `cfg`) – "trust certificate" looks up with the new
    certificates but saves the form values."""
    target = cfg if target is None else target
    names = {n for k in STRICT_RULES for n in bare_names(cfg.get(k, ""))}
    if not names or cfg.get("ldap_enabled") != "1":
        return True
    if not cfg.get("ldap_bind_dn"):
        flash(_("Gruppennamen bei Administratoren und Verwaltern konnten nicht in einen eindeutigen DN umgewandelt "
                "werden (kein Dienstkonto). Ein bloßer Name passt auf jede gleichnamige Gruppe im AD – tragen Sie "
                "besser den vollständigen DN ein."), "warning")
        return True
    try:
        directory = Directory(cfg)
        found = {n: directory.find_groups_by_name(n) for n in sorted(names)}
    except LDAPError as exc:
        flash(_("Gruppennamen bei Administratoren und Verwaltern konnten nicht in einen eindeutigen DN umgewandelt "
                "werden ({error}). Beim nächsten Speichern wird es erneut versucht.", error=str(exc)), "warning")
        return True
    ok = True
    for name, dns in found.items():
        if not dns:
            flash(_("Die Gruppe „{name}“ wurde im Verzeichnis nicht gefunden – nichts gespeichert.", name=name), "error")
            ok = False
        elif len(dns) > 1:
            flash(_("Den Gruppennamen „{name}“ gibt es mehrfach ({dns}) – bitte den vollständigen DN der richtigen "
                    "Gruppe eintragen. Nichts gespeichert.", name=name, dns="; ".join(dns)), "error")
            ok = False
    if not ok:
        return False
    for k in STRICT_RULES:
        target[k] = "\n".join(found[r][0] if r in found else r for r in parse_rules(cfg.get(k, "")))
    n = len(found)
    flash(_n("{n} Gruppenname bei Administratoren/Verwaltern durch den eindeutigen DN ersetzt.",
             "{n} Gruppennamen bei Administratoren/Verwaltern durch den eindeutigen DN ersetzt.", n), "info")
    return True


@bp.route("/verzeichnisdienst", methods=["GET", "POST"])
@admin_required
def ldap():
    from .. import ldap_tls

    test_result, test_error, cfg, fetched, ad_groups = None, None, None, None, None
    if request.method == "POST":
        cfg = _ldap_form_cfg()
        action = request.form.get("action", "save")
        if action == "save" and _resolve_rule_names(cfg):
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            from .. import people
            if not people.cache_enabled() and (get_setting("dircache_ok_at") or people._cache_rows()):
                people.clear_cache()      # disabled: delete cached people immediately
            flash(_("Verzeichnisdienst-Einstellungen gespeichert."), "success")
            return redirect(url_for("admin.ldap"))
        if action == "cache_sync":
            from .. import people
            if not people.directory_available():
                flash(_("Für den Abgleich ist ein Dienstkonto nötig – erst eintragen und speichern."), "error")
            elif people.start_sync_async(current_app._get_current_object()):
                flash(_("Abgleich läuft im Hintergrund. Das Ergebnis steht gleich hier – Seite neu laden."), "success")
            else:
                flash(_("Es läuft bereits ein Abgleich."), "info")
            return redirect(url_for("admin.ldap") + "#cache")
        if action == "cache_clear":
            from .. import people
            n = people.clear_cache()
            flash(_n("{n} zwischengespeicherte Person gelöscht.", "{n} zwischengespeicherte Personen gelöscht.", n),
                  "success")
            return redirect(url_for("admin.ldap") + "#cache")
        if action == "trust_cert":
            try:
                certs = ldap_tls.load_pem_bundle(request.form.get("pending_pem", ""))
            except ValueError:
                certs = []
            if not certs:
                flash(_("Keine gültigen Zertifikate zum Übernehmen."), "error")
            elif _resolve_rule_names({**cfg, "ldap_tls_certs": ldap_tls.to_pem(certs), "ldap_verify_cert": "1"},
                                     target=cfg):
                for k, v in cfg.items():
                    set_setting(k, v)
                set_setting("ldap_tls_certs", ldap_tls.to_pem(certs))
                set_setting("ldap_verify_cert", "1")
                db.session.commit()
                flash(_n("{n} Zertifikat gespeichert. Verbindungen zum DC werden jetzt damit geprüft.",
                         "{n} Zertifikate gespeichert. Verbindungen zum DC werden jetzt damit geprüft.", len(certs)), "success")
                return redirect(url_for("admin.ldap"))
        elif action == "clear_cert":
            set_setting("ldap_tls_certs", "")
            db.session.commit()
            flash(_("Gespeicherte DC-Zertifikate entfernt."), "success")
            return redirect(url_for("admin.ldap"))
        elif action == "fetch_cert":
            try:
                try:
                    directory = Directory(cfg)
                except LDAPError:
                    directory = None
                servers = [x for x in cfg.get("ldap_server", "").replace(",", " ").split() if x]
                if not servers:
                    raise ldap_tls.CertFetchError(_("Bitte zuerst einen Server eintragen."))
                timeout = int(cfg.get("ldap_timeout") or 8)
                res = ldap_tls.fetch_trust_anchors(directory, servers, cfg.get("ldap_security", "none"), timeout)
                trust_fps = {c.fingerprint(ldap_tls.hashes.SHA256()) for c in res.trust}
                fetched = {
                    "pem": ldap_tls.to_pem(res.trust),
                    "certs": [(ldap_tls.describe(c), c.fingerprint(ldap_tls.hashes.SHA256()) in trust_fps)
                              for c in res.chain],
                    "notes": res.notes,
                    "hosts": res.hostname_ok,
                }
            except ldap_tls.CertFetchError as exc:
                test_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                test_error = _("Zertifikat konnte nicht abgerufen werden: {error}", error=f"{exc.__class__.__name__}: {exc}")
        elif action != "save":             # "save" refused above: show the form again with the entered values
            try:
                directory = Directory(cfg)
                if action == "test_bind":
                    test_result = {"message": directory.test_connection()}
                elif action == "list_groups":
                    ad_groups = directory.list_groups(form_str("group_query", 100))
                elif action == "test_user":
                    info = directory.authenticate(form_str("test_user", 200), request.form.get("test_password") or "")
                    test_result = {"user": info}
            except LDAPError as exc:
                test_error = str(exc)
            except Exception as exc:  # noqa: BLE001 – admin should see the error
                test_error = f"{exc.__class__.__name__}: {exc}"
    values = {k: get_setting(k) for k in DEFAULT_SETTINGS if k.startswith("ldap_")}
    if cfg:
        values.update(cfg)
    has_pw = bool(get_setting("ldap_bind_password"))
    values["ldap_bind_password"] = ""
    try:
        stored = [ldap_tls.describe(c) for c in ldap_tls.load_pem_bundle(get_setting("ldap_tls_certs"))]
    except ValueError:
        stored = []
    ldap_groups = Group.query.filter(Group.ldap_dn != "").order_by(Group.name).all()
    from .. import people
    cache = {"rows": people._cache_rows(), "running": people.is_running(), "result": people.last_result(),
             "hours": people.CACHE_HOURS, "ready": people.cache_ready(),
             "text": people.result_text(people.last_result())}
    return render_template("admin/ldap.html", v=values, cache=cache, has_pw=has_pw, test_result=test_result,
                           test_error=test_error, ROLES=ROLES, ldap_groups=ldap_groups,
                           stored_certs=stored, fetched=fetched, ad_groups=ad_groups,
                           group_query=request.form.get("group_query", ""), group_cn=group_cn,
                           bare_strict=sorted({n for k in STRICT_RULES for n in bare_names(get_setting(k))}))


# ---------------------------------------------------------------- Email

MAIL_TEXT = ["smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from", "smtp_from_name",
             "smtp_reply_to", "app_base_url", "mail_reminder_days", "mail_overdue_repeat_days",
             "mail_overdue_max", "mail_hour"]
MAIL_BOOL = ["mail_enabled", "smtp_verify_cert", "mail_on_loan", "mail_on_return", "mail_reminder",
             "mail_overdue", "mail_digest"]


def _mail_form_cfg() -> dict:
    cfg = {k: form_str(k, 300) for k in MAIL_TEXT}
    if cfg["smtp_security"] not in ("none", "starttls", "ssl"):
        cfg["smtp_security"] = "starttls"
    for k in ("smtp_port", "mail_reminder_days", "mail_overdue_repeat_days", "mail_overdue_max", "mail_hour"):
        if _id(cfg[k]) is None:
            cfg[k] = DEFAULT_SETTINGS[k]
    limits = {"smtp_port": (1, 65535), "mail_reminder_days": (0, 60), "mail_overdue_repeat_days": (1, 365),
              "mail_overdue_max": (0, 50), "mail_hour": (0, 23)}
    for k, (lo, hi) in limits.items():
        cfg[k] = str(min(hi, max(lo, int(cfg[k]))))
    cfg["app_base_url"] = cfg["app_base_url"].rstrip("/")
    if cfg["app_base_url"] and not cfg["app_base_url"].lower().startswith(("https://", "http://")):
        cfg["app_base_url"] = ""   # only real web addresses (links in emails)
    for k in MAIL_BOOL:
        cfg[k] = "1" if form_bool(k) else "0"
    cfg["mail_footer"] = (request.form.get("mail_footer") or "").strip()[:1000]
    pw = request.form.get("smtp_password") or ""
    if request.form.get("clear_smtp_password"):
        cfg["smtp_password"] = ""
    elif pw:
        cfg["smtp_password"] = pw
    elif all(cfg[k].strip().lower() == (get_setting(k) or "").strip().lower() for k in ("smtp_host", "smtp_user")):
        # Reuse the stored password only for the same server and the same account – otherwise someone could
        # have it sent to a foreign server
        cfg["smtp_password"] = get_setting("smtp_password")
    else:
        cfg["smtp_password"] = ""
    return cfg


@bp.route("/email", methods=["GET", "POST"])
@admin_required
def email():
    from datetime import datetime

    from .. import mailer
    from ..i18n import format_datetime
    from ..models import MailLog

    cfg = None
    if request.method == "POST":
        cfg = _mail_form_cfg()
        action = request.form.get("action", "save")
        if action == "save":
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            flash(_("E-Mail-Einstellungen gespeichert."), "success")
            return redirect(url_for("admin.email"))
        if action == "test":
            to = form_str("test_to", 300) or current_user.email
            if not to or "@" not in to:
                flash(_("Bitte eine Empfängeradresse für die Testmail angeben."), "error")
            else:
                smtp_cfg = mailer.SmtpConfig.from_settings(cfg)
                subject, text, html = mailer.render_mail(
                    "test", server=f"{smtp_cfg.host}:{smtp_cfg.port} ({smtp_cfg.security})",
                    now=format_datetime(datetime.now()))
                ok, err = mailer.send_mail(to, subject, text, html, kind="test", cfg=smtp_cfg,
                                           user_id=current_user.id)
                if ok:
                    flash(_("Testmail an {to} gesendet. Schauen Sie ins Postfach (ggf. auch in den Spam-Ordner).", to=to), "success")
                else:
                    flash(_("Testmail fehlgeschlagen: {error}", error=err), "error")
        elif action == "run_now":
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            stats = mailer.run_notifications()
            set_setting("mail_last_result", mailer.stats_record(datetime.now(), stats, manual=True))
            db.session.commit()
            flash(_("Erinnerungslauf: {stats}", stats=mailer.format_stats(stats)), "error" if stats.get("error") else "success")
            return redirect(url_for("admin.email"))

    values = {k: get_setting(k) for k in DEFAULT_SETTINGS if k.startswith(("smtp_", "mail_")) or k == "app_base_url"}
    if cfg:
        values.update(cfg)
    has_pw = bool(get_setting("smtp_password"))
    values["smtp_password"] = ""
    logs = MailLog.query.order_by(MailLog.created_at.desc()).limit(60).all()
    pending = mailer.collect_due() if mailer.mail_active() else {}
    without_mail = User.query.filter(User.active.is_(True), (User.email == "") | (User.email.is_(None))).count()
    suggested_url = request.host_url.rstrip("/")
    values = dict(values, mail_last_result=mailer.last_result_text(values.get("mail_last_result", "")))
    return render_template("admin/email.html", v=values, has_pw=has_pw, logs=logs, pending=pending,
                           without_mail=without_mail, suggested_url=suggested_url)


# ---------------------------------------------------------------- Appearance

@bp.route("/erscheinungsbild", methods=["GET", "POST"])
@admin_required
def branding():
    from .. import branding as br

    if request.method == "POST":
        if request.form.get("action") == "reset":
            br.reset_all()
            db.session.commit()
            flash(_("Erscheinungsbild auf Standard zurückgesetzt."), "success")
            return redirect(url_for("admin.branding"))
        errors = []
        for kind, field in (("logo", "logo"), ("bg", "bg")):
            if form_bool(f"remove_{kind}"):
                br.remove(kind)
                continue
            upload = request.files.get(field)
            if upload and upload.filename:
                try:
                    br.store(kind, upload)
                except br.BrandingError as exc:
                    errors.append(f"{_('Logo') if kind == 'logo' else _('Hintergrundbild')}: {exc}")
        for key in ("brand_accent", "brand_brass", "brand_side"):
            set_setting(key, br.clean_hex(form_str(key, 20)))
        set_setting("brand_logo_text", form_bool("brand_logo_text"))
        set_setting("brand_logo_plate", form_bool("brand_logo_plate"))
        set_setting("login_show_shelf", form_bool("login_show_shelf"))
        set_setting("brand_bg_dim", min(85, form_int("brand_bg_dim", 45, 0)))
        set_setting("login_eyebrow", form_str("login_eyebrow", 60))
        set_setting("login_text", form_str("login_text", 300))
        name = form_str("library_name", 120)
        if name:
            set_setting("library_name", name)
        db.session.commit()
        for e in errors:
            flash(e, "error")
        if not errors:
            flash(_("Erscheinungsbild gespeichert."), "success")
        return redirect(url_for("admin.branding"))
    return render_template("admin/branding.html", s=get_setting, colors=br.colors(), defaults=br.DEFAULTS)


@bp.route("/erscheinungsbild/vorschau")
@admin_required
def branding_preview():
    return render_template("login.html", preview=True)
