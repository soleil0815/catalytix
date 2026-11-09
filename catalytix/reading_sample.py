"""Reading samples from Google Books (optional, off by default: Administration → Settings → "Leseproben").

Only publishers decide whether a book has a reading sample on Google Books. The SERVER asks Google (only the ISBN)
whether there is one that may be embedded; the browser does not talk to Google for that. The sample itself can only
be shown by Google's own viewer – copying or storing its text or pages is not allowed. So the book page shows a
button, the next page explains that the sample is loaded from Google, and only after that second click the browser
loads Google's viewer in a frame (two-click solution: Google learns the IP address only of someone who chose to).

The frame is the only place where a page may load something from another server: the Content-Security-Policy of
exactly that response allows Google Books as a frame source (frame-src); scripts stay 'self' only – Google's
viewer runs inside the frame, in Google's own origin, never in ours.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from .isbn import HEADERS, clean_isbn, is_valid_isbn, to_isbn13
from .models import get_setting

log = logging.getLogger(__name__)

SETTING = "reading_sample_google"
API = "https://www.googleapis.com/books/v1/volumes"
EMBED = "https://books.google.com/books"
# Google may forward the frame to a country site (e.g. books.google.de) or, in the EU, to its consent page; every hop
# must be allowed by frame-src
FRAME_SOURCES = ("https://books.google.com", "https://books.google.de", "https://books.google.at",
                 "https://books.google.ch", "https://www.google.com", "https://consent.google.com")
TIMEOUT = 5
TTL_FOUND = 7 * 24 * 3600     # a sample once released usually stays
TTL_NONE = 24 * 3600          # publishers add samples now and then
TTL_ERROR = 15 * 60           # Google not reachable: try again soon, but not on every page view
CACHE_MAX = 5000
VOLUME_ID = re.compile(r"[A-Za-z0-9_-]{6,24}")
EMBEDDABLE = ("PARTIAL", "ALL_PAGES")

_cache: dict[str, tuple[float, str]] = {}     # ISBN-13 → (valid until, volume id or "")
_lock = threading.Lock()


def enabled() -> bool:
    return get_setting(SETTING) in ("1", "true", "on", "yes")


def pick(data: Any) -> str:
    """The first volume of a Google Books answer that has an embeddable sample, else ""."""
    items = data.get("items") if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        access = item.get("accessInfo") or {}
        volume = str(item.get("id") or "")
        if (isinstance(access, dict) and access.get("embeddable") is True
                and access.get("viewability") in EMBEDDABLE and VOLUME_ID.fullmatch(volume)):
            return volume
    return ""


def _fetch(isbn13: str) -> str:
    params: dict[str, str | int] = {"q": f"isbn:{isbn13}", "maxResults": 5,
                                    "fields": "items(id,accessInfo(embeddable,viewability))"}
    r = requests.get(API, params=params,
                     timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    return pick(r.json())


def lookup(isbn: str, now: float | None = None, fetch: Callable[[str], str] | None = None) -> str | None:
    """Google Books volume id with an embeddable reading sample for this ISBN, or None. Answers are kept for a
    while (also "none" and errors), so a busy catalog does not ask Google on every page view."""
    if not isbn or not is_valid_isbn(clean_isbn(isbn)):
        return None
    key = to_isbn13(clean_isbn(isbn))
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1] or None
    try:
        volume = (fetch or _fetch)(key)
        ttl = TTL_FOUND if volume else TTL_NONE
    except (requests.RequestException, ValueError) as exc:
        log.info("Reading sample: Google Books not reachable for %s (%s)", key, type(exc).__name__)
        volume, ttl = "", TTL_ERROR
    if volume and not VOLUME_ID.fullmatch(volume):
        volume = ""
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = (now + ttl, volume)
    return volume or None


def embed_url(volume: str) -> str:
    if not VOLUME_ID.fullmatch(volume):
        raise ValueError("invalid volume id")
    return f"{EMBED}?id={volume}&printsec=frontcover&output=embed"


def csp(base: str) -> str:
    """The normal policy plus Google Books as the only allowed frame source – only for the page with the viewer."""
    return f"{base}; frame-src {' '.join(FRAME_SOURCES)}"
