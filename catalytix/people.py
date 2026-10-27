"""Personen für die Ausleihe finden – lokal und (optional) im Active Directory.

Wer im AD steht und laut Rollenregeln die Bibliothek nutzen darf, sich aber noch nie angemeldet hat, wird in
der Suche angezeigt. Angelegt wird das Konto erst, wenn tatsächlich ein Buch an diese Person geht – und
auch dann nur als AD-Konto ohne Passwort: Anmelden kann sich die Person weiterhin nur mit ihrem AD-Passwort.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .i18n import _
from .models import Group, Loan, User, db, get_bool, get_setting

log = logging.getLogger(__name__)
LIMIT = 10
DIRECTORY_MIN_CHARS = 3        # das Verzeichnis erst ab 3 Zeichen fragen (schont das AD)
CACHE_SECONDS = 60             # gleiche Suche innerhalb einer Minute nicht erneut im AD
OUTAGE_PAUSE = 60              # nach einem Verzeichnisfehler eine Minute nur lokal suchen

_cache: dict[str, tuple[float, list]] = {}
_outage_until = 0.0
_lock = threading.Lock()


def _directory_search(q: str) -> list:
    """Verzeichnissuche mit kurzem Zwischenspeicher und Pause nach Ausfällen (pro Server-Prozess)."""
    global _outage_until
    from .ldap_auth import Directory, LDAPError
    key = q.lower()
    now = time.monotonic()
    with _lock:
        if now < _outage_until:
            raise LDAPError("pause")
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        people = Directory().search_users(q, LIMIT)
    except Exception:
        with _lock:
            _outage_until = time.monotonic() + OUTAGE_PAUSE
        raise
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.monotonic(), people)
    return people


def directory_available() -> bool:
    from .ldap_auth import ldap_enabled
    return ldap_enabled() and bool(get_setting("ldap_bind_dn"))


def _local_matches(q: str) -> list[User]:
    like = f"%{q}%"
    return (User.query.filter(User.active.is_(True), User.pending_approval.is_(False))
            .filter(or_(User.username.ilike(like), User.display_name.ilike(like), User.email.ilike(like)))
            .order_by(User.display_name, User.username).limit(LIMIT).all())


def _library_groups(info) -> list[str]:
    """In welche Bibliotheksgruppen käme diese Person (nur bei eingeschalteter Gruppen-Übernahme)?"""
    if not get_bool("ldap_group_sync"):
        return []
    from .ldap_auth import normalize_dn, rule_matches
    dns = {normalize_dn(g) for g in info.groups}
    return [g.name for g in Group.query.filter(Group.ldap_dn != "").order_by(Group.name)
            if rule_matches(g.ldap_dn, "", dns)]


def search(q: str, directory: bool = True) -> dict:
    """Ergebnis für die Suchliste: lokale Konten und noch nicht angemeldete AD-Personen.

    directory=False: nur lokale Konten (sofort). Die Seite fragt das Verzeichnis danach getrennt,
    damit bekannte Personen nicht auf das Active Directory warten."""
    q = (q or "").strip()[:100]
    result = {"results": [], "directory": directory_available(), "directory_error": "",
              "directory_pending": False}
    if len(q) < 2:
        return result
    from flask_login import current_user
    from .models import Book
    managed = {g.id for g in current_user.manageable_groups()}
    managed_names = {g.name for g in current_user.manageable_groups()}
    global_manager = current_user.is_global_manager

    def row(u: User) -> dict:
        # Gruppen-Verwalter sehen nur ihre Gruppen und Ausleihen ihres Bestands (wie an der Theke)
        q_loans = db.session.query(func.count(Loan.id)).join(Book).filter(Loan.user_id == u.id,
                                                                          Loan.returned_at.is_(None))
        if not global_manager:
            q_loans = q_loans.filter(or_(Book.group_id.is_(None), Book.group_id.in_(managed or {0})))
        return {"username": u.username, "name": u.name, "email": u.email or "",
                "groups": [g.name for g in u.groups if global_manager or g.id in managed],
                "loans": q_loans.scalar() or 0, "source": u.auth_source, "new": False}

    local = _local_matches(q)
    shown = {u.username.lower() for u in local}
    result["results"] = [row(u) for u in local]

    def add_people(people) -> int:
        """AD-Personen anhängen (ohne Doppelte); bekannte Konten erscheinen als Konto. Liefert die Zahl neuer."""
        known = {u.username.lower(): u for u in User.query.filter(
            func.lower(User.username).in_([p.username.lower() for p in people] or [""]))}
        new = 0
        for p in people:
            name = p.username.lower()
            if name in shown:
                continue
            shown.add(name)
            if name in known:     # hat schon ein Konto, passte lokal aber nicht (z. B. neue E-Mail im AD)
                if known[name].active:
                    result["results"].append(row(known[name]))
                continue
            new += 1
            result["results"].append({"username": p.username, "name": p.display_name or p.username,
                                      "email": p.email or "",
                                      "groups": [n for n in _library_groups(p) if global_manager or n in managed_names],
                                      "loans": 0, "source": "ldap", "new": True})
        return new

    if not result["directory"]:
        return result
    cache_hits = 0
    if cache_ready():
        # Aus dem Zwischenspeicher: sofort, ohne das AD zu fragen
        rows = _cache_matches(q)
        cache_hits = len(rows)
        add_people([_info_from_row(r) for r in rows][:LIMIT])
    if len(q) < DIRECTORY_MIN_CHARS or cache_hits:
        return result
    if not directory:
        # Live-Suche im AD danach getrennt – mit Cache nur, wenn er niemanden kennt (z. B. Neuzugang seit dem Abgleich)
        result["directory_pending"] = True
        return result
    from .ldap_auth import LDAPError
    try:
        people = _directory_search(q)
    except LDAPError as exc:
        log.warning("Directory person search failed: %s", exc)
        result["directory_error"] = _("Das Verzeichnis ist gerade nicht erreichbar – es werden nur bekannte Benutzer angezeigt.")
        people = []
    except Exception:  # noqa: BLE001 – Verzeichnisfehler dürfen die Suche nicht abbrechen
        log.exception("Directory person search failed")
        result["directory_error"] = _("Das Verzeichnis ist gerade nicht erreichbar – es werden nur bekannte Benutzer angezeigt.")
        people = []
    add_people(people)
    return result


class Candidate:
    """Noch nicht angelegte AD-Person – sieht für die Theke aus wie ein Benutzer."""

    def __init__(self, info):
        self.info = info
        self.username = info.username
        self.name = info.display_name or info.username
        self.email = info.email or ""
        self.group_names = _library_groups(info)
        self.is_candidate = True

    def can_borrow(self, book) -> bool:
        """Wie User.can_borrow – mit den Gruppen, die die Person beim Anlegen bekäme."""
        if book.group_id is None or self.info.role == "admin":
            return True
        if self.info.role == "librarian" and not self.group_names:
            return True
        return book.group is not None and book.group.name in self.group_names

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p]
        return ("".join(p[0] for p in parts[:2]) or self.username[:2]).upper()


_found: dict[str, tuple[float, object]] = {}


def find(username: str, cached: bool = False) -> User | Candidate | None:
    """Genau diese Person: lokales Konto oder (falls es keines gibt) berechtigte AD-Person.

    cached=True: AD-Ergebnis bis zu einer Minute wiederverwenden (für Vorschlagslisten, die bei jedem Tastendruck
    fragen). Für das eigentliche Verleihen wird immer frisch gefragt."""
    username = (username or "").strip()
    if not username:
        return None
    user = User.query.filter(func.lower(User.username) == username.lower()).first()
    if user is not None and user.ldap_gone_at is not None and not cached and directory_available():
        # vom Abgleich gesperrt – vielleicht inzwischen wieder im AD (oder der Name gehört jetzt jemand anderem)
        if _recheck_gone(user) == "retired":
            user = None
    if user is not None or not directory_available():
        return user
    key = username.lower()
    if cached:
        if cache_ready():
            from .models import DirectoryPerson
            row = DirectoryPerson.query.filter_by(username_key=key).first()
            if row is not None:
                return Candidate(_info_from_row(row))
        with _lock:
            hit = _found.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        if time.monotonic() < _outage_until:
            return None
    from .ldap_auth import Directory, LDAPError
    try:
        info = Directory().check_account(username)
    except LDAPError as exc:
        log.warning("Directory lookup for %s failed: %s", username, exc)
        return None
    result = Candidate(info) if info is not None and info.role is not None else None
    with _lock:
        if len(_found) > 500:
            _found.clear()
        _found[key] = (time.monotonic(), result)
    return result


_rechecked: dict[int, float] = {}


def _recheck_gone(user: User) -> str:
    """Vom Abgleich gesperrtes Konto an der Theke frisch prüfen (höchstens einmal pro Minute und Konto)."""
    global _outage_until
    if user.auth_source != "ldap":
        return ""   # inzwischen lokales Konto (z. B. Passwort per Kommandozeile gesetzt) – nie zurück zum AD machen
    from .ldap_auth import Directory, LDAPError
    from .views.main import AccountMismatch, account_for, same_person, sync_ldap_user
    now = time.monotonic()
    with _lock:
        if now < _outage_until or now - _rechecked.get(user.id, -1e9) < CACHE_SECONDS:
            return ""
        if len(_rechecked) > 500:
            _rechecked.clear()
        _rechecked[user.id] = now
    try:
        info = Directory().check_account(user.username)
    except LDAPError as exc:
        log.warning("Directory lookup for %s failed: %s", user.username, exc)
        with _lock:
            _outage_until = time.monotonic() + OUTAGE_PAUSE
        return ""
    if info is None or info.role is None:
        return ""
    if not same_person(user, info):
        # Der Name gehört inzwischen einer anderen Person: bisherige Person umziehen lassen oder stilllegen
        try:
            freed = account_for(info) is None
            db.session.commit()
        except AccountMismatch:
            db.session.rollback()
            return ""
        return "retired" if freed else ""
    sync_ldap_user(info, user)
    db.session.commit()
    log.info("AD account %s unlocked again (active and entitled in AD)", user.username)
    return "reactivated"


def provision(candidate: Candidate) -> User:
    """AD-Person als Konto anlegen (beim ersten Verleihen). Ein lokales Konto gleichen Namens wird nie verändert."""
    from .views.main import account_for, sync_ldap_user
    existing = account_for(candidate.info)        # kann AccountMismatch auslösen (lend_to fängt das ab)
    db.session.commit()
    if existing is not None:
        if existing.auth_source == "ldap" and existing.username.lower() != candidate.username.lower():
            sync_ldap_user(candidate.info, existing)     # im AD umbenannt: Konto zieht mit
            db.session.commit()
        return existing
    try:
        user = sync_ldap_user(candidate.info, None)
        db.session.commit()
    except IntegrityError:            # gleichzeitig von jemand anderem angelegt (Theke oder erste Anmeldung)
        db.session.rollback()
        return User.query.filter(func.lower(User.username) == candidate.username.lower()).one()
    log.info("AD account %s created when lending (role %s)", user.username, user.role)
    user._created_now = True
    return user


def lend_to(book, person, due=None, note: str = "") -> tuple[bool, str, User | None]:
    """Verleihen an ein vorhandenes Konto oder eine AD-Person (wird dafür angelegt; scheitert die Ausleihe,
    wird das neue Konto wieder entfernt)."""
    from .views.loans import lend_book
    created = None
    if not book.lendable:                     # Präsenzbestand: dafür kein Konto anlegen
        return (*lend_book(book, person, due, note), None)
    if isinstance(person, Candidate):
        from .views.main import AccountMismatch
        try:
            person = provision(person)
        except AccountMismatch:
            db.session.rollback()
            return False, _("Für {name} gibt es ein älteres Konto einer anderen Person, das gerade nicht geklärt "
                            "werden kann (AD nicht erreichbar?). Bitte später erneut versuchen.",
                            name=person.username), None
        created = person if getattr(person, "_created_now", False) else None
    ok, msg = lend_book(book, person, due, note)
    if not ok and created is not None:
        # Wieder entfernen – aber nur, wenn das Konto inzwischen weder benutzt noch angemeldet wurde
        db.session.rollback()
        from sqlalchemy import delete, exists
        db.session.execute(delete(User).where(User.id == created.id, User.last_login.is_(None),
                                               ~exists().where(Loan.user_id == created.id)))
        db.session.commit()
        return ok, msg, None
    if ok and created is not None:
        msg = msg + " " + _("{name} wurde aus dem Active Directory übernommen.", name=created.name)
    return ok, msg, person


# ---------------------------------------------------------------- Such-Cache: AD-Personen im Hintergrund abgleichen

FULL_RESOLVE_HOURS = 24        # verschachtelte Gruppen spätestens nach einem Tag neu auflösen
RUN_LOCK_MINUTES = 30          # ohne Lebenszeichen gilt ein Abgleich nach dieser Zeit als abgebrochen
RETRY_MINUTES = 15             # nach einem Fehler frühestens so viel später erneut versuchen
CACHE_HOURS = (1, 2, 4, 6, 12, 24)
CACHE_MAX_AGE_HOURS = 48       # älterer Cache (AD lange nicht erreichbar) wird nicht mehr benutzt
ACCOUNT_BRAKE_SHARE = 0.3      # mehr als 30 % der AD-Konten auf einmal sperren/umbenennen? → lieber gar nicht
ACCOUNT_BRAKE_MIN = 5


class _LockLost(Exception):
    """Ein anderer Prozess hat den Abgleich übernommen (dieser galt als abgebrochen)."""


def cache_enabled() -> bool:
    return directory_available() and get_bool("ldap_cache_enabled")


def cache_ready() -> bool:
    """Cache benutzen? Nur wenn eingeschaltet, mindestens einmal erfolgreich abgeglichen und nicht veraltet."""
    from datetime import datetime, timedelta
    if not cache_enabled():
        return False
    ok_at = get_setting("dircache_ok_at") or ""
    if not ok_at:
        return False
    max_age = max(CACHE_MAX_AGE_HOURS, 3 * get_int_safe("ldap_cache_hours", 1))
    return ok_at >= (datetime.now() - timedelta(hours=max_age)).isoformat(timespec="seconds")


def _claim(key: str, new: str, free_when) -> bool:
    """Setting atomar von einem „freien“ Wert auf new setzen (mehrere Server-Prozesse)."""
    from sqlalchemy import update
    from .models import Setting
    if db.session.get(Setting, key) is None:
        try:
            db.session.add(Setting(key=key, value=""))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    res = db.session.execute(update(Setting).where(Setting.key == key, free_when(Setting.value)).values(value=new))
    db.session.commit()
    return res.rowcount == 1


def _token() -> str:
    import uuid
    from datetime import datetime
    return f"{datetime.now().isoformat(timespec='seconds')}|{uuid.uuid4().hex[:8]}"


def _acquire_run() -> str:
    """Abgleich-Sperre holen. Liefert das Kennzeichen (für Lebenszeichen und Freigabe) oder "" wenn belegt."""
    from datetime import datetime, timedelta
    stale = (datetime.now() - timedelta(minutes=RUN_LOCK_MINUTES)).isoformat(timespec="seconds")
    token = _token()
    ok = _claim("dircache_running", token, lambda v: or_(v == "", v.is_(None), v < stale))
    return token if ok else ""


def _heartbeat(token: str) -> str:
    """Lebenszeichen: Sperre auffrischen – nur wenn sie noch uns gehört."""
    from sqlalchemy import update
    from .models import Setting
    new = _token()
    res = db.session.execute(update(Setting).where(Setting.key == "dircache_running", Setting.value == token)
                             .values(value=new))
    db.session.commit()
    if res.rowcount != 1:
        raise _LockLost()
    return new


def _release_run(token: str) -> None:
    from sqlalchemy import update
    from .models import Setting
    db.session.execute(update(Setting).where(Setting.key == "dircache_running", Setting.value == token)
                       .values(value=""))
    db.session.commit()


def is_running() -> bool:
    from datetime import datetime, timedelta
    v = get_setting("dircache_running") or ""
    return bool(v) and v >= (datetime.now() - timedelta(minutes=RUN_LOCK_MINUTES)).isoformat(timespec="seconds")


def clear_cache() -> int:
    from .models import DirectoryPerson, set_setting
    n = DirectoryPerson.query.delete()
    set_setting("dircache_ok_at", "")
    db.session.commit()
    return n


def sync_directory(manual: bool = False) -> dict:
    """AD-Personen abgleichen: neue aufnehmen, geänderte aktualisieren, deaktivierte, gelöschte und nicht mehr
    berechtigte entfernen, bestehende AD-Konten nachziehen. Bei einem Fehler bleibt alles unverändert."""
    import json
    from datetime import datetime, timedelta
    from .ldap_auth import Directory, LDAPError
    from .models import DirectoryPerson, set_setting

    started = datetime.now()
    stats = {"at": started.isoformat(timespec="minutes"), "manual": manual}
    if not directory_available():
        stats["skipped"] = True
        return stats
    token = _acquire_run()
    if not token:
        stats["busy"] = True
        return stats
    state = {"token": token}
    try:
        # Bisherigen Stand als einfache Werte merken und die Lese-Transaktion beenden: Während der (evtl.
        # minutenlangen) AD-Abfrage darf die Datenbank nicht blockiert sein.
        limit = started - timedelta(hours=FULL_RESOLVE_HOURS)
        known = {(p.dn or "").lower(): (p.group_key, p.resolved_at, p.groups)
                 for p in DirectoryPerson.query.with_entities(DirectoryPerson.dn, DirectoryPerson.group_key,
                                                              DirectoryPerson.resolved_at, DirectoryPerson.groups)}
        db.session.commit()

        def reuse(dn, key):
            k = known.get((dn or "").lower())
            if k is not None and k[0] == key and k[1] and k[1] > limit:
                return [g for g in (k[2] or "").split("\n") if g]
            return None

        def progress(_n, _total):
            state["token"] = _heartbeat(state["token"])

        try:
            people = Directory().scan_people(reuse, progress=progress)
        except _LockLost:
            raise
        except Exception as exc:  # noqa: BLE001 – Ausfall: alles behalten, Fehler anzeigen
            if not isinstance(exc, LDAPError):
                log.exception("AD sync failed")
            stats["error"] = str(exc) or exc.__class__.__name__
            return stats
        state["token"] = _heartbeat(state["token"])
        if not directory_available():         # AD-Anbindung während des Laufs abgeschaltet: nichts ändern
            stats["skipped"] = True
            return stats
        uncertain = list(getattr(people, "uncertain", []))
        with_cache = cache_enabled()          # Cache aus: nur die bestehenden Konten nachziehen, nichts speichern
        if with_cache:
            stats.update(_store_cache(people, uncertain, started))
            db.session.commit()
        stats["cache"] = with_cache
        stats.update(_sync_accounts(people, uncertain, started))
        stats.update(count=len({i.username.lower() for i, _k, _f in people}),
                     resolved=sum(1 for _i, _k, f in people if f), uncertain=len(uncertain),
                     seconds=round((datetime.now() - started).total_seconds(), 1))
        if with_cache and cache_enabled():
            set_setting("dircache_ok_at", started.isoformat(timespec="seconds"))
        db.session.commit()
        if not cache_enabled() and (get_setting("dircache_ok_at") or _cache_rows()):
            clear_cache()                     # während des Laufs abgeschaltet
        with _lock:
            _cache.clear()
            _found.clear()
        log.info("AD sync: %s", stats)
        return stats
    except _LockLost:
        db.session.rollback()
        log.warning("AD sync aborted: another process took over")
        stats["error"] = _("abgebrochen – ein anderer Abgleich hat übernommen")
        state["token"] = ""
        return stats
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.exception("AD sync failed")
        stats["error"] = str(exc) or exc.__class__.__name__
        return stats
    finally:
        try:
            if stats.get("error") or stats.get("skipped"):
                db.session.rollback()
            if stats.get("skipped") and (get_setting("dircache_ok_at") or _cache_rows()):
                clear_cache()
            set_setting("dircache_result", json.dumps(stats))
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
        if state["token"]:
            try:
                _release_run(state["token"])
            except Exception:  # noqa: BLE001
                db.session.rollback()


def _store_cache(people, uncertain, started) -> dict:
    """Cache-Tabelle auf den Stand des Abgleichs bringen; nur Geändertes schreiben."""
    from .models import DirectoryPerson
    unsure_names = {n for n, _g in uncertain if n}
    unsure_guids = {g for _n, g in uncertain if g}
    old = {p.username_key: p for p in DirectoryPerson.query}
    seen, added, changed = set(), 0, 0
    for info, key, fresh in people:
        k = info.username.lower()
        if k in seen:
            continue
        seen.add(k)
        values = {"username": info.username, "display_name": (info.display_name or "")[:300],
                  "email": (info.email or "")[:300], "dn": (info.dn or "")[:1000], "role": info.role or "",
                  "groups": "\n".join(info.groups), "group_key": key, "guid": info.guid or ""}
        row = old.get(k)
        if row is None:
            db.session.add(DirectoryPerson(username_key=k, resolved_at=started, synced_at=started, **values))
            added += 1
            continue
        if any(getattr(row, f) != v for f, v in values.items()):
            changed += 1
            for f, v in values.items():
                setattr(row, f, v)
            row.synced_at = started
        if fresh:
            row.resolved_at = started
    gone = [k for k, row in old.items()
            if k not in seen and k not in unsure_names and not (row.guid and row.guid in unsure_guids)]
    for k in gone:            # im AD deaktiviert, gelöscht oder nicht mehr berechtigt
        db.session.delete(old[k])
    return {"added": added, "changed": changed, "removed": len(gone)}


def _sync_accounts(people, uncertain, started) -> dict:
    """Bestehende AD-Konten nachziehen: Name, E-Mail, Rolle, Gruppen, Umbenennungen – auch bei Personen, die sich
    nie anmelden (an der Theke angelegt). Im AD deaktivierte, gelöschte oder nicht mehr berechtigte Konten
    werden gesperrt (nicht gelöscht – daran hängen Ausleihen) und wieder freigegeben, sobald sie zurück sind.

    Reihenfolge (unabhängig von der Reihenfolge im AD):
    1. Konten ausgeschiedener Personen, deren Name jetzt jemand anderes trägt, stilllegen (Name wird frei).
    2. Konten über die feste AD-Kennung zuordnen (auch nach Umbenennung).
    3. Übrige über den Namen zuordnen – nur, wenn das Konto keine andere Kennung trägt."""
    from sqlalchemy.orm import selectinload
    from .views.main import retire_account, same_person, sync_ldap_user

    accounts = User.query.options(selectinload(User.groups)).filter(User.auth_source == "ldap").all()
    ldap_groups = Group.query.filter(Group.ldap_dn != "").all()
    group_sync = get_bool("ldap_group_sync")
    unsure_names = {n for n, _g in uncertain if n}
    unsure_guids = {g for _n, g in uncertain if g}
    every = [info for info, _key, _fresh in people]
    infos, names = [], set()
    for info in every:                         # doppelte Benutzernamen im AD: für die Namenszuordnung zählt der erste
        if info.username.lower() not in names:
            names.add(info.username.lower())
            infos.append(info)
    scan_guids = {i.guid for i in every if i.guid}

    def unsure(u):
        return u.username.lower() in unsure_names or (u.ldap_guid and u.ldap_guid in unsure_guids)

    def snapshot(u):
        return (u.username, u.display_name, u.email, u.role, u.active, tuple(sorted(g.id for g in u.groups)))

    matched = set()
    # 1. Konten ausgeschiedener Personen, deren Name jetzt jemand anderes trägt – erst vormerken: sie zählen mit
    #    zur Sicherheitsbremse (z. B. Domänen-Migration mit lauter neuen Kennungen) und werden erst danach stillgelegt
    to_retire = [u for u in accounts if u.ldap_guid and u.ldap_guid not in scan_guids and not unsure(u)
                 and u.username.lower() in names]
    matched.update(u.id for u in to_retire)
    by_guid = {u.ldap_guid: u for u in accounts if u.ldap_guid and u.id not in matched}
    pairs, seen_guids = [], set()
    for i in every:
        if i.guid and i.guid in by_guid and i.guid not in seen_guids:
            seen_guids.add(i.guid)
            pairs.append((by_guid[i.guid], i))
    # Bremse: sehr viele Umbenennungen auf einmal (z. B. anderes Anmeldeattribut eingestellt) → nicht umbenennen
    renames = sum(1 for u, i in pairs if u.username.lower() != i.username.lower())
    allow_rename = not (renames > ACCOUNT_BRAKE_MIN and renames > ACCOUNT_BRAKE_SHARE * max(1, len(accounts)))
    updated, renamed, reactivated = 0, 0, 0

    def apply(u, info):
        nonlocal updated, renamed, reactivated
        matched.add(u.id)
        before, was_gone = snapshot(u), u.ldap_gone_at is not None
        sync_ldap_user(info, u, ldap_groups, rename=allow_rename, group_sync=group_sync)
        after = snapshot(u)
        if after != before:
            updated += 1
            renamed += before[0] != after[0]
            reactivated += was_gone and u.active

    # 2. über die feste Kennung
    done_infos = set()
    for u, info in pairs:
        apply(u, info)
        done_infos.add(id(info))
    db.session.flush()
    # 3. über den Namen (Konten ohne Kennung, z. B. ältere Konten oder OpenLDAP ohne entryUUID)
    by_name = {u.username.lower(): u for u in accounts if u.id not in matched}
    for info in infos:
        if id(info) in done_infos:
            continue
        u = by_name.get(info.username.lower())
        if u is None or u.id in matched or not same_person(u, info):
            continue
        apply(u, info)

    result = {"accounts_updated": updated, "accounts_renamed": renamed, "accounts_reactivated": reactivated,
              "accounts_disabled": 0}
    if not allow_rename:
        result["rename_brake"] = renames
        log.warning("AD sync: %d accounts would be renamed – safety brake, no renaming. "
                    "Check the sign-in attribute.", renames)
    gone = [u for u in accounts
            if u.id not in matched and u.active and u.ldap_gone_at is None and not unsure(u)]
    active_total = sum(1 for u in accounts if u.active)
    hits = len(gone) + sum(1 for u in to_retire if u.active)
    if hits > ACCOUNT_BRAKE_MIN and hits > ACCOUNT_BRAKE_SHARE * active_total:
        log.warning("AD sync: %d of %d AD accounts would be locked – safety brake, none locked. Check the "
                    "base DN, user filter, role rules and (after a domain migration) the AD identifiers.",
                    hits, active_total)
        result["accounts_brake"] = hits
        return result
    for u in to_retire:
        retire_account(u)
    for u in gone:
        u.active = False
        u.ldap_gone_at = started
        u.auth_epoch = (u.auth_epoch or 0) + 1     # laufende Sitzungen beenden
        log.info("AD account %s locked: disabled, deleted or no longer entitled in AD", u.username)
    if to_retire and allow_rename:
        # Umbenennungen, die bisher am Namen eines jetzt stillgelegten Kontos scheiterten, gleich nachholen
        db.session.flush()
        for u, info in pairs:
            if u.username.lower() != info.username.lower():
                before = u.username
                sync_ldap_user(info, u, ldap_groups, group_sync=group_sync)
                if u.username != before:
                    result["accounts_renamed"] += 1
    result["accounts_disabled"] = len(to_retire) + len(gone)
    return result


def run_scheduled_sync(now=None) -> dict | None:
    """Vom Hintergrund-Planer aufgerufen: einmal je Zeitfenster (alle ldap_cache_hours Stunden)."""
    from datetime import datetime, timedelta
    from .models import set_setting
    now = now or datetime.now()
    if not cache_enabled() and (get_setting("dircache_ok_at") or _cache_rows()):
        clear_cache()         # abgeschaltet oder AD aus: zwischengespeicherte Personen nicht aufbewahren
    if not directory_available():
        return None
    retry_after = get_setting("dircache_retry_after") or ""
    if retry_after and retry_after > now.isoformat(timespec="seconds"):
        return None
    # Ohne Cache werden die Gruppen jedes Mal komplett neu aufgelöst – dann nur einmal täglich (schont das AD)
    hours = get_int_safe("ldap_cache_hours", 1) if cache_enabled() else 24
    slot = f"{now.date().isoformat()}T{(now.hour // hours) * hours:02d}|{hours}"
    previous = get_setting("dircache_slot") or ""
    if not _claim("dircache_slot", slot, lambda v: or_(v.is_(None), v != slot)):
        return None
    stats = sync_directory()
    if stats.get("error") or stats.get("busy"):
        # Fehler oder belegt: Zeitfenster wieder freigeben – nach RETRY_MINUTES erneut versuchen
        from sqlalchemy import update
        from .models import Setting
        db.session.execute(update(Setting).where(Setting.key == "dircache_slot", Setting.value == slot)
                           .values(value=previous))
        set_setting("dircache_retry_after", (now + timedelta(minutes=RETRY_MINUTES)).isoformat(timespec="seconds"))
        db.session.commit()
    elif retry_after:
        set_setting("dircache_retry_after", "")
        db.session.commit()
    return stats


def _cache_rows() -> int:
    from .models import DirectoryPerson
    return DirectoryPerson.query.count()


def get_int_safe(key: str, default: int) -> int:
    try:
        v = int(get_setting(key) or default)
    except (TypeError, ValueError):
        return default
    return v if v in CACHE_HOURS else default


def start_sync_async(app) -> bool:
    """„Jetzt abgleichen“: im Hintergrund starten, die Seite wartet nicht. False, wenn schon einer läuft."""
    if is_running():
        return False

    def work():
        with app.app_context():      # die Datenbank-Sitzung endet mit dem Kontext
            try:
                sync_directory(manual=True)
            except Exception:  # noqa: BLE001
                log.exception("AD sync failed")
    threading.Thread(target=work, name="catalytix-ad-sync", daemon=True).start()
    return True


def last_result() -> dict:
    import json
    try:
        return json.loads(get_setting("dircache_result") or "{}")
    except ValueError:
        return {}


def _cache_matches(q: str) -> list:
    from .models import DirectoryPerson
    like = f"%{q.replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%"
    return (DirectoryPerson.query.filter(or_(DirectoryPerson.username.ilike(like, escape="\\"),
                                             DirectoryPerson.display_name.ilike(like, escape="\\"),
                                             DirectoryPerson.email.ilike(like, escape="\\")))
            .order_by(DirectoryPerson.display_name, DirectoryPerson.username).limit(LIMIT * 2).all())


def _info_from_row(p):
    from .ldap_auth import LDAPUser
    return LDAPUser(username=p.username, dn=p.dn or "", display_name=p.display_name or "", email=p.email or "",
                    groups=[g for g in (p.groups or "").split("\n") if g], role=p.role or None)


def result_text(res: dict) -> str:
    """Letztes Abgleich-Ergebnis in der Sprache des Betrachters."""
    from datetime import datetime
    from .i18n import _n, format_datetime
    if not res or not res.get("at"):
        return _("Noch kein Abgleich.")
    try:
        when = format_datetime(datetime.fromisoformat(res["at"]))
    except ValueError:
        when = res["at"]
    if res.get("manual"):
        when += " " + _("(manuell)")
    if res.get("error"):
        return _("{when}: Fehler – {error}. Es wurde nichts geändert.", when=when, error=res["error"])
    if res.get("skipped"):
        return _("{when}: übersprungen (kein Dienstkonto).", when=when)
    if res.get("cache", True):
        text = _n("{n} Person im Cache", "{n} Personen im Cache", res.get("count", 0))
        text += " · " + _("{a} neu, {c} geändert, {r} entfernt", a=res.get("added", 0), c=res.get("changed", 0),
                          r=res.get("removed", 0))
    else:
        text = _n("{n} berechtigte Person im AD (Cache aus)", "{n} berechtigte Personen im AD (Cache aus)",
                  res.get("count", 0))
    acc = [_n("{n} Konto aktualisiert", "{n} Konten aktualisiert", res.get("accounts_updated", 0))]
    if res.get("accounts_renamed"):
        acc.append(_n("{n} umbenannt", "{n} umbenannt", res["accounts_renamed"]))
    acc.append(_n("{n} gesperrt", "{n} gesperrt", res.get("accounts_disabled", 0)))
    if res.get("accounts_reactivated"):
        acc.append(_n("{n} wieder freigegeben", "{n} wieder freigegeben", res["accounts_reactivated"]))
    text += " · " + ", ".join(acc)
    from .i18n import get_locale
    secs = str(res.get("seconds", 0))
    text += " · " + _("{s} s", s=secs.replace(".", ",") if get_locale() == "de" else secs)
    if res.get("uncertain"):
        text += " · " + _n("{n} Person ungewiss (Gruppen nicht ermittelbar) – unverändert gelassen",
                           "{n} Personen ungewiss (Gruppen nicht ermittelbar) – unverändert gelassen", res["uncertain"])
    if res.get("rename_brake"):
        text += " · " + _("Sicherheitsbremse: {n} Konten wären umbenannt worden – keines umbenannt. Anmeldeattribut "
                          "prüfen.", n=res["rename_brake"])
    if res.get("accounts_brake"):
        text += " · " + _("Sicherheitsbremse: {n} Konten wären gesperrt worden – keines gesperrt. Basis-DN, "
                          "Benutzerfilter und Rollenregeln prüfen.", n=res["accounts_brake"])
    return f"{when}: {text}"
