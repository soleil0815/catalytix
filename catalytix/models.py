"""Catalytix data model."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, backref, relationship
from werkzeug.security import check_password_hash, generate_password_hash

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

db = SQLAlchemy()


def hash_secret(value: str) -> str:
    """Hash for passwords and recovery codes: scrypt. Only in the self-tests (app config TESTING, never set in
    operation) a cheap PBKDF2 – scrypt took more than half of the test time and with it of the CI minutes.
    check_password_hash reads the method from the hash, so both kinds are checked the same way."""
    from flask import current_app, has_app_context
    if has_app_context() and current_app.config.get("TESTING"):
        return generate_password_hash(value, method="pbkdf2:sha256:1000")
    return generate_password_hash(value)

# Base class of all tables. For mypy it is (still) Any: the columns are written in the old db.Column(...) style;
# only with Mapped[...] columns does mypy know their types. The relationships are already typed (Mapped[...] below).
Model: Any = db.Model


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Enable foreign keys and WAL for SQLite."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


MIN_PASSWORD_LENGTH = 10   # for all new passwords (setup, profile, administration, registration)

ROLE_READER = "reader"
ROLE_LIBRARIAN = "librarian"
ROLE_ADMIN = "admin"
from .i18n import _l  # noqa: E402

ROLES = {
    ROLE_READER: _l("Leser"),
    ROLE_LIBRARIAN: _l("Verwalter"),
    ROLE_ADMIN: _l("Administrator"),
}

user_groups = db.Table(
    "user_groups",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class Group(Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.String(500), default="")
    # Optional: DN of an AD group – members are assigned automatically at login
    ldap_dn = db.Column(db.String(500), default="")
    color = db.Column(db.String(16), default="#2f6f62")
    # False = "Präsenzbestand" (German UI term: reference-only stock): the group's books are not lent or reserved
    lendable = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    books: Mapped[list[Book]] = relationship("Book", back_populates="group")
    users: Mapped[list[User]] = relationship("User", secondary=user_groups, back_populates="groups")

    def __repr__(self) -> str:
        return f"<Group {self.name}>"


def _app_version() -> str:
    from . import __version__
    return __version__


class User(UserMixin, Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    password_hash = db.Column(db.String(256))
    auth_source = db.Column(db.String(10), default="local")  # local | ldap
    ldap_dn = db.Column(db.String(500), default="")
    ldap_guid = db.Column(db.String(64), default="", index=True)   # fixed AD identifier (detect renames)
    ldap_gone_at = db.Column(db.DateTime)   # disabled by the AD sync (disabled/deleted/not authorized in AD)
    role = db.Column(db.String(20), default=ROLE_READER, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)
    email_notifications = db.Column(db.Boolean, default=True, nullable=False)
    # Self-registration
    self_registered = db.Column(db.Boolean, default=False, nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    pending_approval = db.Column(db.Boolean, default=False, nullable=False)
    # Two-factor login
    totp_secret = db.Column(db.Text, default="")          # encrypted (Fernet, key derived from SECRET_KEY)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    totp_last_step = db.Column(db.Integer, default=0, nullable=False)  # against reuse of a code
    recovery_codes = db.Column(db.Text, default="")       # JSON list of hashed one-time codes
    # Incremented to invalidate all existing sessions and "Remember me" cookies
    auth_epoch = db.Column(db.Integer, default=0, nullable=False)
    # Language of the UI and e-mails ("" = browser or default language)
    language = db.Column(db.String(5), default="", nullable=False)
    # Up to which version the news has been seen; new accounts start at the current state.
    # Existing accounts get NULL when the column is added (see news.py) – hence a callable as the default.
    news_seen = db.Column(db.String(20), default=lambda: _app_version())
    # Was the account already assigned to a group? Then losing the last group (group deleted,
    # removed in AD) doesn't turn a group librarian into a librarian of the entire collection. Only an admin can do
    # that explicitly, by saving the account without a group.
    group_bound = db.Column(db.Boolean, default=False, nullable=False)
    # 3.3.1: "Don't show again" on the overview's passkey suggestion
    passkey_hint_off = db.Column(db.Boolean, default=False, nullable=False)

    groups: Mapped[list[Group]] = relationship("Group", secondary=user_groups, back_populates="users")
    loans: Mapped[list[Loan]] = relationship("Loan", back_populates="user", foreign_keys="Loan.user_id")
    reservations: Mapped[list[Reservation]] = relationship("Reservation", back_populates="user",
                                                              cascade="all, delete-orphan")

    # --- Flask-Login ---
    @property
    def is_active(self) -> bool:  # noqa: D401
        return bool(self.active)

    def get_id(self) -> str:
        # The ID contains the "session generation": incrementing it immediately invalidates all old sessions
        # and remember-me cookies of this account.
        return f"{self.id}:{self.auth_epoch or 0}"

    def end_all_sessions(self) -> None:
        self.auth_epoch = (self.auth_epoch or 0) + 1

    # --- Passwords ---
    def set_password(self, password: str) -> None:
        self.password_hash = hash_secret(password)
        if self.id is not None:   # existing account: log out other devices
            self.end_all_sessions()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    # --- Roles ---
    @property
    def is_admin(self) -> bool:
        return bool(self.role == ROLE_ADMIN)

    @property
    def is_librarian(self) -> bool:
        return self.role in (ROLE_LIBRARIAN, ROLE_ADMIN)

    @property
    def role_label(self) -> str:
        return str(ROLES.get(self.role, self.role))

    @property
    def name(self) -> str:
        return str(self.display_name or self.username)

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def group_ids(self) -> set[int]:
        return {g.id for g in self.groups}

    @property
    def is_global_manager(self) -> bool:
        """Admins and librarians without a group assignment manage the entire collection."""
        return self.is_admin or (self.role == ROLE_LIBRARIAN and not self.groups and not self.group_bound)

    def can_see(self, book: "Book", catalog_all: bool = False) -> bool:
        if catalog_all or self.is_global_manager or book.group_id is None:
            return True
        return book.group_id in self.group_ids

    def can_borrow(self, book: "Book") -> bool:
        """Readers may only borrow books of their groups (or general books)."""
        if book.group_id is None or self.is_global_manager:
            return True
        return book.group_id in self.group_ids

    def can_manage(self, book: "Book | None" = None) -> bool:
        if not self.is_librarian:
            return False
        if self.is_global_manager or book is None or book.group_id is None:
            return True
        return book.group_id in self.group_ids

    def manageable_groups(self) -> list["Group"]:
        if self.is_global_manager:
            groups: list[Group] = Group.query.order_by(Group.name).all()
            return groups
        return sorted(self.groups, key=lambda g: g.name.lower())

    @property
    def active_loans(self) -> list[Loan]:
        return [l for l in self.loans if l.returned_at is None]

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_enabled) or bool(self.webauthn_credentials)


class Book(Model):
    __tablename__ = "books"
    id = db.Column(db.Integer, primary_key=True)
    isbn = db.Column(db.String(20), index=True, default="")
    title = db.Column(db.String(500), nullable=False)
    subtitle = db.Column(db.String(500), default="")
    authors = db.Column(db.String(500), default="")
    publisher = db.Column(db.String(300), default="")
    published = db.Column(db.String(30), default="")
    edition = db.Column(db.String(100), default="")
    pages = db.Column(db.Integer)
    language = db.Column(db.String(30), default="")
    category = db.Column(db.String(200), default="")
    tags = db.Column(db.String(500), default="")
    description = db.Column(db.Text, default="")
    location = db.Column(db.String(200), default="")  # location / shelf
    signature = db.Column(db.String(100), default="")  # call number / inventory number
    cover_url = db.Column(db.String(1000), default="")
    cover_file = db.Column(db.String(200), default="")
    copies = db.Column(db.Integer, default=1, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    # Number of earlier loans whose individual data was deleted after the retention period expired
    archived_loans = db.Column(db.Integer, default=0, nullable=False)

    group: Mapped[Group | None] = relationship("Group", back_populates="books")
    loans: Mapped[list[Loan]] = relationship("Loan", back_populates="book", cascade="all, delete-orphan",
                                                order_by="Loan.loaned_at.desc()")
    reservations: Mapped[list[Reservation]] = relationship(
        "Reservation", back_populates="book", cascade="all, delete-orphan",
        order_by="Reservation.created_at, Reservation.id")

    @property
    def active_loans(self) -> list[Loan]:
        return [l for l in self.loans if l.returned_at is None]

    @property
    def open_reservations(self) -> list[Reservation]:
        """Reservations in the queue and those on hold – in queue order."""
        return [r for r in self.reservations if r.status in (RES_WAITING, RES_READY)]

    @property
    def held(self) -> int:
        """Copies held for a reservation."""
        return sum(1 for r in self.reservations if r.status == RES_READY)

    @property
    def lendable(self) -> bool:
        """False if the book belongs to a group without lending ("Präsenzbestand", German UI term: reference-only)."""
        return not (self.group and self.group.lendable is False)

    @property
    def available(self) -> int:
        """Available for everyone: copies minus loans minus held copies."""
        return max(0, (self.copies or 0) - len(self.active_loans) - self.held)

    def available_for(self, user: object) -> int:
        """Available for this person – a copy held for them counts as available."""
        mine = any(r.status == RES_READY and r.user_id == getattr(user, "id", None) for r in self.reservations)
        return self.available + (1 if mine else 0)

    @property
    def cover(self) -> str:
        from flask import url_for
        if self.cover_file:
            return url_for("books.cover", filename=self.cover_file)
        if self.cover_url and self.id:
            # never directly from the external server – our own server downloads the image once and stores it
            return url_for("books.cover_remote", book_id=self.id)
        return ""

    @property
    def spine_hue(self) -> int:
        return sum(ord(c) for c in (self.title or "x")) % 360


class Loan(Model):
    __tablename__ = "loans"
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    returned_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    loaned_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    due_at = db.Column(db.Date, nullable=False)
    returned_at = db.Column(db.DateTime)
    extensions = db.Column(db.Integer, default=0, nullable=False)
    # Time of the last due-date change (renewal): reminders and overdue notices before it no longer count
    due_changed_at = db.Column(db.DateTime)
    note = db.Column(db.String(500), default="")

    # At most one open loan per book and person (protection against concurrent requests).
    # Partial indexes exist only in SQLite and PostgreSQL; MySQL/MariaDB relies on the row lock.
    __table_args__ = (
        db.Index("uq_loans_active_book_user", "book_id", "user_id", unique=True,
                 sqlite_where=db.text("returned_at IS NULL"),
                 postgresql_where=db.text("returned_at IS NULL")).ddl_if(dialect=("sqlite", "postgresql")),
    )

    book: Mapped[Book] = relationship("Book", back_populates="loans")
    user: Mapped[User] = relationship("User", back_populates="loans", foreign_keys=[user_id])
    issued_by: Mapped[User | None] = relationship("User", foreign_keys=[issued_by_id])
    returned_by: Mapped[User | None] = relationship("User", foreign_keys=[returned_by_id])

    @property
    def is_active(self) -> bool:
        return self.returned_at is None

    @property
    def is_overdue(self) -> bool:
        return self.returned_at is None and self.due_at < date.today()

    @property
    def days_left(self) -> int:
        days: int = (self.due_at - date.today()).days
        return days


class WebAuthnCredential(Model):
    """A user's security key (YubiKey & co.) or passkey."""
    __tablename__ = "webauthn_credentials"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), default="Sicherheitsschlüssel")
    rp_id = db.Column(db.String(253), nullable=False)          # domain the key is valid for
    credential_id = db.Column(db.String(1400), nullable=False, index=True)  # websafe-base64
    credential_data = db.Column(db.Text, nullable=False)       # AttestedCredentialData (base64)
    sign_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_used_at = db.Column(db.DateTime)
    # 3.3.0: registered as a passkey (discoverable, user verification required) – also signs in without a password
    passkey = db.Column(db.Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship("User", backref=backref("webauthn_credentials",
                                                                     cascade="all, delete-orphan"))


class MailLog(Model):
    """Log of all sent (or failed) e-mails – also used to avoid
    sending reminders twice."""
    __tablename__ = "mail_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False, index=True)  # loan|return|reminder|overdue|digest|manual|test
    to_addr = db.Column(db.String(300), default="")
    subject = db.Column(db.String(300), default="")
    status = db.Column(db.String(10), default="sent")  # sent|error|skipped
    error = db.Column(db.String(1000), default="")
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id", ondelete="SET NULL"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))


class Location(Model):
    """Predefined location (shelf, room …) – for selection when cataloging, against typos."""
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class LoginAttempt(Model):
    """Failed login attempt (for the temporary lockout, see lockout.py)."""
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(300), nullable=False, index=True)       # e.g. "anna|203.0.113.5" or "2fa|anna|…"
    username = db.Column(db.String(200), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    ip = db.Column(db.String(64), default="", index=True)          # address (throttle per address across all accounts)
    internal = db.Column(db.Boolean, default=False)                 # from the internal network?


class DirectoryPerson(Model):
    """Cache of AD people allowed to use the library – only for fast searching when lending.

    Synced in the background (people.sync_directory). People disabled, deleted or no longer
    authorized in AD are removed in the process. Lending never relies on this entry: for that,
    Catalytix always queries AD fresh."""
    __tablename__ = "directory_people"
    id = db.Column(db.Integer, primary_key=True)
    username_key = db.Column(db.String(200), nullable=False, unique=True)   # username in lowercase
    username = db.Column(db.String(200), nullable=False)
    guid = db.Column(db.String(64), default="")
    display_name = db.Column(db.String(300), default="")
    email = db.Column(db.String(300), default="")
    dn = db.Column(db.String(1000), default="")
    role = db.Column(db.String(20), default="")
    groups = db.Column(db.Text, default="")          # group DNs, one per line
    group_key = db.Column(db.String(64), default="")  # checksum of direct groups – unchanged = don't re-resolve
    resolved_at = db.Column(db.DateTime)             # last full (nested) group resolution
    synced_at = db.Column(db.DateTime, default=datetime.now)


RES_WAITING, RES_READY, RES_DONE, RES_CANCELLED, RES_EXPIRED = "waiting", "ready", "done", "cancelled", "expired"


class Reservation(Model):
    """Reservation: queue per book. When a copy becomes available, it is held for the first person
    (ready, until hold_until); once it is lent to them, the reservation is done (done)."""
    __tablename__ = "reservations"
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    status = db.Column(db.String(12), default=RES_WAITING, nullable=False, index=True)
    ready_at = db.Column(db.DateTime)
    hold_until = db.Column(db.Date)          # held up to and including this day
    closed_at = db.Column(db.DateTime)       # done, cancelled or expired

    book: Mapped[Book] = relationship("Book", back_populates="reservations")
    user: Mapped[User] = relationship("User", back_populates="reservations")

    @property
    def is_open(self) -> bool:
        return self.status in (RES_WAITING, RES_READY)


class Setting(Model):
    __tablename__ = "settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")


