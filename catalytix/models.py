"""Datenmodell von Catalytix."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """Fremdschlüssel und WAL für SQLite aktivieren."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


MIN_PASSWORD_LENGTH = 10   # für alle neuen Passwörter (Einrichtung, Profil, Verwaltung, Registrierung)

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


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.String(500), default="")
    # Optional: DN einer AD-Gruppe – Mitglieder werden beim Login automatisch zugeordnet
    ldap_dn = db.Column(db.String(500), default="")
    color = db.Column(db.String(16), default="#2f6f62")
    created_at = db.Column(db.DateTime, default=datetime.now)

    books = db.relationship("Book", back_populates="group")
    users = db.relationship("User", secondary=user_groups, back_populates="groups")

    def __repr__(self):
        return f"<Group {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    password_hash = db.Column(db.String(256))
    auth_source = db.Column(db.String(10), default="local")  # local | ldap
    ldap_dn = db.Column(db.String(500), default="")
    ldap_guid = db.Column(db.String(64), default="", index=True)   # feste AD-Kennung (Umbenennungen erkennen)
    ldap_gone_at = db.Column(db.DateTime)   # vom AD-Abgleich gesperrt (im AD deaktiviert/gelöscht/nicht berechtigt)
    role = db.Column(db.String(20), default=ROLE_READER, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)
    email_notifications = db.Column(db.Boolean, default=True, nullable=False)
    # Selbstregistrierung
    self_registered = db.Column(db.Boolean, default=False, nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    pending_approval = db.Column(db.Boolean, default=False, nullable=False)
    # Zwei-Faktor-Anmeldung
    totp_secret = db.Column(db.Text, default="")          # verschlüsselt (Fernet, Schlüssel aus SECRET_KEY)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    totp_last_step = db.Column(db.Integer, default=0, nullable=False)  # gegen Wiederverwendung eines Codes
    recovery_codes = db.Column(db.Text, default="")       # JSON-Liste gehashter Einmal-Codes
    # Wird erhöht, um alle bestehenden Sitzungen und „Angemeldet bleiben“-Cookies ungültig zu machen
    auth_epoch = db.Column(db.Integer, default=0, nullable=False)
    # Sprache der Oberfläche und der E-Mails ("" = Browser bzw. Standardsprache)
    language = db.Column(db.String(5), default="", nullable=False)

    groups = db.relationship("Group", secondary=user_groups, back_populates="users")
    loans = db.relationship("Loan", back_populates="user", foreign_keys="Loan.user_id")

    # --- Flask-Login ---
    @property
    def is_active(self):  # noqa: D401
        return bool(self.active)

    def get_id(self) -> str:
        # Die Kennung enthält die „Sitzungs-Generation“: erhöht man sie, sind alle alten Sitzungen
        # und Erinnerungs-Cookies dieses Kontos sofort ungültig.
        return f"{self.id}:{self.auth_epoch or 0}"

    def end_all_sessions(self):
        self.auth_epoch = (self.auth_epoch or 0) + 1

    # --- Passwörter ---
    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)
        if self.id is not None:   # bestehendes Konto: andere Geräte abmelden
            self.end_all_sessions()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    # --- Rollen ---
    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_librarian(self) -> bool:
        return self.role in (ROLE_LIBRARIAN, ROLE_ADMIN)

    @property
    def role_label(self) -> str:
        return str(ROLES.get(self.role, self.role))

    @property
    def name(self) -> str:
        return self.display_name or self.username

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def group_ids(self) -> set[int]:
        return {g.id for g in self.groups}

    @property
    def is_global_manager(self) -> bool:
        """Admins und Verwalter ohne Gruppenzuordnung verwalten den gesamten Bestand."""
        return self.is_admin or (self.role == ROLE_LIBRARIAN and not self.groups)

    def can_see(self, book: "Book", catalog_all: bool = False) -> bool:
        if catalog_all or self.is_global_manager or book.group_id is None:
            return True
        return book.group_id in self.group_ids

    def can_borrow(self, book: "Book") -> bool:
        """Leser dürfen nur Bücher ihrer Gruppen (oder allgemeine Bücher) ausleihen."""
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
            return Group.query.order_by(Group.name).all()
        return sorted(self.groups, key=lambda g: g.name.lower())

    @property
    def active_loans(self):
        return [l for l in self.loans if l.returned_at is None]

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_enabled) or bool(self.webauthn_credentials)


