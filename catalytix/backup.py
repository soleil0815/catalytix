"""Automatische Datensicherung der Datenbank (SQLite).

- täglich zur eingestellten Uhrzeit (Standard 2 Uhr), auch bei mehreren Server-Prozessen nur einmal
- im laufenden Betrieb über die Online-Backup-Funktion von SQLite (konsistent, ohne Anhalten)
- jede Sicherung wird mit PRAGMA integrity_check geprüft, erst dann gilt sie als fertig
- es werden die neuesten N Sicherungen aufbewahrt (Standard 10), ältere werden gelöscht
- Ablage: <Datenverzeichnis>/backups oder CATALYTIX_BACKUP_DIR (z. B. ein NAS-Laufwerk)

Bei MySQL/MariaDB oder PostgreSQL sichert man besser mit den Werkzeugen des Datenbankservers.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from .i18n import _
from .models import ROLE_ADMIN, User, db, get_bool, get_int, get_setting, set_setting

log = logging.getLogger(__name__)

PREFIX = "catalytix-"
NAME_RE = re.compile(r"^catalytix-(\d{4}-\d{2}-\d{2})_(\d{6})(-vor-\d+\.\d+\.\d+|-[a-z-]*[a-z])?(-\d+)?\.db$")
UPDATE_KIND_RE = re.compile(r"^-vor-(\d+\.\d+\.\d+)$")   # automatisch beim Start einer neuen Version
KEEP_UPDATE_BACKUPS = 3                                    # davon die neuesten behalten (eigene Zählung)
PART_MAX_AGE = 3600   # unfertige *.part-Dateien erst nach 1 Stunde als Reste abgebrochener Läufe löschen
KIND_LABELS = {"": "automatisch", "-manuell": "manuell", "-vor-wiederherstellung": "vor Wiederherstellung"}
KEEP_MIN, KEEP_MAX = 1, 100


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- Orte

def backup_dir() -> Path:
    custom = os.environ.get("CATALYTIX_BACKUP_DIR")
    d = Path(custom) if custom else Path(current_app.instance_path) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def database_file() -> Path | None:
    """Pfad der SQLite-Datei – oder None bei anderen Datenbanken bzw. In-Memory-Datenbank."""
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


# ---------------------------------------------------------------- Sichern

def _integrity_ok(path: Path) -> bool:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def create_backup(kind: str = "", now: datetime | None = None, *, rotate_after: bool = True) -> Path:
    """Sicherung anlegen; kind: '' (automatisch), '-manuell', '-vor-wiederherstellung'."""
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
    while final.exists():  # zwei Sicherungen in derselben Sekunde
        final = target_dir / name.replace(".db", f"-{n}.db")
        n += 1
    part = final.with_suffix(".part")
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(str(part))
    try:
        src.backup(dst)
        # Die Kopie übernimmt den WAL-Modus der Quelle – als eigenständige Datei ohne -wal/-shm ablegen
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
        os.chmod(part, 0o600)  # enthält personenbezogene Daten
    except OSError:
        pass
    os.replace(part, final)
    if rotate_after:
        rotate()
    return final


def _unlink_siblings(path: Path):
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _unlink_part(path: Path):
    path.unlink(missing_ok=True)
    _unlink_siblings(path)


def list_backups() -> list[dict]:
    items = []
    for p in backup_dir().iterdir():
        m = NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        created = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M%S")
        kind = m.group(3) or ""
        seq = int(m.group(4)[1:]) if m.group(4) else 0   # „-1“, „-2“ …: später in derselben Sekunde
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
    # Sicherungen „vor Update auf X“ zählen extra: die täglichen verdrängen sie nicht (für den Weg zurück)
    regular = [i for i in items if not i["before_version"]]
    updates = [i for i in items if i["before_version"]]
    for item in regular[keep:] + updates[KEEP_UPDATE_BACKUPS:]:
        try:
            item["path"].unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not delete old backup %s: %s", item["name"], exc)
    # Reste abgebrochener Läufe – aber nicht, was ein anderer Prozess gerade noch schreibt
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
    # keine Verknüpfungen (auf einer fremd beschreibbaren Freigabe könnte sonst eine beliebige Datei angeboten werden)
    return p if p.is_file() and not p.is_symlink() else None


def delete_backup(name: str) -> bool:
    p = find_backup(name)
    if p is None:
        return False
    p.unlink()
    return True


# ---------------------------------------------------------------- Wiederherstellen (nur bei angehaltener App!)

def restore_backup(name: str) -> Path:
    """Sicherung zurückspielen. Vorher wird der aktuelle Stand gesichert („vor Wiederherstellung“)."""
    src = find_backup(name)
    if src is None:
        raise BackupError(f"Backup {name} not found.")
    if not _integrity_ok(src):
        raise BackupError(f"Backup {name} is damaged.")
    target = database_file()
    if target is None:
        raise BackupError("Restoring only works with SQLite.")
    # ohne Aufräumen – sonst könnte gerade die Sicherung gelöscht werden, die zurückgespielt werden soll
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
    """Neueste Sicherung, die beim Start von <version> angelegt wurde (Stand der Version davor).
    since: nur eine Sicherung, die ab diesem Zeitpunkt geschrieben wurde – eine ältere aus einem früheren,
    schon zurückgenommenen Versuch würde sonst alles seitdem Eingetragene überschreiben."""
    for item in list_backups():
        if item["before_version"] != version or item["path"].is_symlink():
            continue
        if since is not None and item["path"].stat().st_mtime < since - 2:
            continue
        return item["path"]
    return None


def version_of_backup(path: Path) -> str | None:
    """Programmversion, die zuletzt in die gesicherte Datenbank geschrieben hat (None: vor 3.0.0 oder unlesbar)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute('SELECT value FROM settings WHERE "key" = ?', ("db_version",)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def find_for_version(version: str) -> Path | None:
    """Für den Weg zurück zu <version>: neueste Sicherung, die diese Version lesen kann (von ihr oder älter)."""
    from .dbversion import vtuple
    want = vtuple(version)
    for item in list_backups():
        if item["path"].is_symlink():
            continue
        have = vtuple(version_of_backup(item["path"]))
        if have is None or have <= want:
            return item["path"]
    return None


# ---------------------------------------------------------------- Zeitplan und Status

def display_message(msg: str) -> str:
    """Gespeichertes Ergebnis (deutsch) in der aktuellen Sprache."""
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
    """Für Protokoll und Kommandozeile: immer englisch."""
    from .i18n import force_locale
    with force_locale("en"):
        return display_message(msg)


def _record(ok: bool, message: str, now: datetime):
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
    """Vom Zeitplan aufgerufen (alle paar Minuten); sichert einmal pro Tag ab der eingestellten Uhrzeit."""
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
    """Kurzer Warnhinweis für Admins, wenn die letzte Sicherung fehlgeschlagen ist."""
    if not get_bool("backup_enabled") or not get_setting("backup_last_error"):
        return ""
    if get_setting("backup_last_ok") > get_setting("backup_last_error"):
        return ""
    return _("Die letzte Datensicherung ist fehlgeschlagen.")


def _notify_admins(message: str):
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
