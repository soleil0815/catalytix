"""Automatic database backup (SQLite).

- daily at the configured time (default 2 a.m.), only once even with several server processes
- while running, via SQLite's online backup API (consistent, no downtime)
- every backup is checked with PRAGMA integrity_check; only then is it considered complete
- the newest N backups are kept (default 10), older ones are deleted
- location: <data directory>/backups or CATALYTIX_BACKUP_DIR (e.g. a NAS drive)

With MySQL/MariaDB or PostgreSQL, better back up with the database server's own tools.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app

from .i18n import _
from .models import ROLE_ADMIN, User, db, get_bool, get_int, get_setting, set_setting

log = logging.getLogger(__name__)

PREFIX = "catalytix-"
NAME_RE = re.compile(r"^catalytix-(\d{4}-\d{2}-\d{2})_(\d{6})(-vor-\d+\.\d+\.\d+|-[a-z-]*[a-z])?(-\d+)?\.db$")
UPDATE_KIND_RE = re.compile(r"^-vor-(\d+\.\d+\.\d+)$")   # automatically when a new version starts
KEEP_UPDATE_BACKUPS = 3                                    # keep the newest of these (counted separately)
PART_MAX_AGE = 3600   # delete incomplete *.part files as leftovers of aborted runs only after 1 hour
KIND_LABELS = {"": "automatisch", "-manuell": "manuell", "-vor-wiederherstellung": "vor Wiederherstellung"}
KEEP_MIN, KEEP_MAX = 1, 100


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- Locations

def backup_dir() -> Path:
    custom = os.environ.get("CATALYTIX_BACKUP_DIR")
    d = Path(custom) if custom else Path(current_app.instance_path) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def database_file() -> Path | None:
    """Path of the SQLite file – or None for other databases or an in-memory database."""
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def supported() -> bool:
    return database_file() is not None


def human_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / 1024 / 1024:.1f} MB".replace(".", ",")


def keep_count() -> int:
    return min(KEEP_MAX, max(KEEP_MIN, get_int("backup_keep", 10)))


# ---------------------------------------------------------------- Backing up

def _integrity_ok(path: Path) -> bool:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        con.close()


def create_backup(kind: str = "", now: datetime | None = None, *, rotate_after: bool = True) -> Path:
    """Create a backup; kind: '' (automatic), '-manuell', '-vor-wiederherstellung'."""
    src_path = database_file()
    if src_path is None:
        raise BackupError("Automatische Sicherung gibt es nur für SQLite.")
    if not src_path.exists():
        raise BackupError(f"Datenbankdatei {src_path} nicht gefunden.")
    now = now or datetime.now()
    target_dir = backup_dir()
    name = f"{PREFIX}{now:%Y-%m-%d_%H%M%S}{kind}.db"
    final = target_dir / name
    n = 1
    while final.exists():  # two backups in the same second
        final = target_dir / name.replace(".db", f"-{n}.db")
        n += 1
    part = final.with_suffix(".part")
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(str(part))
    try:
        src.backup(dst)
        # The copy inherits the source's WAL mode – store it as a standalone file without -wal/-shm
        dst.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as exc:
        dst.close()
        _unlink_part(part)
        raise BackupError(f"Sicherung fehlgeschlagen: {exc}") from exc
    finally:
        src.close()
    dst.close()
    _unlink_siblings(part)
    if not _integrity_ok(part):
        _unlink_part(part)
        raise BackupError("Die Sicherung ist beschädigt (integrity_check) und wurde verworfen.")
    _unlink_siblings(part)
    try:
        os.chmod(part, 0o600)  # contains personal data
    except OSError:
        pass
    os.replace(part, final)
    if rotate_after:
        rotate()
    return final


def _unlink_siblings(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _unlink_part(path: Path) -> None:
    path.unlink(missing_ok=True)
    _unlink_siblings(path)


def list_backups() -> list[dict[str, Any]]:
    items = []
    for p in backup_dir().iterdir():
        m = NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        created = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M%S")
        kind = m.group(3) or ""
        seq = int(m.group(4)[1:]) if m.group(4) else 0   # "-1", "-2" …: later within the same second
        upd = UPDATE_KIND_RE.match(kind)
        items.append({"name": p.name, "path": p, "size": p.stat().st_size, "created": created,
                      "kind": "vor Update" if upd else KIND_LABELS.get(kind, "manuell"),
                      "before_version": upd.group(1) if upd else None, "_seq": seq})
    items.sort(key=lambda x: (x["created"], x["_seq"]), reverse=True)
    return items


def rotate(keep: int | None = None) -> int:
    keep = keep_count() if keep is None else keep
    removed = 0
    items = list_backups()
    # "before update to X" backups are counted separately: daily ones do not push them out (for going back)
    regular = [i for i in items if not i["before_version"]]
    updates = [i for i in items if i["before_version"]]
    for item in regular[keep:] + updates[KEEP_UPDATE_BACKUPS:]:
        try:
            item["path"].unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not delete old backup %s: %s", item["name"], exc)
    # Leftovers of aborted runs – but not what another process is still writing
    limit = time.time() - PART_MAX_AGE
    for stale in backup_dir().glob(f"{PREFIX}*.part*"):
        try:
            if stale.is_file() and stale.stat().st_mtime < limit:
                stale.unlink()
        except OSError:
            pass
    return removed


def find_backup(name: str) -> Path | None:
    if not NAME_RE.match(name or ""):
        return None
    p = backup_dir() / name
    # no symlinks (on a share writable by others, any arbitrary file could otherwise be offered)
    return p if p.is_file() and not p.is_symlink() else None


def delete_backup(name: str) -> bool:
    p = find_backup(name)
    if p is None:
        return False
    p.unlink()
    return True


# ---------------------------------------------------------------- Restoring (only with the app stopped!)

def restore_backup(name: str) -> Path | None:
    """Restore a backup. The current state is backed up first ("vor Wiederherstellung")."""
    src = find_backup(name)
    if src is None:
        raise BackupError(f"Backup {name} not found.")
    if not _integrity_ok(src):
        raise BackupError(f"Backup {name} is damaged.")
    target = database_file()
    if target is None:
        raise BackupError("Restoring only works with SQLite.")
    # without rotation – otherwise the very backup that is to be restored could be deleted
    safety = create_backup("-vor-wiederherstellung", rotate_after=False) if target.exists() else None
    db.session.remove()
    db.engine.dispose()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    t = sqlite3.connect(str(target), timeout=30)
    try:
        s.backup(t)
    finally:
        s.close()
        t.close()
    return safety


def find_before(version: str, since: float | None = None) -> Path | None:
    """Newest backup created when <version> started (the state of the version before it).
    since: only a backup written at or after this time – an older one from an earlier attempt that was
    already rolled back would otherwise overwrite everything entered since then."""
    found: list[Path] = [i["path"] for i in list_backups()
             if i["before_version"] == version and not i["path"].is_symlink()
             and (since is None or i["path"].stat().st_mtime >= since - 2)]
    if not found:
        return None
    # Since <since>: the OLDEST – later attempts would have backed up an already half-migrated state
    return found[-1] if since is not None else found[0]


def version_of_backup(path: Path) -> str | None:
    """Program version that last wrote to the backed-up database (None: before 3.0.0 or unreadable)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute('SELECT value FROM settings WHERE "key" = ?', ("db_version",)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] is not None else None