class Book(db.Model):
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
    location = db.Column(db.String(200), default="")  # Standort / Regal
    signature = db.Column(db.String(100), default="")  # Signatur / Inventarnummer
    cover_url = db.Column(db.String(1000), default="")
    cover_file = db.Column(db.String(200), default="")
    copies = db.Column(db.Integer, default=1, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    # Anzahl früherer Ausleihen, deren Einzeldaten nach Ablauf der Aufbewahrungsfrist gelöscht wurden
    archived_loans = db.Column(db.Integer, default=0, nullable=False)

    group = db.relationship("Group", back_populates="books")
    loans = db.relationship("Loan", back_populates="book", cascade="all, delete-orphan",
                            order_by="Loan.loaned_at.desc()")

    @property
    def active_loans(self):
        return [l for l in self.loans if l.returned_at is None]

    @property
    def available(self) -> int:
        return max(0, (self.copies or 0) - len(self.active_loans))

    @property
    def cover(self) -> str:
        from flask import url_for
        if self.cover_file:
            return url_for("books.cover", filename=self.cover_file)
        if self.cover_url and self.id:
            # nie direkt vom Fremdserver – der eigene Server lädt das Bild einmal und speichert es
            return url_for("books.cover_remote", book_id=self.id)
        return ""

    @property
    def spine_hue(self) -> int:
        return sum(ord(c) for c in (self.title or "x")) % 360


class Loan(db.Model):
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
    note = db.Column(db.String(500), default="")

    # Höchstens eine offene Ausleihe je Buch und Person (Schutz vor gleichzeitigen Anfragen).
    # Teil-Indizes gibt es nur in SQLite und PostgreSQL; MySQL/MariaDB verlässt sich auf die Zeilensperre.
    __table_args__ = (
        db.Index("uq_loans_active_book_user", "book_id", "user_id", unique=True,
                 sqlite_where=db.text("returned_at IS NULL"),
                 postgresql_where=db.text("returned_at IS NULL")).ddl_if(dialect=("sqlite", "postgresql")),
    )

    book = db.relationship("Book", back_populates="loans")
    user = db.relationship("User", back_populates="loans", foreign_keys=[user_id])
    issued_by = db.relationship("User", foreign_keys=[issued_by_id])
    returned_by = db.relationship("User", foreign_keys=[returned_by_id])

    @property
    def is_active(self) -> bool:
        return self.returned_at is None

    @property
    def is_overdue(self) -> bool:
        return self.returned_at is None and self.due_at < date.today()

    @property
    def days_left(self) -> int:
        return (self.due_at - date.today()).days


class WebAuthnCredential(db.Model):
    """Sicherheitsschlüssel (YubiKey & Co.) bzw. Passkey eines Benutzers."""
    __tablename__ = "webauthn_credentials"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), default="Sicherheitsschlüssel")
    rp_id = db.Column(db.String(253), nullable=False)          # Domain, für die der Schlüssel gilt
    credential_id = db.Column(db.String(1400), nullable=False, index=True)  # websafe-base64
    credential_data = db.Column(db.Text, nullable=False)       # AttestedCredentialData (base64)
    sign_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_used_at = db.Column(db.DateTime)

    user = db.relationship("User", backref=db.backref("webauthn_credentials", cascade="all, delete-orphan"))


class MailLog(db.Model):
    """Protokoll aller versendeten (oder fehlgeschlagenen) E-Mails – dient auch dazu,
    Erinnerungen nicht doppelt zu verschicken."""
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


