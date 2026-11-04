"""Finding people for lending – locally and (optionally) in Active Directory.

Anyone who is in AD and may use the library according to the role rules, but has never logged in, is shown in
the search. The account is created only when a book actually goes to this person – and
even then only as an AD account without a password: the person can still log in only with their AD password.
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
DIRECTORY_MIN_CHARS = 3        # only query the directory from 3 characters on (spares the AD)
CACHE_SECONDS = 60             # don't query AD again for the same search within a minute
OUTAGE_PAUSE = 60              # after a directory error, search only locally for one minute

_cache: dict[str, tuple[float, list]] = {}
_outage_until = 0.0
_lock = threading.Lock()


def _directory_search(q: str) -> list:
    """Directory search with a short cache and a pause after failures (per server process)."""
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
    """Which library groups would this person get (only with group import enabled)?"""
    if not get_bool("ldap_group_sync"):
        return []
    from .ldap_auth import normalize_dn, rule_matches
    dns = {normalize_dn(g) for g in info.groups}
    return [g.name for g in Group.query.filter(Group.ldap_dn != "").order_by(Group.name)
            if rule_matches(g.ldap_dn, "", dns)]


def search(q: str, directory: bool = True) -> dict:
    """Result for the search list: local accounts and AD people who have not logged in yet.

    directory=False: local accounts only (immediately). The page then queries the directory separately,
    so known people don't have to wait for Active Directory."""
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
        # group librarians only see their groups and loans of their collection (as at the desk)
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
        """Append AD people (without duplicates); known accounts appear as accounts. Returns the number of new ones."""
        known = {u.username.lower(): u for u in User.query.filter(
            func.lower(User.username).in_([p.username.lower() for p in people] or [""]))}
        new = 0
        for p in people:
            name = p.username.lower()
            if name in shown:
                continue
            shown.add(name)
            if name in known:     # already has an account but didn't match locally (e.g. new e-mail in AD)
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
        # from the cache: immediately, without querying AD
        rows = _cache_matches(q)
        cache_hits = len(rows)
        add_people([_info_from_row(r) for r in rows][:LIMIT])
    if len(q) < DIRECTORY_MIN_CHARS or cache_hits:
        return result
    if not directory:
        # live AD search separately afterwards – with cache only if it knows no one (e.g. newcomer since the sync)
        result["directory_pending"] = True
        return result
    from .ldap_auth import LDAPError
    try:
        people = _directory_search(q)
    except LDAPError as exc:
        log.warning("Directory person search failed: %s", exc)
        result["directory_error"] = _("Das Verzeichnis ist gerade nicht erreichbar – es werden nur bekannte Benutzer angezeigt.")
        people = []
    except Exception:  # noqa: BLE001 – directory errors must not abort the search
        log.exception("Directory person search failed")
        result["directory_error"] = _("Das Verzeichnis ist gerade nicht erreichbar – es werden nur bekannte Benutzer angezeigt.")
        people = []
    add_people(people)
    return result


class Candidate:
    """AD person without an account yet – looks like a user to the desk."""

    def __init__(self, info):
        self.info = info
        self.username = info.username
        self.name = info.display_name or info.username
        self.email = info.email or ""
        self.group_names = _library_groups(info)
        self.is_candidate = True

    def can_borrow(self, book) -> bool:
        """Like User.can_borrow – with the groups the person would get on creation."""
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
    """Exactly this person: local account or (if there is none) an authorized AD person.

    cached=True: reuse the AD result for up to a minute (for suggestion lists that query on every
    keystroke). For the actual lending, AD is always queried fresh."""
    username = (username or "").strip()
    if not username:
        return None
    user = User.query.filter(func.lower(User.username) == username.lower()).first()
    if user is not None and user.ldap_gone_at is not None and not cached and directory_available():
        # disabled by the sync – maybe back in AD by now (or the name now belongs to someone else)
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
    """Re-check an account disabled by the sync fresh at the desk (at most once per minute and account)."""
    global _outage_until
    if user.auth_source != "ldap":
        return ""   # meanwhile a local account (e.g. password set via command line) – never turn it back into AD
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
        # the name now belongs to another person: move the previous person or retire their account
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
    """Create an AD person as an account (on first lending). A local account of the same name is never changed."""
    from .views.main import account_for, sync_ldap_user
    existing = account_for(candidate.info)        # may raise AccountMismatch (lend_to catches it)
    db.session.commit()
    if existing is not None:
        if existing.auth_source == "ldap" and existing.username.lower() != candidate.username.lower():
            sync_ldap_user(candidate.info, existing)     # renamed in AD: the account follows
            db.session.commit()
        return existing
    try:
        user = sync_ldap_user(candidate.info, None)
        db.session.commit()
    except IntegrityError:            # created by someone else at the same time (desk or first login)
        db.session.rollback()
        return User.query.filter(func.lower(User.username) == candidate.username.lower()).one()
    log.info("AD account %s created when lending (role %s)", user.username, user.role)
    user._created_now = True
    return user


