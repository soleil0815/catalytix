"""Welche Programmversion hat die Datenbank zuletzt benutzt? – Schutz beim Versionswechsel.

Beim Start (unter der Start-Sperre, VOR dem Anlegen neuer Tabellen und Spalten):
- Datenbank von einer älteren Version (oder von vor 3.0.0, ohne Eintrag): erst eine Sicherung
  „catalytix-<Zeit>-vor-<neue Version>.db“ anlegen (nur SQLite), dann migrieren, dann die neue Version eintragen.
- Datenbank von einer NEUEREN Version: nicht starten – mit klarer Meldung, welche Sicherung zurückzuspielen ist
  (oder welche Version in der .env einzutragen ist). Ohne diese Prüfung liefe ein altes Programm auf einem Schema,
  das es nicht kennt, und hinterließe Folgefehler.

CATALYTIX_SKIP_DB_CHECK=1 überspringt die Prüfung UND alle Migrationen – nur für das Zurückspielen einer Sicherung
mit einer älteren Version (update.sh bei einem missglückten Update, „flask restore-before“).
"""
from __future__ import annotations

import logging
import os
import re

from sqlalchemy import inspect, text

from . import __version__

log = logging.getLogger(__name__)

KEY = "db_version"
SKIP_ENV = "CATALYTIX_SKIP_DB_CHECK"


def vtuple(v: str | None) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


class DatabaseTooNew(SystemExit):
    """Die Datenbank stammt von einer neueren Programmversion."""


def read_stamp(db) -> str | None:
    """Gespeicherte Version (None: neue Datenbank oder Datenbank von vor 3.0.0)."""
    insp = inspect(db.engine)
    if not insp.has_table("settings"):
        return None
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM settings WHERE " + _key_col(db) + " = :k"), {"k": KEY}).first()
    return (row[0] or None) if row else None


def _key_col(db) -> str:
    return db.engine.dialect.identifier_preparer.quote("key")   # „key“ ist in MySQL ein reserviertes Wort


def is_empty(db) -> bool:
    """Noch keine Catalytix-Datenbank (Neuinstallation)."""
    insp = inspect(db.engine)
    return not (insp.has_table("users") or insp.has_table("settings"))


def too_new_message(stamp: str, current: str = __version__) -> str:
    return (f"The database was last used by Catalytix {stamp}, but this is version {current} – an older program "
            f"must not run on a newer database. Either start version {stamp} or newer again "
            f"(CATALYTIX_TAG={stamp} in .env, then: docker compose up -d), or go back to {current} with the data "
            f"from before the update: restore the backup \"catalytix-…-vor-{stamp}.db\" "
            f"(see README, \"Going back to an earlier version\"; update.sh does this automatically when a new "
            f"version fails to start). Nothing was changed.")


def prepare(db, current: str = __version__) -> str:
    """Vor den Migrationen aufrufen. Liefert 'new' (leere Datenbank), 'same', 'upgrade' oder 'skip'
    (CATALYTIX_SKIP_DB_CHECK – keine Migrationen!). Bricht bei einer neueren Datenbank ab."""
    if os.environ.get(SKIP_ENV) == "1":
        log.warning("%s=1: database version check and migrations skipped (only for restoring a backup)", SKIP_ENV)
        return "skip"
    if is_empty(db):
        return "new"
    stamp = read_stamp(db)
    have, want = vtuple(stamp), vtuple(current)
    if have is not None and want is not None and have > want:
        log.error(too_new_message(stamp, current))
        raise DatabaseTooNew("Catalytix: " + too_new_message(stamp, current))
    if have is not None and have == want:
        return "same"
    _backup_before(current, stamp)
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
        raise SystemExit(msg) from exc
    log.info("Version change %s → %s: database backed up as %s", stamp or "before 3.0.0", current, path.name)


def stamp(db, current: str = __version__) -> None:
    """Nach erfolgreichen Migrationen: diese Version eintragen."""
    from .models import Setting
    row = db.session.get(Setting, KEY)
    if row is None:
        db.session.add(Setting(key=KEY, value=current))
    elif row.value != current:
        row.value = current
    else:
        return
    db.session.commit()
