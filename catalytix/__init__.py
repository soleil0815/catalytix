"""Catalytix – Bibliotheksverwaltung.

Copyright (C) 2026 Die Catalytix-Autoren
SPDX-License-Identifier: AGPL-3.0-or-later

Dieses Programm ist freie Software: Sie können es unter den Bedingungen der GNU Affero General
Public License, Version 3 oder (nach Ihrer Wahl) jeder späteren Version, weitergeben und/oder
verändern. Es wird OHNE JEDE GEWÄHRLEISTUNG bereitgestellt. Details: Datei LICENSE.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from flask import Flask, flash, has_request_context, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect

from .models import MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLES, Loan, User, db, get_bool, get_setting
from .models import get_int as get_int_setting
from .i18n import gettext as _

__version__ = "2.20.0"
APP_NAME = "Catalytix"
LICENSE_NAME = "GNU AGPL v3 oder später"
# Wo der Quellcode zu finden ist (AGPL § 13). Wer Catalytix verändert betreibt, trägt hier
# bzw. in CATALYTIX_SOURCE_URL die Adresse des eigenen, veränderten Quellcodes ein.
DEFAULT_SOURCE_URL = ""
AUTHOR_URL = "https://helfi.space"


import hashlib as _hashlib

_ASSET_REV = _hashlib.sha1(f"catalytix-{__version__}".encode()).hexdigest()[:8]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")

log = logging.getLogger(__name__)
LDAP_RECHECK_SECONDS = 4 * 3600   # AD-Konten laufender Sitzungen alle 4 Stunden nachprüfen

login_manager = LoginManager()
csrf = CSRFProtect()


def _engine_options(url: str) -> dict:
    opts = {"pool_pre_ping": True}
    if url.startswith(("mysql", "mariadb")):
        # Standard bei MySQL ist REPEATABLE READ: Nach dem Warten auf die Buch-Sperre sähe eine Ausleihe den
        # alten Stand und könnte das letzte Exemplar ein zweites Mal verleihen.
        opts["isolation_level"] = "READ COMMITTED"
    return opts


def _secret_key(instance: Path) -> str:
    """Sitzungs- und Verschlüsselungsschlüssel: CATALYTIX_SECRET_KEY oder <Datenordner>/secret_key.

    Die Datei wird atomar angelegt (O_EXCL): Starten mehrere Worker gleichzeitig, erzeugt genau einer
    den Schlüssel, die anderen lesen ihn. Ein leerer Schlüssel wird nie verwendet."""
    key = os.environ.get("CATALYTIX_SECRET_KEY", "")
    if key.strip():
        return key   # unverändert wie bis 2.9 (Leerzeichen gehören zum Schlüssel)
    f = instance / "secret_key"
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):           # ein anderer Worker schreibt gerade – kurz warten
            key = f.read_text().strip()
            if key:
                return key
            time.sleep(0.1)
        raise SystemExit(
            f"Catalytix: the key file {f} is empty. If Catalytix has never run with this data folder, delete "
            f"the file; otherwise restore it from a backup (it belongs to the database – without it, 2FA "
            f"sign-ins and stored SMTP/AD passwords no longer work).")
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    return key


@contextmanager
def _startup_lock(instance: Path):
    """Exklusive Sperre auf <Datenordner>/.startup.lock für die Start-Schritte (Schlüssel, Tabellen,
    Spalten, Migrationen). gunicorn startet mehrere Worker gleichzeitig – ohne Sperre würden sie
    dieselben Tabellen/Spalten parallel anlegen und mit „table already exists“ abbrechen."""
    try:
        fh = open(instance / ".startup.lock", "a+b")
    except OSError as exc:
        log.warning("Startup lock %s not available (%s) – continuing without it", instance / ".startup.lock", exc)
        yield
        return
    unlock = None
    try:
        try:
            import fcntl
        except ImportError:           # Windows
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # noqa: E731
            except OSError as exc:    # z. B. NFS ohne Sperren (ENOLCK)
                log.warning("Startup lock not possible (%s) – continuing without it", exc)
        else:
            try:
                import msvcrt
                fh.seek(0)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.2)

                def unlock():
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception as exc:  # noqa: BLE001 – Sperre ist eine Absicherung, kein Muss
                log.warning("Startup lock not possible (%s) – continuing without it", exc)
        yield
    finally:
        try:
            if unlock is not None:
                unlock()
        finally:
            fh.close()


KEY_CHECK_SETTING = "secret_key_check"


def _key_fingerprint(key: str) -> str:
    import hmac
    return hmac.new(key.encode(), b"catalytix-secret-key-check", "sha256").hexdigest()


def _check_secret_key(app: Flask) -> None:
    """Passt der Schlüssel zur Datenbank? Mit einem anderen Schlüssel wären 2FA-Geheimnisse und die
    gespeicherten SMTP-/AD-Passwörter nicht mehr lesbar. Beim ersten Start wird ein Prüfwert (HMAC einer
    Konstante, nicht der Schlüssel) gespeichert; passt er später nicht, startet Catalytix nicht –
    außer mit CATALYTIX_ACCEPT_NEW_KEY=1 (dann gilt der neue Schlüssel)."""
    from .models import set_setting
    expected = _key_fingerprint(app.config["SECRET_KEY"])
    stored = get_setting(KEY_CHECK_SETTING)
    if stored == expected:
        return
    if stored and os.environ.get("CATALYTIX_ACCEPT_NEW_KEY", "").strip() != "1":
        msg = ("Catalytix: the key (CATALYTIX_SECRET_KEY or the file secret_key in the data folder) does "
               "NOT match the database. With this key, 2FA sign-ins fail and stored SMTP/AD passwords are "
               "unreadable. Please restore the key that belongs to the database (secret_key from the same "
               "backup as the database). To use the new key anyway, start once with "
               "CATALYTIX_ACCEPT_NEW_KEY=1 – then reset 2FA for everyone and enter the SMTP/AD passwords again.")
        log.error("%s\n%s\n%s", "!" * 78, msg, "!" * 78)
        raise SystemExit(msg)
    if stored:
        log.warning("New key accepted (CATALYTIX_ACCEPT_NEW_KEY=1): existing 2FA setups and stored SMTP/AD "
                    "passwords are no longer readable and have to be set up again.")
    set_setting(KEY_CHECK_SETTING, expected)
    db.session.commit()


def _ensure_timezone(environ=None, zoneinfo_dirs=None, tzdata_root=None, tzset=None) -> str | None:
    """TZ (z. B. Europe/Berlin) wirkt nur, wenn das System die Zeitzonendatei hat (/usr/share/zoneinfo).
    Fehlt sie, wird – falls installiert – die Datei aus dem Python-Paket „tzdata“ verwendet; sonst
    eine deutliche Warnung (Uhrzeiten wären dann UTC). Gibt den verwendeten TZ-Wert zurück."""
    environ = os.environ if environ is None else environ
    tzset = tzset or getattr(time, "tzset", None)
    raw = (environ.get("TZ") or "").strip()
    name = raw[1:] if raw.startswith(":") else raw
    if not name or tzset is None:
        return None
    if ".." in name.split("/"):
        pass                          # ungültig – unten Warnung
    elif os.path.isabs(name):
        if os.path.exists(name):
            return raw
    else:
        if zoneinfo_dirs is None:
            zoneinfo_dirs = [d for d in (environ.get("TZDIR"), "/usr/share/zoneinfo", "/usr/lib/zoneinfo",
                                         "/usr/share/lib/zoneinfo") if d]
        if any(os.path.isfile(os.path.join(d, name)) for d in zoneinfo_dirs):
            return raw
        if "," in name or re.match(r"^(<[^>]+>|[A-Za-z]{3,})[+-]?\d", name):
            return raw                # POSIX-Angabe wie CET-1CEST,M3.5.0,M10.5.0/3 – braucht keine Datei
        if tzdata_root is None:
            try:
                from importlib.resources import files
                tzdata_root = str(files("tzdata") / "zoneinfo")
            except Exception:  # noqa: BLE001 – Paket tzdata nicht installiert
                tzdata_root = None
        candidate = os.path.join(tzdata_root, *name.split("/")) if tzdata_root else None
        if candidate and os.path.isfile(candidate):
            environ["TZ"] = ":" + candidate
            tzset()
            log.info("Time zone %s loaded from the Python package tzdata", name)
            return environ["TZ"]
    if name.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "UTC0", "GMT0"):
        return raw                    # ohne Datei ohnehin UTC
    log.warning("Time zone TZ=%s not found – times in UTC. Check in the container: "
                "\"ls /usr/share/zoneinfo/%s\"; spelling like Europe/Berlin.", raw, name)
    return None


DB_FILE = "catalytix.db"


def _sqlite_file(instance: Path) -> Path:
    """Standard-Datenbank im Datenverzeichnis.

    Schutz vor einem „leeren Neustart“: Fehlt catalytix.db, liegt aber eine andere Datenbankdatei im
    Datenverzeichnis, startet das Programm nicht, statt still eine neue, leere Bibliothek anzulegen."""
    db_file = instance / DB_FILE
    if not db_file.exists():
        others = sorted(p.name for p in instance.glob("*.db") if p.is_file() and p.stat().st_size > 0)
        if others:
            raise SystemExit(
                f"The data folder {instance} contains no {DB_FILE}, but: {', '.join(others)}. "
                f"If that is your previous database, please rename it, e.g.: mv {others[0]} {DB_FILE} – "
                f"otherwise move the file away. (Protection against accidentally starting with an empty library.)")
    return db_file


def create_app(test_config: dict | None = None) -> Flask:
    data_dir = os.environ.get("CATALYTIX_DATA_DIR")
    app = Flask(__name__, instance_path=os.path.abspath(data_dir) if data_dir else None,
                instance_relative_config=True)
    instance = Path(app.instance_path)
    try:
        instance.mkdir(parents=True, exist_ok=True)
        (instance / "covers").mkdir(exist_ok=True)
        probe = instance / f".write-test-{os.getpid()}"   # je Prozess eigene Datei (Worker starten gleichzeitig)
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            f"Catalytix: no write permission in the data folder {instance} (user ID {os.getuid()}). "
            f"Please fix the permissions, e.g. on the host: sudo chown -R {os.getuid()}:{os.getgid()} <data folder>"
        ) from exc

    with _startup_lock(instance):     # siehe _startup_lock: Worker starten gleichzeitig
        secret_key = _secret_key(instance)
    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL") or f"sqlite:///{_sqlite_file(instance)}",
        SQLALCHEMY_ENGINE_OPTIONS=_engine_options(os.environ.get("DATABASE_URL", "")),
        COVER_DIR=str(instance / "covers"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_DURATION=timedelta(days=14),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=None,
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(level=os.environ.get("CATALYTIX_LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _ensure_timezone()

    # Reverse-Proxy und HTTPS werden automatisch erkannt (siehe proxy.py)
    from .proxy import install as install_proxy_support
    install_proxy_support(app, os.environ)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Bitte melde dich an."
    from .i18n import gettext as _gettext
    login_manager.localize_callback = _gettext
    login_manager.login_message_category = "info"

    @login_manager.unauthorized_handler
    def _unauthorized():
        from flask import jsonify
        if request.path.startswith("/api/"):
            # Abfragen aus app.js: klare Antwort statt Umleitung auf die Anmeldeseite (HTML)
            msg = _gettext("Deine Sitzung ist abgelaufen – bitte die Seite neu laden und erneut anmelden.")  # noqa: E501
            return jsonify(ok=False, error=msg), 401
        from flask_login.utils import login_url
        flash(_gettext(login_manager.login_message), login_manager.login_message_category)
        return redirect(login_url(login_manager.login_view, request.url))

    @login_manager.user_loader
    def load_user(user_id):
        uid, _, epoch = str(user_id).partition(":")
        if not uid.isdigit():
            return None
        user = db.session.get(User, int(uid))
        if user is None or not user.active:
            return None
        # Sitzung aus einer älteren „Generation“ (Passwort geändert, überall abgemeldet …) -> ungültig
        if str(user.auth_epoch or 0) != (epoch or "0"):
            return None
        return user

    from .views.admin import bp as admin_bp
    from .views.books import bp as books_bp
    from .views.loans import bp as loans_bp
    from .views.locations import bp as locations_bp
    from .views.main import bp as main_bp
    from .views.register import bp as register_bp
    from .views.security import bp as security_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(books_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(security_bp)
    app.register_blueprint(register_bp)
    from .views.help import bp as help_bp, help_url
    app.register_blueprint(help_bp)
    app.jinja_env.globals["help_url_for"] = help_url

    # ---- Start-Schritte unter exklusiver Sperre (mehrere gunicorn-Worker starten gleichzeitig) ----
    with _startup_lock(instance), app.app_context():
        db.create_all()
        from .models import ensure_columns
        for col in ensure_columns():
            logging.getLogger(__name__).info("Database updated: column %s added", col)
            if col == "users.group_bound":   # bestehende Konten mit Gruppen sind gruppengebunden
                from sqlalchemy import text as _text
                db.session.execute(_text("UPDATE users SET group_bound = :t WHERE id IN "
                                         "(SELECT user_id FROM user_groups)"), {"t": True})
                db.session.commit()
        from .models import ensure_indexes; ensure_indexes()  # noqa: E702
        _check_secret_key(app)
        from . import setupcode
        from .models import User as _User
        if get_setting("setup_completed") != "1" and _User.query.first() is None:
            setupcode.ensure()
            setupcode.announce()
        from .models import set_setting
        if get_setting("library_name") == "CATALYTIX":  # alter Standardname (Programmname, bis 2.2)
            set_setting("library_name", "Bibliothek")
            db.session.commit()
        from .models import encrypt_stored_secrets
        if encrypt_stored_secrets():
            logging.getLogger(__name__).info("Stored credentials (AD, SMTP) are now kept encrypted")
        if get_setting("registration_privacy_url") and not get_setting("privacy_url"):  # bis 2.0
            set_setting("privacy_url", get_setting("registration_privacy_url"))
            set_setting("registration_privacy_url", "")
            db.session.commit()

    # Hintergrund-Job für E-Mail-Erinnerungen (täglich zur eingestellten Uhrzeit)
    if not app.config.get("TESTING") and os.environ.get("CATALYTIX_SCHEDULER", "1") == "1":
        from .mailer import start_scheduler
        start_scheduler(app)

    # ------------------------------------------------------------ Ersteinrichtung
    @app.before_request
    def require_setup():
        if request.endpoint in ("main.setup", "main.set_language", "static", "main.branding_file") or request.endpoint is None:
            return None
        if not app.config.get("_setup_done"):
            if User.query.filter_by(role=ROLE_ADMIN).first() is None:
                # Einmal eingerichtet → nie wieder zur Web-Einrichtung (siehe main.setup)
                # Nur eine wirklich leere Datenbank darf über das Web eingerichtet werden
                if get_setting("setup_completed") != "1" and User.query.first() is None:
                    return redirect(url_for("main.setup"))
            elif get_setting("setup_completed") != "1":
                from sqlalchemy.exc import IntegrityError
                from .models import set_setting as _set
                try:
                    _set("setup_completed", "1")
                    db.session.commit()
                except IntegrityError:   # anderer Prozess war schneller
                    db.session.rollback()
            app.config["_setup_done"] = True
        return None

    # ------------------------------------------------------------ LDAP-Sitzungen nachprüfen
    @app.before_request
    def recheck_ldap_session():
        """Wer im AD gesperrt, gelöscht oder aus der berechtigten Gruppe entfernt wurde, soll nicht
        mit einer alten Sitzung (oder „Angemeldet bleiben“) weiterarbeiten können."""
        if request.endpoint in (None, "static", "main.branding_file") or not current_user.is_authenticated:
            return None
        if current_user.auth_source != "ldap":
            return None
        import time
        from .ldap_auth import Directory, LDAPError, ldap_enabled
        now = int(time.time())
        if now - int(session.get("ldap_ok_at") or 0) < LDAP_RECHECK_SECONDS:
            return None
        if not ldap_enabled() or not get_setting("ldap_bind_dn"):
            session["ldap_ok_at"] = now    # ohne Dienstkonto nicht prüfbar
            return None
        try:
            info = Directory().check_account(current_user.username)
        except LDAPError as exc:
            log.warning("LDAP re-check for %s not possible: %s", current_user.username, exc)
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600   # in 10 Minuten erneut
            return None
        except Exception:  # noqa: BLE001 – ein AD-Problem darf niemanden aussperren
            log.exception("LDAP re-check failed")
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600
            return None
        from .views.main import same_person
        if info is not None and not same_person(current_user, info):
            # Der Name gehört im AD inzwischen einer anderen Person (z. B. umbenannt und neu vergeben):
            # diese Sitzung nicht weiterführen und das Konto nicht mit fremden Daten überschreiben
            log.warning("LDAP account %s: the name now belongs to a different person in AD – session ended",
                        current_user.username)
            info = None
        if info is None or info.role is None:
            log.info("LDAP account %s no longer entitled – session ended", current_user.username)
            session.clear()     # zuerst – logout_user() merkt sich darin, dass das Merk-Cookie gelöscht wird
            logout_user()
            flash(_("Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an."), "info")
            return redirect(url_for("main.login"))
        from .views.main import sync_ldap_user
        sync_ldap_user(info, current_user._get_current_object())
        db.session.commit()
        session["ldap_ok_at"] = now
        return None

    # ------------------------------------------------------------ Template-Helfer
    from .branding import context as brand_context
    from .branding import library_name as brand_library_name

    @app.context_processor
    def inject_globals():
        ctx = {
            "library_name": brand_library_name(),
            "ROLES": ROLES,
            "today": date.today(),
            "version": __version__,
            "app_name": APP_NAME,
            "license_name": LICENSE_NAME,
            "author_url": AUTHOR_URL,
            "source_url": os.environ.get("CATALYTIX_SOURCE_URL", DEFAULT_SOURCE_URL),
            "imprint_url": get_setting("imprint_url"),
            "retention_months": get_int_setting("retention_months"),
            "privacy_url": get_setting("privacy_url"),
            # Cache-Schlüssel für CSS/JS ohne Versionsnummer (nicht öffentlich erkennbar)
            "asset_rev": _ASSET_REV,
            "ldap_on": get_bool("ldap_enabled"),
            "brand": brand_context(),
            "registration_open": get_setting("registration_mode") in ("approval", "open"),
            "pending_registrations": 0,
            "overdue_count": 0,
            "news_unseen": False,
        }
        if has_request_context() and current_user.is_authenticated:
            from .news import has_unseen
            ctx["news_unseen"] = has_unseen(current_user)
        # Außerhalb einer Anfrage (z. B. Mail-Versand im Hintergrund) gibt es keinen Benutzer
        if has_request_context() and current_user.is_authenticated and current_user.is_admin:
            ctx["pending_registrations"] = User.query.filter_by(pending_approval=True, email_verified=True).count()
            from .backup import warning as backup_warning
            ctx["backup_warning"] = backup_warning()
        if has_request_context() and current_user.is_authenticated and current_user.is_librarian:
            from .models import Book, manageable_books_query
            ctx["overdue_count"] = Loan.query.filter(      # nur der Bestand, den die Person verwaltet
                Loan.returned_at.is_(None), Loan.due_at < date.today(),
                Loan.book_id.in_(manageable_books_query(current_user).with_entities(Book.id))).count()
        return ctx

    @app.template_filter("filesize")
    def filesize(n):
        from .backup import human_size
        return human_size(n or 0)

    from . import i18n
    i18n.init_app(app)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self)")
        # Strenge Content-Security-Policy: kein eingebettetes oder fremdes JavaScript, keine fremden Quellen.
        # (Für SVG-Logos setzt die jeweilige Route eine noch strengere eigene Richtlinie.)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    # ------------------------------------------------------------ Fehlerseiten
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, title=_("Kein Zugriff"),
                               text=_("Dafür fehlt dir die Berechtigung.")), 403

    @app.errorhandler(OverflowError)
    def too_large_number(_e):
        # z. B. /admin/benutzer/99999999999999999999 – zu große Zahl für die Datenbank
        db.session.rollback()
        return not_found(None)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, title=_("Nicht gefunden"),
                               text=_("Diese Seite oder dieses Buch gibt es nicht.")), 404

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400, title=_("Sitzung abgelaufen"),
                               text=_("Bitte lade die Seite neu und versuche es noch einmal.")), 400

    # ------------------------------------------------------------ CLI
    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option()
    def reset_password(username, password):
        """Reset the password of a local account (e.g. admin)."""
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"User {username} not found.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        user.set_password(password)
        user.auth_source = "local"
        user.active = True
        user.ldap_gone_at = None   # nicht mehr vom AD-Abgleich verwaltet
        db.session.commit()
        click.echo(f"Password set for {username}.")

    @app.cli.command("reset-2fa")
    @click.argument("username")
    def reset_2fa_cmd(username):
        """Reset the second factor of an account (e.g. phone/key lost)."""
        from .twofa import reset_2fa
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"User {username} not found.")
        reset_2fa(user)
        click.echo(f"Two-factor authentication reset for {username}.")

    @app.cli.command("send-reminders")
    def send_reminders():
        """Send due reminders/overdue notices now (e.g. via cron)."""
        from .i18n import force_locale
        from .mailer import format_stats, run_notifications
        stats = run_notifications()
        with force_locale("en"):
            click.echo(format_stats(stats))

    @app.cli.command("apply-retention")
    def apply_retention():
        """Apply the retention period now (delete old loans and mail log entries)."""
        from .i18n import force_locale
        from .retention import display_message, format_result, run_retention
        res = run_retention()
        with force_locale("en"):
            click.echo(display_message(format_result(res)))

    @app.cli.command("backup")
    def backup_cmd():
        """Back up the database now."""
        from .backup import english, run_now
        ok, msg = run_now("-manuell")
        if not ok:
            raise click.ClickException(english(msg))
        click.echo(english(msg))

    @app.cli.command("list-backups")
    def list_backups_cmd():
        """List existing backups (newest first)."""
        from .backup import backup_dir, human_size, list_backups
        click.echo(f"Folder: {backup_dir()}")
        for b in list_backups():
            click.echo(f"  {b['name']}  {human_size(b['size']):>8}  {b['kind']}")

    @app.cli.command("restore-backup")
    @click.argument("name")
    @click.option("--yes", is_flag=True, help="No confirmation")
    def restore_backup_cmd(name, yes):
        """Restore a backup – ONLY with the server stopped! The current state is backed up first."""
        from .backup import BackupError, restore_backup
        if not yes:
            click.confirm(f"Replace the database with {name}? The server must be stopped for this.", abort=True)
        try:
            safety = restore_backup(name)
        except BackupError as exc:
            from .backup import english
            raise click.ClickException(english(str(exc)))
        if safety:
            click.echo(f"Previous state backed up as {safety.name}")
        click.echo(f"{name} has been restored. Start the server again now.")

    @app.cli.command("setup-code")
    def setup_code_cmd():
        """Show the setup code for the first-time setup (empty once setup is complete)."""
        from . import setupcode
        if get_setting("setup_completed") != "1" and User.query.first() is None:
            click.echo(setupcode.ensure())

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        """Create a local administrator account."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException("User already exists.")
        user = User(username=username, display_name=username, role=ROLE_ADMIN, auth_source="local")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Administrator {username} created.")

    return app