DEFAULT_SETTINGS = {
    "library_name": "Bibliothek",
    "default_language": "de",   # language for visitors without their own choice and for e-mails
    "loan_days": "28",
    "extension_days": "14",
    "max_extensions": "2",
    "max_loans": "0",
    "self_checkout": "1",
    "self_return": "0",
    "self_extend": "1",
    "reservations_enabled": "0",      # reserving books that are on loan
    "reservation_hold_days": "7",     # how long a copy that became available is held for the person
    "reservation_max": "5",           # open reservations per person (0 = unlimited)
    "catalog_visibility": "groups",  # groups | all
    "isbn_sources": "dnb,openlibrary,google",
    "reading_sample_google": "0",    # reading samples from Google Books (reading_sample.py) – off by default
    "passkey_login": "1",            # sign-in with a passkey instead of a password (twofa.py) – on since 3.3.2
    "cache_covers": "1",
    # Active Directory / LDAP
    "ldap_enabled": "0",
    "ldap_server": "",
    "ldap_security": "none",  # none | starttls | ldaps
    "ldap_verify_cert": "1",
    "ldap_ca_file": "",
    "ldap_tls_certs": "",       # certificates fetched from the DC and confirmed (PEM)
    "ldap_check_hostname": "1",
    "ldap_bind_dn": "",
    "ldap_bind_password": "",
    "ldap_upn_suffix": "",
    "ldap_base_dn": "",
    "ldap_user_attr": "sAMAccountName",
    "ldap_user_filter": "(objectClass=user)",
    "ldap_login_rules": "",
    "ldap_librarian_rules": "",
    "ldap_admin_rules": "",
    "ldap_nested_groups": "1",
    "ldap_group_sync": "1",
    "ldap_timeout": "8",
    "ldap_cache_enabled": "1",  # cache AD people for search (synced in the background)
    "ldap_cache_hours": "1",    # sync every X hours
    # E-mail (SMTP)
    "mail_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",  # none | starttls | ssl
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_from_name": "",
    "smtp_reply_to": "",
    "smtp_verify_cert": "1",
    "app_base_url": "",
    "mail_on_loan": "1",
    "mail_on_return": "0",
    "mail_reminder": "1",
    "mail_reminder_days": "2",
    "mail_overdue": "1",
    "mail_overdue_repeat_days": "7",
    "mail_overdue_max": "3",
    "mail_digest": "0",
    "mail_hour": "7",
    "backup_enabled": "1",     # daily backup (SQLite only)
    "backup_keep": "10",       # number of backups kept
    "backup_hour": "2",        # from this hour on
    "retention_months": "12",  # delete completed loans and mail log after X months; 0 = never
    "session_admin_minutes": "60",   # automatic sign-out for admins (minutes, "" = as for everyone)
    "session_external_minutes": "",  # automatic sign-out for sign-ins from outside (minutes, "" = off)
    "session_warning": "1",          # warn 2 minutes before the automatic sign-out
    "session_browser_close": "0",    # sign out when the browser is closed (no "stay signed in")
    "mfa_internal": "same",    # two-factor on the internal network: same | optional | skip
    "internal_networks": "",   # empty = private networks (10/8, 172.16/12, 192.168/16, fc00::/7)
    "own_public_hosts": "",    # own connection: static IP or DynDNS name, also counts as internal
    "mail_footer": "",
    "mail_last_run": "",
    "mail_last_result": "",
    # Two-factor login: optional | admins | librarians | all
    "mfa_policy": "optional",
    # Self-registration: off | approval | open
    "registration_mode": "off",
    "registration_verify_email": "1",
    "registration_domains": "",
    "registration_group_id": "",
    "registration_privacy_url": "",  # up to 2.0; carried over into privacy_url
    "imprint_url": "",
    "privacy_url": "",
    "registration_notify_admins": "1",
    # Appearance
    "brand_logo": "",
    "brand_favicon": "",
    "brand_bg": "",
    "brand_bg_dim": "45",
    "brand_logo_text": "1",
    "brand_logo_plate": "0",
    "brand_accent": "",
    "brand_brass": "",
    "brand_side": "",
    "login_show_shelf": "1",
    "login_eyebrow": "Willkommen",
    "login_text": "Stöbern, ausleihen, weiterlesen – der Katalog Ihrer Bibliothek.",
}