def find_for_version(version: str) -> Path | None:
    """For going back to <version>: newest backup that this version can read (written by it or older)."""
    from .dbversion import vtuple
    want = vtuple(version)
    if want is None:          # no valid version number → no matching backup
        return None
    for item in list_backups():
        if item["path"].is_symlink():
            continue
        have = vtuple(version_of_backup(item["path"]))
        if have is None or have <= want:
            path: Path = item["path"]
            return path
    return None


# ---------------------------------------------------------------- Schedule and status

def display_message(msg: str) -> str:
    """Stored result (German) in the current language."""
    m = re.match(r"^(\S+) gesichert \((.+)\)$", msg)
    if m:
        return _("{name} gesichert ({size})", name=m.group(1), size=m.group(2))
    m = re.match(r"^Sicherung fehlgeschlagen: (.*)$", msg, re.S)
    if m:
        return _("Sicherung fehlgeschlagen: {error}", error=m.group(1))
    m = re.match(r"^Datenbankdatei (.+) nicht gefunden\.$", msg, re.S)
    if m:
        return _("Datenbankdatei {path} nicht gefunden.", path=m.group(1))
    return _(msg)


def english(msg: str) -> str:
    """For the log and the command line: always English."""
    from .i18n import force_locale
    with force_locale("en"):
        return display_message(msg)


def _record(ok: bool, message: str, now: datetime) -> None:
    set_setting("backup_last_result", f"{now:%d.%m.%Y %H:%M}: {message}")
    if ok:
        set_setting("backup_last_ok", now.isoformat(timespec="seconds"))
        set_setting("backup_last_error", "")
    else:
        set_setting("backup_last_error", now.isoformat(timespec="seconds"))
    db.session.commit()


def run_now(kind: str = "-manuell", now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    try:
        path = create_backup(kind, now)
        size = path.stat().st_size if path.exists() else 0
    except Exception as exc:  # noqa: BLE001
        log.exception("Backup failed")
        msg = str(exc) if isinstance(exc, BackupError) else f"Sicherung fehlgeschlagen: {exc}"
        _record(False, msg, now)
        return False, msg
    msg = f"{path.name} gesichert ({human_size(size)})"
    _record(True, msg, now)
    log.info("Backup: %s", english(msg))
    return True, msg


def run_daily(now: datetime | None = None) -> tuple[bool, str] | None:
    """Called by the scheduler (every few minutes); backs up once a day from the configured time on."""
    from .mailer import _claim_today
    now = now or datetime.now()
    if not get_bool("backup_enabled") or not supported() or now.hour < get_int("backup_hour", 2):
        return None
    if not _claim_today(now.date().isoformat(), key="backup_last_run"):
        return None
    ok, msg = run_now("", now)
    if not ok:
        _notify_admins(msg)
    return ok, msg


def warning() -> str:
    """Short warning for admins if the last backup failed."""
    if not get_bool("backup_enabled") or not get_setting("backup_last_error"):
        return ""
    if get_setting("backup_last_ok") > get_setting("backup_last_error"):
        return ""
    return _("Die letzte Datensicherung ist fehlgeschlagen.")


def _notify_admins(message: str) -> None:
    from .mailer import mail_active, render_mail, send_mail
    if not mail_active():
        return
    for admin in User.query.filter_by(role=ROLE_ADMIN, active=True):
        if admin.email:
            try:
                subject, text, html = render_mail("backup_failed", user=admin, message=message)
                send_mail(admin.email, subject, text, html, kind="backup", user_id=admin.id)
            except Exception:  # noqa: BLE001
                log.exception("Could not send the notice about the failed backup")