def lend_to(book, person, due=None, note: str = "") -> tuple[bool, str, User | None]:
    """Lend to an existing account or an AD person (created for this; if the loan fails,
    the new account is removed again)."""
    from .views.loans import lend_book
    created = None
    if not book.lendable:                     # "Präsenzbestand" (UI term: reference-only stock): no account
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
        # remove again – but only if the account has neither been used nor logged in meanwhile
        db.session.rollback()
        from sqlalchemy import delete, exists
        db.session.execute(delete(User).where(User.id == created.id, User.last_login.is_(None),
                                               ~exists().where(Loan.user_id == created.id)))
        db.session.commit()
        return ok, msg, None
    if ok and created is not None:
        msg = msg + " " + _("{name} wurde aus dem Active Directory übernommen.", name=created.name)
    return ok, msg, person


# ---------------------------------------------------------------- Search cache: sync AD people in the background

FULL_RESOLVE_HOURS = 24        # re-resolve nested groups after one day at the latest
RUN_LOCK_MINUTES = 30          # without a heartbeat, a sync counts as aborted after this time
RETRY_MINUTES = 15             # after an error, retry no sooner than this much later
CACHE_HOURS = (1, 2, 4, 6, 12, 24)
CACHE_MAX_AGE_HOURS = 48       # an older cache (AD unreachable for a long time) is no longer used
ACCOUNT_BRAKE_SHARE = 0.3      # disable/rename more than 30 % of the AD accounts at once? → better not at all
ACCOUNT_BRAKE_MIN = 5


class _LockLost(Exception):
    """Another process has taken over the sync (this one was considered aborted)."""


def cache_enabled() -> bool:
    return directory_available() and get_bool("ldap_cache_enabled")


def cache_ready() -> bool:
    """Use the cache? Only if enabled, synced successfully at least once and not stale."""
    from datetime import datetime, timedelta
    if not cache_enabled():
        return False
    ok_at = get_setting("dircache_ok_at") or ""
    if not ok_at:
        return False
    max_age = max(CACHE_MAX_AGE_HOURS, 3 * get_int_safe("ldap_cache_hours", 1))
    return ok_at >= (datetime.now() - timedelta(hours=max_age)).isoformat(timespec="seconds")


def _claim(key: str, new: str, free_when) -> bool:
    """Atomically set a setting from a "free" value to new (multiple server processes)."""
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
    """Acquire the sync lock. Returns the token (for heartbeat and release) or "" if taken."""
    from datetime import datetime, timedelta
    stale = (datetime.now() - timedelta(minutes=RUN_LOCK_MINUTES)).isoformat(timespec="seconds")
    token = _token()
    ok = _claim("dircache_running", token, lambda v: or_(v == "", v.is_(None), v < stale))
    return token if ok else ""


def _heartbeat(token: str) -> str:
    """Heartbeat: refresh the lock – only if it still belongs to us."""
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
    """Sync AD people: add new ones, update changed ones, remove disabled, deleted and no longer
    authorized ones, update existing AD accounts. On an error, everything stays unchanged."""
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
        # Remember the previous state as plain values and end the read transaction: during the (possibly
        # minutes-long) AD query the database must not be blocked.
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
        except Exception as exc:  # noqa: BLE001 – failure: keep everything, show the error
            if not isinstance(exc, LDAPError):
                log.exception("AD sync failed")
            stats["error"] = str(exc) or exc.__class__.__name__
            return stats
        state["token"] = _heartbeat(state["token"])
        if not directory_available():         # AD connection disabled during the run: change nothing
            stats["skipped"] = True
            return stats
        uncertain = list(getattr(people, "uncertain", []))
        with_cache = cache_enabled()          # cache off: only update the existing accounts, store nothing
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
            clear_cache()                     # disabled during the run
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
    """Bring the cache table up to the state of the sync; write only what changed."""
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
    for k in gone:            # disabled, deleted or no longer authorized in AD
        db.session.delete(old[k])
    return {"added": added, "changed": changed, "removed": len(gone)}


