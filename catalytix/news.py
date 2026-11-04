"""In-app news: short, easy-to-understand notes on new features and security updates.

The entries are in news.json (per version, German and English, with target audience). Each account stores in
users.news_seen up to which version it has seen the news. New accounts start at the current state
(no old messages); accounts from before this feature (NULL) see everything since its introduction (2.17.0) as new.
"""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

KINDS = ("security", "new", "improved", "fixed")        # order on the page: security first
AUDIENCES = ("all", "librarian", "admin")
INTRODUCED = "2.17.0"     # news exists from this version on


def vtuple(version: str) -> tuple:
    try:
        return tuple(int(x) for x in str(version).split("."))
    except ValueError:
        return (0,)


def current_version() -> str:
    from . import __version__
    return __version__


@lru_cache(maxsize=1)
def _entries() -> tuple:
    data = json.loads((Path(__file__).with_name("news.json")).read_text(encoding="utf-8"))
    current = vtuple(current_version())
    entries = [e for e in data if vtuple(e["version"]) <= current]     # never show anything from the future
    return tuple(sorted(entries, key=lambda e: vtuple(e["version"]), reverse=True))


def _may_see(user, audience: str) -> bool:
    if audience == "admin":
        return user.is_admin
    if audience == "librarian":
        return user.is_librarian
    return True


def _seen(user) -> tuple:
    if user.news_seen:
        return vtuple(user.news_seen)
    # account from before this feature (NULL): everything before its introduction counts as seen
    return tuple(x - 1 if i == 2 else x for i, x in enumerate(vtuple(INTRODUCED)))


def _day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _text(item: dict, lang: str) -> dict:
    return item.get(lang) or item["de"]


def for_user(user, lang: str) -> list[dict]:
    """All versions with the entries relevant to this person (newest first)."""
    seen = _seen(user)
    result = []
    for entry in _entries():
        items = [dict(kind=i["kind"], **_text(i, lang)) for i in entry["items"] if _may_see(user, i["audience"])]
        if not items:
            continue
        items.sort(key=lambda i: KINDS.index(i["kind"]))
        result.append({"version": entry["version"], "date": _day(entry.get("date")), "items": items,
                       "unseen": vtuple(entry["version"]) > seen})
    return result


def unseen(user, lang: str) -> list[dict]:
    return [e for e in for_user(user, lang) if e["unseen"]]


def has_unseen(user) -> bool:
    seen = _seen(user)
    return any(vtuple(e["version"]) > seen and any(_may_see(user, i["audience"]) for i in e["items"])
               for e in _entries())


def mark_seen(user) -> bool:
    """Returns True if something changed (then it must be saved)."""
    if user.news_seen == current_version():
        return False
    user.news_seen = current_version()
    return True
