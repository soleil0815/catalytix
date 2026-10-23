"""Hilfe – zeigt jedem nur, was für seine Rolle gilt, und springt zur passenden Stelle.

Jedes Thema hat eine Mindestrolle und die Seiten (Endpunkte), zu denen es gehört. Der „?“-Knopf
oben rechts führt so direkt zur Hilfe der aktuellen Seite. Die Texte liegen je Sprache in
templates/help/<sprache>/<thema>.html und dürfen die aktuellen Einstellungen verwenden
(z. B. Leihfrist, Selbstausleihe), damit die Hilfe zum tatsächlichen Betrieb passt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

from ..i18n import get_locale
from ..models import ROLE_ADMIN, ROLE_LIBRARIAN, ROLE_READER, get_bool, get_int, get_setting

bp = Blueprint("help", __name__)

RANK = {ROLE_READER: 0, ROLE_LIBRARIAN: 1, ROLE_ADMIN: 2}


@dataclass(frozen=True)
class Topic:
    id: str
    role: str
    title_de: str
    title_en: str
    endpoints: tuple[str, ...] = field(default_factory=tuple)

    def title(self) -> str:
        return self.title_en if get_locale() == "en" else self.title_de


GROUPS = {
    ROLE_READER: ("Für alle", "For everyone"),
    ROLE_LIBRARIAN: ("Bibliotheksverwaltung", "Library staff"),
    ROLE_ADMIN: ("Administration", "Administration"),
}

TOPICS: tuple[Topic, ...] = (
    # --- alle
    Topic("start", ROLE_READER, "Erste Schritte", "Getting started", ("main.dashboard", "main.about")),
    Topic("catalog", ROLE_READER, "Bücher finden", "Finding books", ("books.index", "books.detail")),
    Topic("borrow", ROLE_READER, "Ausleihen und zurückgeben", "Borrowing and returning", ("loans.borrow", "loans.give_back")),
    Topic("my-loans", ROLE_READER, "Meine Ausleihen und Verlängern", "My loans and renewing", ("loans.index", "loans.extend")),
    Topic("profile", ROLE_READER, "Profil, Sprache und Benachrichtigungen", "Profile, language and notifications",
          ("main.profile",)),
    Topic("security", ROLE_READER, "Sicherheit und Zwei-Faktor-Anmeldung", "Security and two-factor sign-in",
          ("security.settings", "security.challenge")),
    Topic("login", ROLE_READER, "Probleme bei der Anmeldung", "Sign-in problems", ("main.login",)),
    # --- Verwalter
    Topic("desk", ROLE_LIBRARIAN, "Ausleihtheke", "Circulation desk", ("loans.desk",)),
    Topic("all-loans", ROLE_LIBRARIAN, "Alle Ausleihen, Mahnungen", "All loans and reminders", ("loans.remind",)),
    Topic("add-book", ROLE_LIBRARIAN, "Bücher erfassen und bearbeiten", "Adding and editing books",
          ("books.create", "books.edit", "books.delete")),
    Topic("quick-capture", ROLE_LIBRARIAN, "Schnellerfassung mit Scanner", "Quick capture with a scanner", ("books.scan",)),
    Topic("scanner", ROLE_LIBRARIAN, "Barcodescanner und Kamera", "Barcode scanner and camera", ()),
    Topic("locations", ROLE_LIBRARIAN, "Standorte", "Locations", ("locations.index",)),
    Topic("export", ROLE_LIBRARIAN, "Katalog exportieren", "Exporting the catalogue", ("books.export_csv",)),
    # --- Administration
    Topic("users", ROLE_ADMIN, "Benutzer und Rollen", "Users and roles", ("admin.users", "admin.user_new", "admin.user_edit")),
    Topic("registration", ROLE_ADMIN, "Selbstregistrierung", "Self-registration", ()),
    Topic("groups", ROLE_ADMIN, "Gruppen", "Groups", ("admin.groups", "admin.group_new", "admin.group_edit")),
    Topic("settings", ROLE_ADMIN, "Einstellungen", "Settings", ("admin.settings",)),
    Topic("twofa-policy", ROLE_ADMIN, "Zwei-Faktor-Regeln und internes Netz", "Two-factor rules and internal network", ()),
    Topic("ldap", ROLE_ADMIN, "Active Directory", "Active Directory", ("admin.ldap",)),
    Topic("email", ROLE_ADMIN, "E-Mail-Versand", "E-mail delivery", ("admin.email",)),
    Topic("branding", ROLE_ADMIN, "Erscheinungsbild", "Branding", ("admin.branding",)),
    Topic("backups", ROLE_ADMIN, "Datensicherung", "Backups", ("admin.backups",)),
    Topic("privacy", ROLE_ADMIN, "Datenschutz und Aufbewahrung", "Privacy and retention", ()),
    Topic("maintenance", ROLE_ADMIN, "Update und Wartung", "Updates and maintenance", ()),
)

MANUALS = {"de": "Handbuch-Catalytix.pdf", "en": "Manual-Catalytix.pdf"}


def _docs_dir() -> Path:
    return Path(current_app.root_path).parent / "docs"


def visible_topics(user) -> list[Topic]:
    rank = RANK.get(getattr(user, "role", ROLE_READER), 0)
    return [t for t in TOPICS if RANK[t.role] <= rank]


def topic_for(endpoint: str | None, user) -> str | None:
    if not endpoint:
        return None
    for t in visible_topics(user):
        if endpoint in t.endpoints:
            return t.id
    return None


def help_url() -> str:
    """Link für den „?“-Knopf: Hilfe zur aktuellen Seite (oder Übersicht)."""
    topic = topic_for(request.endpoint, current_user) if current_user.is_authenticated else None
    if (topic == "my-loans" and request.endpoint == "loans.index" and current_user.is_librarian
            and request.args.get("nur") != "meine"):
        topic = "all-loans"   # Verwalter sehen hier „Alle Ausleihen“
    return url_for("help.index", thema=topic) if topic else url_for("help.index")


def facts() -> dict:
    """Aktuelle Einstellungen, damit die Hilfe den echten Betrieb beschreibt."""
    from .. import backup, twofa
    from ..i18n import LANGUAGES
    from ..models import User
    return {
        "loan_days": get_int("loan_days", 28),
        "extension_days": get_int("extension_days", 14),
        "max_extensions": get_int("max_extensions", 2),
        "max_loans": get_int("max_loans", 0),
        "self_checkout": get_bool("self_checkout"),
        "self_return": get_bool("self_return"),
        "self_extend": get_bool("self_extend"),
        "mail_on": get_bool("mail_enabled") and bool(get_setting("smtp_host")),
        "reminder_days": get_int("mail_reminder_days", 2),
        "reminder_on": get_bool("mail_reminder"),
        "overdue_on": get_bool("mail_overdue"),
        "mfa_internal": twofa.internal_mode(),   # same | optional | skip
        "ldap_on": get_bool("ldap_enabled"),
        "loan_mail": get_bool("mail_on_loan"),
        "https": request.is_secure,   # Kamera und Sicherheitsschlüssel brauchen HTTPS
        "registration_mode": get_setting("registration_mode"),
        "retention_months": get_int("retention_months", 12),
        "catalog_all": get_setting("catalog_visibility") == "all",
        "mfa_required": twofa.required_for(current_user),
        "is_ldap": current_user.auth_source == "ldap",
        "has_2fa": current_user.has_2fa,
        "languages": LANGUAGES,
        # Administration
        "mfa_policy": get_setting("mfa_policy") or "optional",   # optional | admins | librarians | all
        "internal_networks_set": bool((get_setting("internal_networks") or "").strip()),
        "own_public_set": bool((get_setting("own_public_hosts") or "").strip()),
        "ldap_service_account": bool((get_setting("ldap_bind_dn") or "").strip()),
        "ldap_cache": get_setting("ldap_cache_enabled") == "1" and bool((get_setting("ldap_bind_dn") or "").strip()),
        "registration_verify": get_bool("registration_verify_email"),
        "pending_approvals": (User.query.filter(User.pending_approval.is_(True)).count()
                              if current_user.is_admin else 0),
        "mail_hour": get_int("mail_hour", 7),
        "mail_digest": get_bool("mail_digest"),
        "overdue_repeat_days": get_int("mail_overdue_repeat_days", 7),
        "overdue_max": get_int("mail_overdue_max", 3),
        "backup_supported": backup.supported(),
        "backup_enabled": get_bool("backup_enabled"),
        "backup_keep": backup.keep_count(),
        "backup_hour": get_int("backup_hour", 2),
        "default_language": get_setting("default_language") or "de",
        "imprint_set": bool(get_setting("imprint_url")),
        "privacy_set": bool(get_setting("privacy_url")),
    }


@bp.route("/hilfe")
@login_required
def index():
    lang = get_locale()
    topics = visible_topics(current_user)
    focus = request.args.get("thema", "")
    if focus and focus not in {t.id for t in topics}:
        focus = ""
    groups = []
    for role in (ROLE_READER, ROLE_LIBRARIAN, ROLE_ADMIN):
        items = [t for t in topics if t.role == role]
        if items:
            groups.append((GROUPS[role][1 if lang == "en" else 0], items))
    manual = MANUALS.get(lang, MANUALS["de"])
    has_manual = (_docs_dir() / manual).exists()
    return render_template("help/index.html", groups=groups, lang=lang, focus=focus, f=facts(),
                           has_manual=has_manual, manual=manual)


@bp.route("/hilfe/handbuch/<name>")
@login_required
def manual(name):
    if name not in MANUALS.values():
        abort(404)
    return send_from_directory(_docs_dir(), name, mimetype="application/pdf", max_age=3600)