class Location(db.Model):
    """Festgelegter Standort (Regal, Raum …) – zur Auswahl beim Erfassen, gegen Tippfehler."""
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class LoginAttempt(db.Model):
    """Fehlgeschlagener Anmeldeversuch (für die zeitweise Sperre, siehe lockout.py)."""
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(300), nullable=False, index=True)       # z. B. "anna|203.0.113.5" oder "2fa|anna|…"
    username = db.Column(db.String(200), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    ip = db.Column(db.String(64), default="", index=True)          # Adresse (Bremse je Adresse über alle Konten)
    internal = db.Column(db.Boolean, default=False)                 # aus dem internen Netz?


class DirectoryPerson(db.Model):
    """Zwischenspeicher der AD-Personen, die die Bibliothek nutzen dürfen – nur für die schnelle Suche beim Verleihen.

    Wird im Hintergrund abgeglichen (people.sync_directory). Im AD deaktivierte, gelöschte oder nicht mehr
    berechtigte Personen fliegen dabei heraus. Verliehen wird nie aufgrund dieses Eintrags: dafür fragt
    Catalytix das AD immer frisch."""
    __tablename__ = "directory_people"
    id = db.Column(db.Integer, primary_key=True)
    username_key = db.Column(db.String(200), nullable=False, unique=True)   # Benutzername in Kleinbuchstaben
    username = db.Column(db.String(200), nullable=False)
    guid = db.Column(db.String(64), default="")
    display_name = db.Column(db.String(300), default="")
    email = db.Column(db.String(300), default="")
    dn = db.Column(db.String(1000), default="")
    role = db.Column(db.String(20), default="")
    groups = db.Column(db.Text, default="")          # Gruppen-DNs, eine pro Zeile
    group_key = db.Column(db.String(64), default="")  # Prüfsumme der direkten Gruppen – unverändert = nicht neu auflösen
    resolved_at = db.Column(db.DateTime)             # letzte vollständige (verschachtelte) Gruppenauflösung
    synced_at = db.Column(db.DateTime, default=datetime.now)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")


DEFAULT_SETTINGS = {
    "library_name": "Bibliothek",
    "default_language": "de",   # Sprache für Besucher ohne eigene Wahl und für E-Mails
    "loan_days": "28",
    "extension_days": "14",
    "max_extensions": "2",
    "max_loans": "0",
    "self_checkout": "1",
    "self_return": "0",
    "self_extend": "1",
    "catalog_visibility": "groups",  # groups | all
    "isbn_sources": "dnb,openlibrary,google",
    "cache_covers": "1",
    # Active Directory / LDAP
    "ldap_enabled": "0",
    "ldap_server": "",
    "ldap_security": "none",  # none | starttls | ldaps
    "ldap_verify_cert": "1",
    "ldap_ca_file": "",
    "ldap_tls_certs": "",       # vom DC abgerufene und bestätigte Zertifikate (PEM)
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
    "ldap_cache_enabled": "1",  # AD-Personen für die Suche zwischenspeichern (Abgleich im Hintergrund)
    "ldap_cache_hours": "1",    # Abgleich alle X Stunden
    # E-Mail (SMTP)
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
    "backup_enabled": "1",     # tägliche Datensicherung (nur SQLite)
    "backup_keep": "10",       # Anzahl aufbewahrter Sicherungen
    "backup_hour": "2",        # ab dieser Uhrzeit
    "retention_months": "12",  # abgeschlossene Ausleihen und Mail-Protokoll nach X Monaten löschen; 0 = nie
    "mfa_internal": "same",    # Zwei-Faktor im internen Netz: same | optional | skip
    "internal_networks": "",   # leer = private Netze (10/8, 172.16/12, 192.168/16, fc00::/7)
    "own_public_hosts": "",    # eigener Anschluss: feste IP oder DynDNS-Name, gilt ebenfalls als intern
    "mail_footer": "",
    "mail_last_run": "",
    "mail_last_result": "",
    # Zwei-Faktor-Anmeldung: optional | admins | librarians | all
    "mfa_policy": "optional",
    # Selbstregistrierung: off | approval | open
    "registration_mode": "off",
    "registration_verify_email": "1",
    "registration_domains": "",
    "registration_group_id": "",
    "registration_privacy_url": "",  # bis 2.0; wird in privacy_url übernommen
    "imprint_url": "",
    "privacy_url": "",
    "registration_notify_admins": "1",
    # Erscheinungsbild
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
    "login_text": "Stöbern, ausleihen, weiterlesen – der Katalog deiner Bibliothek.",
}


# Zugangsdaten, die nur verschlüsselt in der Datenbank (und damit in Sicherungen) stehen
SECRET_SETTINGS = {"ldap_bind_password", "smtp_password"}
_ENC = "enc:"


def _secret_box():
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    from flask import current_app
    key = hashlib.sha256(("settings-secret:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decrypt_setting(key: str, value: str) -> str:
    if not value.startswith(_ENC):
        return value          # Altbestand – wird beim Start verschlüsselt
    try:
        return _secret_box().decrypt(value[len(_ENC):].encode()).decode()
    except Exception:  # noqa: BLE001 – z. B. anderer Sitzungsschlüssel
        import logging
        logging.getLogger(__name__).warning("Stored secret %s not readable – please enter it again", key)
        return ""


def encrypt_stored_secrets() -> int:
    """Noch im Klartext gespeicherte Zugangsdaten verschlüsseln (einmalig nach dem Update)."""
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


def set_setting(key: str, value) -> None:
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


def visible_books_query(user: User):
    """Query der Bücher, die ein Benutzer im Katalog sehen darf."""
    q = Book.query
    if user.is_global_manager or get_setting("catalog_visibility") == "all":
        return q
    ids = list(user.group_ids)
    return q.filter((Book.group_id.is_(None)) | (Book.group_id.in_(ids or [-1])))


def manageable_books_query(user: User):
    q = Book.query
    if user.is_global_manager:
        return q
    ids = list(user.group_ids)
    return q.filter((Book.group_id.is_(None)) | (Book.group_id.in_(ids or [-1])))


def count_active_loans(user_id: int) -> int:
    return db.session.query(func.count(Loan.id)).filter(
        Loan.user_id == user_id, Loan.returned_at.is_(None)
    ).scalar() or 0


def _add_column_ddl(table, col, dialect) -> list[str]:
    """SQL zum Nachrüsten einer Spalte (Bezeichner korrekt gequotet, z. B. `groups`/"key")."""
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
        # MySQL erlaubt für TEXT/BLOB keinen DEFAULT-Wert: Spalte ohne anlegen, Bestand danach füllen
        return [ddl, f"UPDATE {tname} SET {cname} = {lit}"]
    ddl += f" DEFAULT {lit}"
    if not col.nullable:
        ddl += " NOT NULL"
    return [ddl]


ACTIVE_LOAN_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_loans_active_book_user "
                     "ON loans (book_id, user_id) WHERE returned_at IS NULL")


LATE_INDEXES = {"ix_users_ldap_guid", "ix_login_attempts_ip"}   # Indizes neuer Spalten, die bestehende Datenbanken beim Start bekommen


def ensure_indexes() -> bool:
    """Indizes nachziehen, die db.create_all() bei bestehenden Tabellen nicht anlegt.

    Liefert False, wenn der Index wegen vorhandener doppelter offener Ausleihen nicht angelegt werden
    konnte – dann bleibt es bei einer Warnung im Protokoll, der Start läuft weiter."""
    import logging

    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    # Indizes auf Spalten, die erst per ensure_columns dazukamen (gezielt – keine alten Indizes beim Start nachbauen)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        present = {ix["name"] for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in LATE_INDEXES or index.name in present:
                continue
            try:
                index.create(db.engine)
            except Exception as exc:  # noqa: BLE001 – z. B. gleichzeitig von einem anderen Prozess angelegt
                logging.getLogger(__name__).debug("Index %s not created: %s", index.name, exc)

    if db.engine.dialect.name not in ("sqlite", "postgresql") or not insp.has_table("loans"):
        return True
    try:
        with db.engine.begin() as conn:
            conn.execute(text(ACTIVE_LOAN_INDEX))
    except Exception as exc:  # noqa: BLE001 – z. B. IntegrityError bei Altdaten
        logging.getLogger(__name__).warning(
            "Unique index for open loans not created (duplicate open loans present?): %s", exc)
        return False
    return True


def ensure_columns() -> list[str]:
    """Einfache Schema-Aktualisierung: fehlende Spalten bestehender Tabellen ergänzen.

    db.create_all() legt nur neue Tabellen an. Kommen in einer neuen Version Spalten hinzu,
    werden sie hier per ALTER TABLE nachgezogen (SQLite, MySQL/MariaDB, PostgreSQL).
    Jede Spalte in eigener Transaktion; hat ein anderer Prozess sie gerade angelegt
    („duplicate column“), wird das übersprungen."""
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