# Credentials that are stored only encrypted in the database (and thus in backups)
SECRET_SETTINGS = {"ldap_bind_password", "smtp_password", "google_api_key"}
_ENC = "enc:"


def _secret_box() -> Fernet:
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    from flask import current_app
    key = hashlib.sha256(("settings-secret:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decrypt_setting(key: str, value: str) -> str:
    if not value.startswith(_ENC):
        return value          # legacy data – encrypted at startup
    try:
        return str(_secret_box().decrypt(value[len(_ENC):].encode()).decode())
    except Exception:  # noqa: BLE001 – e.g. a different session key
        import logging
        logging.getLogger(__name__).warning("Stored secret %s not readable – please enter it again", key)
        return ""


def encrypt_stored_secrets() -> int:
    """Encrypt credentials still stored in plain text (once after the update)."""
    n = 0
    for key in SECRET_SETTINGS:
        row = db.session.get(Setting, key)
        if row is not None and row.value and not row.value.startswith(_ENC):
            set_setting(key, row.value)
            n += 1
    if n:
        db.session.commit()
    return n


def get_setting(key: str, default: str | None = None) -> str:
    row = db.session.get(Setting, key)
    if row is not None:
        if key in SECRET_SETTINGS and row.value:
            return _decrypt_setting(key, row.value)
        return row.value or ""
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key, "")


def get_bool(key: str) -> bool:
    return get_setting(key) in ("1", "true", "on", "yes")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return fallback


def set_setting(key: str, value: object) -> None:
    if isinstance(value, bool):
        value = "1" if value else "0"
    if key in SECRET_SETTINGS and value:
        value = _ENC + _secret_box().encrypt(str(value).encode()).decode()
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


def default_due_date() -> date:
    return date.today() + timedelta(days=get_int("loan_days", 28))


def visible_books_query(user: User) -> Any:
    """Query of the books a user may see in the catalog."""
    q = Book.query
    if user.is_global_manager or get_setting("catalog_visibility") == "all":
        return q
    ids = list(user.group_ids)
    return q.filter((Book.group_id.is_(None)) | (Book.group_id.in_(ids or [-1])))


def manageable_books_query(user: User) -> Any:
    q = Book.query
    if user.is_global_manager:
        return q
    ids = list(user.group_ids)
    return q.filter((Book.group_id.is_(None)) | (Book.group_id.in_(ids or [-1])))


def occupied_counts() -> Any:
    """Subquery book_id → n: open loans plus held reservations per book."""
    from sqlalchemy import literal, union_all
    loans = db.session.query(Loan.book_id.label("book_id"), literal(1).label("one")).filter(Loan.returned_at.is_(None))
    held = db.session.query(Reservation.book_id.label("book_id"), literal(1).label("one")).filter(
        Reservation.status == RES_READY)
    both = union_all(loans.statement, held.statement).subquery()
    return (db.session.query(both.c.book_id.label("book_id"), func.count().label("n"))
            .group_by(both.c.book_id).subquery())


def count_active_loans(user_id: int) -> int:
    return db.session.query(func.count(Loan.id)).filter(
        Loan.user_id == user_id, Loan.returned_at.is_(None)
    ).scalar() or 0


@event.listens_for(User.groups, "append")
def _mark_group_bound(user: User, _group: Group, _initiator: Any) -> None:
    user.group_bound = True


def _add_column_ddl(table: Any, col: Any, dialect: Any) -> list[str]:
    """SQL for adding a column (identifiers correctly quoted, e.g. `groups`/"key")."""
    from sqlalchemy import literal

    prep = dialect.identifier_preparer
    tname, cname = prep.format_table(table), prep.format_column(col)
    coltype = col.type.compile(dialect)
    ddl = f"ALTER TABLE {tname} ADD COLUMN {cname} {coltype}"
    default = getattr(col.default, "arg", None)
    if default is None or callable(default):
        return [ddl]
    lit = literal(default, col.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    if dialect.name in ("mysql", "mariadb") and any(t in coltype.upper() for t in ("TEXT", "BLOB", "JSON")):
        # MySQL allows no DEFAULT value for TEXT/BLOB: create the column without one, then fill existing rows
        return [ddl, f"UPDATE {tname} SET {cname} = {lit}"]
    ddl += f" DEFAULT {lit}"
    if not col.nullable:
        ddl += " NOT NULL"
    return [ddl]


ACTIVE_LOAN_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_loans_active_book_user "
                     "ON loans (book_id, user_id) WHERE returned_at IS NULL")


LATE_INDEXES = {"ix_users_ldap_guid", "ix_login_attempts_ip"}   # indexes of new columns, added to existing DBs at start


def ensure_indexes() -> bool:
    """Add indexes that db.create_all() does not create for existing tables.

    Returns False if the index could not be created because of existing duplicate open loans
    – then a warning in the log is all that happens, and startup continues."""
    import logging

    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    # Indexes on columns only added via ensure_columns (targeted – don't rebuild old indexes at startup)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        present = {ix["name"] for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in LATE_INDEXES or index.name in present:
                continue
            try:
                index.create(db.engine)
            except Exception as exc:  # noqa: BLE001 – e.g. created at the same time by another process
                logging.getLogger(__name__).debug("Index %s not created: %s", index.name, exc)

    if db.engine.dialect.name not in ("sqlite", "postgresql") or not insp.has_table("loans"):
        return True
    try:
        with db.engine.begin() as conn:
            conn.execute(text(ACTIVE_LOAN_INDEX))
    except Exception as exc:  # noqa: BLE001 – e.g. IntegrityError with legacy data
        logging.getLogger(__name__).warning(
            "Unique index for open loans not created (duplicate open loans present?): %s", exc)
        return False
    return True


def lendable_books() -> Any:
    """SQL condition: books without a group or in a group that lends (not reference-only stock)."""
    from sqlalchemy import or_, select
    return or_(Book.group_id.is_(None),
               ~Book.group_id.in_(select(Group.id).where(Group.lendable.is_(False))))


def ensure_columns() -> list[str]:
    """Simple schema update: add missing columns to existing tables.

    db.create_all() only creates new tables. If a new version adds columns,
    they are added here via ALTER TABLE (SQLite, MySQL/MariaDB, PostgreSQL).
    Each column in its own transaction; if another process has just created it
    ("duplicate column"), it is skipped."""
    import logging
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    added = []
    insp = inspect(db.engine)
    dialect = db.engine.dialect
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            try:
                with db.engine.begin() as conn:
                    for stmt in _add_column_ddl(table, col, dialect):
                        conn.execute(text(stmt))
            except DBAPIError as exc:
                msg = str(exc.orig if exc.orig is not None else exc).lower()
                if "duplicate column" in msg or ("column" in msg and "already exists" in msg):
                    logging.getLogger(__name__).debug("Column %s.%s already exists", table.name, col.name)
                    continue
                raise
            added.append(f"{table.name}.{col.name}")
    return added