def _sync_accounts(people, uncertain, started) -> dict:
    """Update existing AD accounts: name, e-mail, role, groups, renames – also for people who
    never log in (created at the desk). Accounts disabled, deleted or no longer authorized in AD
    are locked (not deleted – loans depend on them) and unlocked again as soon as they are back.

    Order (independent of the order in AD):
    1. Retire accounts of departed people whose name is now held by someone else (frees the name).
    2. Match accounts via the fixed AD identifier (also after a rename).
    3. Match the rest by name – only if the account doesn't carry a different identifier."""
    from sqlalchemy.orm import selectinload
    from .views.main import retire_account, same_person, sync_ldap_user

    accounts = User.query.options(selectinload(User.groups)).filter(User.auth_source == "ldap").all()
    ldap_groups = Group.query.filter(Group.ldap_dn != "").all()
    group_sync = get_bool("ldap_group_sync")
    unsure_names = {n for n, _g in uncertain if n}
    unsure_guids = {g for _n, g in uncertain if g}
    every = [info for info, _key, _fresh in people]
    infos, names = [], set()
    for info in every:                         # duplicate usernames in AD: the first one counts for name matching
        if info.username.lower() not in names:
            names.add(info.username.lower())
            infos.append(info)
    scan_guids = {i.guid for i in every if i.guid}

    def unsure(u):
        return u.username.lower() in unsure_names or (u.ldap_guid and u.ldap_guid in unsure_guids)

    def snapshot(u):
        return (u.username, u.display_name, u.email, u.role, u.active, tuple(sorted(g.id for g in u.groups)))

    matched = set()
    # 1. Accounts of departed people whose name is now held by someone else – only mark them first: they count
    #    toward the safety brake (e.g. domain migration with all-new identifiers) and are retired only afterwards
    to_retire = [u for u in accounts if u.ldap_guid and u.ldap_guid not in scan_guids and not unsure(u)
                 and u.username.lower() in names]
    matched.update(u.id for u in to_retire)
    by_guid = {u.ldap_guid: u for u in accounts if u.ldap_guid and u.id not in matched}
    pairs, seen_guids = [], set()
    for i in every:
        if i.guid and i.guid in by_guid and i.guid not in seen_guids:
            seen_guids.add(i.guid)
            pairs.append((by_guid[i.guid], i))
    # brake: very many renames at once (e.g. a different login attribute configured) → don't rename
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

    # 2. via the fixed identifier
    done_infos = set()
    for u, info in pairs:
        apply(u, info)
        done_infos.add(id(info))
    db.session.flush()
    # 3. by name (accounts without an identifier, e.g. older accounts or OpenLDAP without entryUUID)
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
        u.auth_epoch = (u.auth_epoch or 0) + 1     # end running sessions
        log.info("AD account %s locked: disabled, deleted or no longer entitled in AD", u.username)
    if to_retire and allow_rename:
        # renames that previously failed on the name of a now-retired account: catch up right away
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
    """Called by the background scheduler: once per time window (every ldap_cache_hours hours)."""
    from datetime import datetime, timedelta
    from .models import set_setting
    now = now or datetime.now()
    if not cache_enabled() and (get_setting("dircache_ok_at") or _cache_rows()):
        clear_cache()         # disabled or AD off: don't keep cached people
    if not directory_available():
        return None
    retry_after = get_setting("dircache_retry_after") or ""
    if retry_after and retry_after > now.isoformat(timespec="seconds"):
        return None
    # without a cache the groups are fully re-resolved every time – then only once a day (spares the AD)
    hours = get_int_safe("ldap_cache_hours", 1) if cache_enabled() else 24
    slot = f"{now.date().isoformat()}T{(now.hour // hours) * hours:02d}|{hours}"
    previous = get_setting("dircache_slot") or ""
    if not _claim("dircache_slot", slot, lambda v: or_(v.is_(None), v != slot)):
        return None
    stats = sync_directory()
    if stats.get("error") or stats.get("busy"):
        # error or taken: release the time window again – retry after RETRY_MINUTES
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
    """"Sync now": start in the background, the page doesn't wait. False if one is already running."""
    if is_running():
        return False

    def work():
        with app.app_context():      # the database session ends with the context
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
    """Last sync result in the viewer's language."""
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
