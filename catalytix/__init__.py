"""Catalytix – library management.

Copyright (C) 2026 The Catalytix authors
SPDX-License-Identifier: AGPL-3.0-or-later

This program is free software: you can redistribute it and/or modify it under the terms of the GNU
Affero General Public License, version 3 or (at your option) any later version. It is provided
WITHOUT ANY WARRANTY. Details: file LICENSE.
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
from flask import Flask, abort, flash, has_request_context, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect

from .models import MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLES, Loan, User, db, get_bool, get_setting
from .models import get_int as get_int_setting
from .i18n import gettext as _

__version__ = "3.1.4"
APP_NAME = "Catalytix"
LICENSE_NAME = "GNU AGPL v3 oder später"
# Where the source code can be found (AGPL § 13). Anyone running a modified Catalytix enters the
# address of their own modified source code here or in CATALYTIX_SOURCE_URL.
DEFAULT_SOURCE_URL = ""
AUTHOR_URL = "https://helfi.space"
# "Buy me a coffee" button in the sidebar – shown to admins only
SUPPORT_URL = "https://buymeacoffee.com/soleil0815"


import hashlib as _hashlib

_ASSET_REV = _hashlib.sha1(f"catalytix-{__version__}".encode()).hexdigest()[:8]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")

log = logging.getLogger(__name__)
LDAP_RECHECK_SECONDS = 4 * 3600   # re-check AD accounts of active sessions every 4 hours

login_manager = LoginManager()
csrf = CSRFProtect()


def _engine_options(url: str) -> dict:
    opts = {"pool_pre_ping": True}
    if url.startswith(("mysql", "mariadb")):
        # MySQL defaults to REPEATABLE READ: after waiting for the book lock, a loan would see the
        # old state and could lend out the last copy a second time.
        opts["isolation_level"] = "READ COMMITTED"
    return opts


def _secret_key(instance: Path) -> str:
    """Session and encryption key: CATALYTIX_SECRET_KEY or <data folder>/secret_key.

    The file is created atomically (O_EXCL): if several workers start at the same time, exactly one
    creates the key and the others read it. An empty key is never used."""
    key = os.environ.get("CATALYTIX_SECRET_KEY", "")
    if key.strip():
        return key   # unchanged, as up to 2.9 (whitespace is part of the key)
    f = instance / "secret_key"
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):           # another worker is writing it right now – wait briefly
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
    """Exclusive lock on <data folder>/.startup.lock for the startup steps (key, tables,
    columns, migrations). gunicorn starts several workers at the same time – without the lock they would
    create the same tables/columns in parallel and abort with "table already exists"."""
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
            except OSError as exc:    # e.g. NFS without locking (ENOLCK)
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
            except Exception as exc:  # noqa: BLE001 – the lock is a safeguard, not a requirement
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
    """Does the key match the database? With a different key, 2FA secrets and the stored SMTP/AD
    passwords would no longer be readable. On first start a check value (HMAC of a constant, not the
    key) is stored; if it does not match later, Catalytix does not start –
    unless CATALYTIX_ACCEPT_NEW_KEY=1 is set (then the new key applies)."""
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
    """TZ (e.g. Europe/Berlin) only takes effect if the system has the time zone file (/usr/share/zoneinfo).
    If it is missing, the file from the Python package "tzdata" is used (if installed); otherwise a
    clear warning is logged (times would then be UTC). Returns the TZ value that is used."""
    environ = os.environ if environ is None else environ
    tzset = tzset or getattr(time, "tzset", None)
    raw = (environ.get("TZ") or "").strip()
    name = raw[1:] if raw.startswith(":") else raw
    if not name or tzset is None:
        return None
    if ".." in name.split("/"):
        pass                          # invalid – warning below
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
            return raw                # POSIX spec like CET-1CEST,M3.5.0,M10.5.0/3 – needs no file
        if tzdata_root is None:
            try:
                from importlib.resources import files
                tzdata_root = str(files("tzdata") / "zoneinfo")
            except Exception:  # noqa: BLE001 – package tzdata not installed
                tzdata_root = None
        candidate = os.path.join(tzdata_root, *name.split("/")) if tzdata_root else None
        if candidate and os.path.isfile(candidate):
            environ["TZ"] = ":" + candidate
            tzset()
            log.info("Time zone %s loaded from the Python package tzdata", name)
            return environ["TZ"]
    if name.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "UTC0", "GMT0"):
        return raw                    # UTC anyway without a file
    log.warning("Time zone TZ=%s not found – times in UTC. Check in the container: "
                "\"ls /usr/share/zoneinfo/%s\"; spelling like Europe/Berlin.", raw, name)
    return None


DB_FILE = "catalytix.db"


def _sqlite_file(instance: Path) -> Path:
    """Default database in the data directory.

    Protection against an "empty restart": if catalytix.db is missing but another database file exists in
    the data directory, the program does not start instead of silently creating a new, empty library."""
    db_file = instance / DB_FILE
    if not db_file.exists():
        others = sorted(p.name for p in instance.glob("*.db") if p.is_file() and p.stat().st_size > 0)
        if others:
            raise SystemExit(
                f"The data folder {instance} contains no {DB_FILE}, but: {', '.join(others)}. "
                f"If that is your previous database, please rename it, e.g.: mv {others[0]} {DB_FILE} – "
                f"otherwise move the file away. (Protection against accidentally starting with an empty library.)")
    return db_file


def _migrate_and_init(app: Flask) -> None:
    """Add missing tables/columns and run one-time conversions – under the startup lock, in the app context."""
    db.create_all()
    from .models import ensure_columns
    for col in ensure_columns():
        logging.getLogger(__name__).info("Database updated: column %s added", col)
        if col == "users.group_bound":   # existing accounts with groups are group-bound
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
    if get_setting("library_name") == "CATALYTIX":  # old default name (program name, up to 2.2)
        set_setting("library_name", "Bibliothek")
        db.session.commit()
    from .models import encrypt_stored_secrets
    if encrypt_stored_secrets():
        logging.getLogger(__name__).info("Stored credentials (AD, SMTP) are now kept encrypted")
    if get_setting("registration_privacy_url") and not get_setting("privacy_url"):  # up to 2.0
        set_setting("privacy_url", get_setting("registration_privacy_url"))
        set_setting("registration_privacy_url", "")
        db.session.commit()


def create_app(test_config: dict | None = None) -> Flask:
    data_dir = os.environ.get("CATALYTIX_DATA_DIR")
    app = Flask(__name__, instance_path=os.path.abspath(data_dir) if data_dir else None,
                instance_relative_config=True)
    instance = Path(app.instance_path)
    try:
        instance.mkdir(parents=True, exist_ok=True)
        (instance / "covers").mkdir(exist_ok=True)
        probe = instance / f".write-test-{os.getpid()}"   # separate file per process (workers start at the same time)
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            f"Catalytix: no write permission in the data folder {instance} (user ID {os.getuid()}). "
            f"Please fix the permissions, e.g. on the host: sudo chown -R {os.getuid()}:{os.getgid()} <data folder>"
        ) from exc

    with _startup_lock(instance):     # see _startup_lock: workers start at the same time
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
        # Upper bound for the session cookie; the actual limit (time without activity) is checked on every
        # request, see autologout.py
        PERMANENT_SESSION_LIFETIME=timedelta(hours=168),
        # Write the session cookie only when it changed (autologout.touch: at most once a minute) – otherwise a
        # slow response that started before signing out would bring the old session back
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_TIME_LIMIT=None,
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(level=os.environ.get("CATALYTIX_LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _ensure_timezone()

    # Reverse proxy and HTTPS are detected automatically (see proxy.py)
    from .proxy import install as install_proxy_support
    install_proxy_support(app, os.environ)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Bitte melden Sie sich an."
    from .i18n import gettext as _gettext
    login_manager.localize_callback = _gettext
    login_manager.login_message_category = "info"

    @login_manager.unauthorized_handler
    def _unauthorized():
        from flask import jsonify
        if request.path.startswith("/api/"):
            # Requests from app.js: a clear answer instead of a redirect to the login page (HTML)
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
        # Session from an older "generation" (password changed, signed out everywhere …) -> invalid
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

    # ---- Startup steps under an exclusive lock (several gunicorn workers start at the same time) ----
    with _startup_lock(instance), app.app_context():
        from . import dbversion
        db_state = dbversion.prepare(db)     # newer database: abort; version change: back up first
        if db_state != "skip":               # "skip": only restoring a backup – migrate nothing
            _migrate_and_init(app)
            dbversion.stamp(db)

    # Background job for email reminders (daily at the configured time)
    from . import composefile
    if composefile.outdated() is not None:
        log.warning(composefile.log_message(composefile.outdated()))
    if (not app.config.get("TESTING") and os.environ.get("CATALYTIX_SCHEDULER", "1") == "1"
            and db_state != "skip"):
        from .mailer import start_scheduler
        start_scheduler(app)

    # ------------------------------------------------------------ First-time setup
    @app.before_request
    def require_setup():
        if request.endpoint in ("main.setup", "main.set_language", "static", "main.branding_file") or request.endpoint is None:
            return None
        if not app.config.get("_setup_done"):
            if User.query.filter_by(role=ROLE_ADMIN).first() is None:
                # Once set up → never back to the web setup (see main.setup)
                # Only a truly empty database may be set up via the web
                if get_setting("setup_completed") != "1" and User.query.first() is None:
                    return redirect(url_for("main.setup"))
            elif get_setting("setup_completed") != "1":
                from sqlalchemy.exc import IntegrityError
                from .models import set_setting as _set
                try:
                    _set("setup_completed", "1")
                    db.session.commit()
                except IntegrityError:   # another process was faster
                    db.session.rollback()
            app.config["_setup_done"] = True
        return None

    # ------------------------------------------------------------ Automatic sign-out (see autologout.py)
    @app.before_request
    def enforce_idle_signout():
        """Sign out after the configured time without activity, and end sessions restored from the
        "stay signed in" cookie where that is no longer allowed (admins, sign-ins from outside, sign-out on
        browser close)."""
        if request.endpoint in (None, "static", "main.branding_file") or not current_user.is_authenticated:
            return None
        from flask import jsonify
        from flask_login import login_fresh
        from flask_login.utils import login_url
        from . import autologout, netzone
        from .i18n import ngettext as _n
        internal = netzone.is_internal()
        user = current_user._get_current_object()
        left = autologout.seconds_left(user, internal)
        expired = left is not None and left <= 0
        # Restored from Flask-Login's remember cookie (only sign-ins from before 3.0.8 have one): continue as a
        # "stay signed in" session where that is allowed, otherwise sign in again
        restored = left is None and not login_fresh()
        if restored and autologout.remember_allowed(user, internal):
            session[autologout.STAY_KEY] = True
            session["_remember"] = "clear"      # Flask-Login deletes the old remember cookie
            restored = False
        if expired or restored:
            if expired:
                n, unit = autologout.describe_limit(autologout.limit_seconds(user, internal))
                msg = (_n("Sie wurden nach {n} Stunde ohne Aktivität automatisch abgemeldet.",
                                 "Sie wurden nach {n} Stunden ohne Aktivität automatisch abgemeldet.", n)
                       if unit == "h" else
                       _n("Sie wurden nach {n} Minute ohne Aktivität automatisch abgemeldet.",
                                 "Sie wurden nach {n} Minuten ohne Aktivität automatisch abgemeldet.", n))
            else:
                msg = _("Ihre Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an.")
            session.clear()     # first – logout_user() records in it that the remember cookie is to be deleted
            logout_user()
            flash(msg, "info")     # also for /api/: the login page the browser goes to next shows why
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error=msg, signed_out=True), 401
            return redirect(login_url("main.login", request.url if request.method == "GET" else None))
        autologout.apply_cookie_mode(user, internal)
        if request.endpoint not in autologout.NO_ACTIVITY:
            autologout.touch()
        return None

    # ------------------------------------------------------------ Re-check LDAP sessions
    @app.before_request
    def recheck_ldap_session():
        """Anyone disabled or deleted in AD, or removed from the authorized group, must not be able to
        keep working with an old session (or via "remember me")."""
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
            session["ldap_ok_at"] = now    # cannot be checked without a service account
            return None
        try:
            info = Directory().check_account(current_user.username)
        except LDAPError as exc:
            log.warning("LDAP re-check for %s not possible: %s", current_user.username, exc)
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600   # retry in 10 minutes
            return None
        except Exception:  # noqa: BLE001 – an AD problem must not lock anyone out
            log.exception("LDAP re-check failed")
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600
            return None
        from .views.main import same_person
        if info is not None and not same_person(current_user, info):
            # In AD the name now belongs to a different person (e.g. renamed and reassigned):
            # do not continue this session and do not overwrite the account with someone else's data
            log.warning("LDAP account %s: the name now belongs to a different person in AD – session ended",
                        current_user.username)
            info = None
        if info is None or info.role is None:
            log.info("LDAP account %s no longer entitled – session ended", current_user.username)
            session.clear()     # first – logout_user() records in it that the remember cookie is to be deleted
            logout_user()
            flash(_("Ihre Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an."), "info")
            return redirect(url_for("main.login"))
        from .views.main import sync_ldap_user
        sync_ldap_user(info, current_user._get_current_object())
        db.session.commit()
        session["ldap_ok_at"] = now
        return None

    # ------------------------------------------------------------ Template helpers
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
            "support_url": SUPPORT_URL,
            "source_url": os.environ.get("CATALYTIX_SOURCE_URL", DEFAULT_SOURCE_URL),
            "imprint_url": get_setting("imprint_url"),
            "retention_months": get_int_setting("retention_months"),
            "privacy_url": get_setting("privacy_url"),
            # Cache key for CSS/JS without the version number (not publicly identifiable)
            "asset_rev": _ASSET_REV,
            "ldap_on": get_bool("ldap_enabled"),
            "brand": brand_context(),
            "registration_open": get_setting("registration_mode") in ("approval", "open"),
            "pending_registrations": 0,
            "overdue_count": 0,
            "news_unseen": False,
        }
        ctx["idle_warning"] = None
        ctx["remember_offered"] = True
        if has_request_context():
            from . import autologout
            ctx["remember_offered"] = not autologout.browser_close()
        if has_request_context() and current_user.is_authenticated:
            from .news import has_unseen
            ctx["news_unseen"] = has_unseen(current_user)
            from . import autologout, netzone
            if autologout.warning_enabled():
                ctx["idle_warning"] = {"seconds": autologout.limit_seconds(current_user, netzone.is_internal()),
                                       "warn": autologout.WARN_SECONDS}
        # Outside a request (e.g. sending mail in the background) there is no user
        if has_request_context() and current_user.is_authenticated and current_user.is_admin:
            ctx["pending_registrations"] = User.query.filter_by(pending_approval=True, email_verified=True).count()
            from .backup import warning as backup_warning
            ctx["backup_warning"] = backup_warning()
        if has_request_context() and current_user.is_authenticated and current_user.is_librarian:
            from .models import Book, manageable_books_query
            ctx["overdue_count"] = Loan.query.filter(      # only the holdings this person manages
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
        # Pages opened from here (or opening this one) get no handle on each other's window. No
        # Cross-Origin-Resource-Policy: mails load the logo from here, webmail would be blocked.
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # Strict Content-Security-Policy: no inline or third-party JavaScript, no external sources.
        # (For SVG logos the respective route sets an even stricter policy of its own.)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        # Pages and data are never stored by the browser or a proxy: after signing out on a shared computer
        # (lending desk), the back button must not show names, loans or admin pages from the cache. Static files
        # (versioned URLs), covers and logos keep their own caching.
        if "Cache-Control" not in resp.headers and resp.mimetype in ("text/html", "application/json"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.before_request
    def static_no_trailing_slash():
        """/static/js/app.js/ is not a directory – answer 404 instead of serving the file (scanners report the
        file content as a directory listing)."""
        if request.endpoint == "static" and request.path.endswith("/"):
            abort(404)
        return None

    # ------------------------------------------------------------ Error pages
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, title=_("Kein Zugriff"),
                               text=_("Dafür fehlt Ihnen die Berechtigung.")), 403

    @app.errorhandler(OverflowError)
    def too_large_number(_e):
        # e.g. /admin/benutzer/99999999999999999999 – number too large for the database
        db.session.rollback()
        return not_found(None)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, title=_("Nicht gefunden"),
                               text=_("Diese Seite oder dieses Buch gibt es nicht.")), 404

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400, title=_("Sitzung abgelaufen"),
                               text=_("Bitte laden Sie die Seite neu und versuchen Sie es noch einmal.")), 400

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
        user.ldap_gone_at = None   # no longer managed by the AD sync
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

    @app.cli.command("restore-before")
    @click.argument("version")
    @click.option("--since", type=int, default=None,
                  help="Only a backup written at/after this Unix time (update.sh: start of the update)")
    @click.option("--yes", is_flag=True, help="No confirmation")
    def restore_before_cmd(version, since, yes):
        """Restore the backup taken when VERSION first started (the state before that update) – server stopped!

        Exit code 3 if there is no such backup (that version never changed the database), 4 with MySQL/PostgreSQL
        (no automatic backups)."""
        from . import dbversion
        from .backup import BackupError, english, find_before, restore_backup, supported
        if dbversion.vtuple(version) is None:
            raise click.ClickException("VERSION must look like 3.0.0")
        if not supported():
            click.echo("No automatic backups with this database (MySQL/PostgreSQL) – restore your own dump.")
            raise SystemExit(4)
        src = find_before(version, since)
        if src is None:
            click.echo(f"No backup from before {version} found – the database was not changed by {version}.")
            raise SystemExit(3)
        if not yes:
            click.confirm(f"Replace the database with {src.name}? The server must be stopped for this.", abort=True)
        try:
            safety = restore_backup(src.name)
        except BackupError as exc:
            raise click.ClickException(english(str(exc)))
        if safety:
            click.echo(f"Previous state backed up as {safety.name}")
        click.echo(f"{src.name} has been restored (state before {version}).")

    @app.cli.command("restore-for-version")
    @click.argument("version")
    @click.option("--yes", is_flag=True, help="No confirmation")
    def restore_for_version_cmd(version, yes):
        """Going back to VERSION: restore the newest backup that VERSION can use (written by VERSION or older).

        Exit code 3 if there is none, 4 with MySQL/PostgreSQL. Server stopped!"""
        from . import dbversion
        from .backup import BackupError, english, find_for_version, restore_backup, supported
        if dbversion.vtuple(version) is None:
            raise click.ClickException("VERSION must look like 3.0.0")
        if not supported():
            click.echo("No automatic backups with this database (MySQL/PostgreSQL) – restore your own dump.")
            raise SystemExit(4)
        src = find_for_version(version)
        if src is None:
            click.echo(f"No backup that version {version} can use was found.")
            raise SystemExit(3)
        if not yes:
            click.confirm(f"Replace the database with {src.name}? The server must be stopped for this.", abort=True)
        try:
            safety = restore_backup(src.name)
        except BackupError as exc:
            raise click.ClickException(english(str(exc)))
        if safety:
            click.echo(f"Previous state backed up as {safety.name}")
        click.echo(f"{src.name} has been restored.")

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
