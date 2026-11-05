"""Which program version last used the database? – Protection when changing versions.

At startup (under the startup lock, BEFORE new tables and columns are created):
- Database from an older version (or from before 3.0.0, without an entry): first create a backup
  "catalytix-<time>-vor-<new version>.db" (SQLite only), then migrate, then record the new version.
- Database from a NEWER version: do not start – with a clear message saying which backup to restore
  (or which version to set in .env). Without this check an old program would run on a schema it does
  not know and leave follow-up errors behind.

CATALYTIX_SKIP_DB_CHECK=1 skips the check AND all migrations – only for restoring a backup
with an older version (update.sh after a failed update, "flask restore-before").
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text

from . import __version__

if TYPE_CHECKING:
    from flask_sqlalchemy import SQLAlchemy

log = logging.getLogger(__name__)

KEY = "db_version"
BACKUP_KEY = "db_upgrade_backup"     # version for which the backup before the change has already been created
SKIP_ENV = "CATALYTIX_SKIP_DB_CHECK"


def vtuple(v: str | None) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


class StartupRefused(RuntimeError):
    """Startup refused (not SystemExit: gunicorn then stops with "Worker failed to boot" instead of restarting
    the workers every second)."""


class DatabaseTooNew(StartupRefused):
    """The database comes from a newer program version."""


def read_stamp(db: SQLAlchemy, key: str = KEY) -> str | None:
    """Stored version (None: new database or a database from before 3.0.0)."""
    insp = inspect(db.engine)
    if not insp.has_table("settings"):
        return None
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM settings WHERE " + _key_col(db) + " = :k"), {"k": key}).first()
    return (row[0] or None) if row else None


def _write_setting(db: SQLAlchemy, key: str, value: str) -> None:
    """Before the migrations: write directly to the settings table (the model may not match yet)."""
    col = _key_col(db)
    with db.engine.begin() as conn:
        done = conn.execute(text(f"UPDATE settings SET value = :v WHERE {col} = :k"), {"k": key, "v": value})
        if not done.rowcount:
            conn.execute(text(f"INSERT INTO settings ({col}, value) VALUES (:k, :v)"), {"k": key, "v": value})


def _key_col(db: SQLAlchemy) -> str:
    quoted: str = db.engine.dialect.identifier_preparer.quote("key")
    return quoted   # "key" is a reserved word in MySQL


def is_empty(db: SQLAlchemy) -> bool:
    """Not yet a Catalytix database (new installation)."""
    insp = inspect(db.engine)
    return not (insp.has_table("users") or insp.has_table("settings"))


def too_new_message(stamp: str, current: str = __version__) -> str:
    return (f"The database was last used by Catalytix {stamp}, but this is version {current} – an older program "
            f"must not run on a newer database. Either start version {stamp} or newer again "
            f"(CATALYTIX_TAG={stamp} in .env, then: docker compose up -d), or go back to {current} with a backup "
            f"it can read (everything entered since is lost): ./update.sh --version={current} does that – or by "
            f"hand with \"flask --app wsgi restore-for-version {current}\" (backups \"catalytix-…-vor-{stamp}.db\" "
            f"and older; see README, \"Going back\"). Nothing was changed.")


def prepare(db: SQLAlchemy, current: str = __version__) -> str:
    """Call before the migrations. Returns 'new' (empty database), 'same', 'upgrade' or 'skip'
    (CATALYTIX_SKIP_DB_CHECK – no migrations!). Aborts on a newer database."""
    if os.environ.get(SKIP_ENV) == "1":
        log.warning("%s=1: database version check and migrations skipped (only for restoring a backup)", SKIP_ENV)
        return "skip"
    if is_empty(db):
        return "new"
    stamp = read_stamp(db)
    have, want = vtuple(stamp), vtuple(current)
    if stamp is not None and have is not None and want is not None and have > want:
        log.error(too_new_message(stamp, current))
        raise DatabaseTooNew("Catalytix: " + too_new_message(stamp, current))
    if have is not None and have == want:
        return "same"
    if read_stamp(db, BACKUP_KEY) == current:
        # An earlier start attempt of this version already made a backup (e.g. migration aborted, restart
        # loop): do not back up again on every start – that would fill the disk, and the first backup is
        # the one from before the update
        log.warning("Version change %s → %s: backup from the first start attempt is kept, no new one",
                    stamp or "before 3.0.0", current)
        return "upgrade"
    _backup_before(current, stamp)
    if inspect(db.engine).has_table("settings"):
        _write_setting(db, BACKUP_KEY, current)
    return "upgrade"


def _backup_before(current: str, stamp: str | None) -> None:
    from .backup import BackupError, create_backup, supported
    if not supported():
        log.warning("Version change %s → %s: no automatic backup with this database (MySQL/PostgreSQL) – back it up "
                    "with mysqldump/pg_dump before every update.", stamp or "before 3.0.0", current)
        return
    try:
        path = create_backup(f"-vor-{current}", rotate_after=False)
    except BackupError as exc:
        from .backup import english
        msg = (f"Catalytix: backup before the update to {current} failed ({english(str(exc))}) – not starting, so "
               f"the database is not changed without a backup. Free up space in the backup folder or fix its "
               f"permissions and start again.")
        log.error(msg)
        raise StartupRefused(msg) from exc
    log.info("Version change %s → %s: database backed up as %s", stamp or "before 3.0.0", current, path.name)


def stamp(db: SQLAlchemy, current: str = __version__) -> None:
    """After successful migrations: record this version."""
    from .models import Setting
    row = db.session.get(Setting, KEY)
    changed = False
    if row is None:
        db.session.add(Setting(key=KEY, value=current))
        changed = True
    elif row.value != current:
        row.value = current
        changed = True
    # Clear the "backup already made" marker after EVERY successful start, also when the version stayed the
    # same: after a failed upgrade and a return to the old version, the next upgrade attempt must make a new,
    # current backup instead of relying on the stale one from weeks ago.
    marker = db.session.get(Setting, BACKUP_KEY)
    if marker is not None and marker.value:
        marker.value = ""
        changed = True
    if changed:
        db.session.commit()
