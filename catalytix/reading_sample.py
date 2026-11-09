"""Reading samples from Google Books (optional, off by default: Administration → Settings → "Leseproben").

Only publishers decide whether a book has a reading sample on Google Books, and it can only be shown by Google's own
viewer – copying or storing its text or pages is not allowed. So the book page shows a button, the next page explains
that the sample is loaded from Google, and only after that second click the browser loads Google's viewer in a frame
(two-click solution: Google learns the IP address only of someone who chose to).

Whether there is a sample:
- with an own Google API key (Settings → "Leseproben", stored encrypted) the SERVER asks the Books API (only the
  ISBN; the browser does not talk to Google for that) – the button appears only for books Google knows. Its
  "embeddable"/"viewability" fields are NOT used: they say "NO_PAGES" for books whose preview the viewer shows
  (seen in 3.1.7), so only the viewer itself can tell;
- without a key Google answers no queries (daily limit 0 for anonymous requests, HTTP 429) – then the button appears
  for every book with an ISBN and Google's viewer, opened by ISBN, says itself when there is no preview. The same
  happens with a key while Google cannot be reached or the daily limit is used up.

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

from .isbn import GoogleBooksError, clean_isbn, google_api_key, google_get, is_valid_isbn, to_isbn13
from .models import get_setting

log = logging.getLogger(__name__)

SETTING = "reading_sample_google"
EMBED = "https://books.google.com/books"
# Google may forward the frame to a country site (e.g. books.google.de) or, in the EU, to its consent page; every hop
# must be allowed by frame-src
FRAME_SOURCES = ("https://books.google.com", "https://books.google.de", "https://books.google.at",
                 "https://books.google.ch", "https://www.google.com", "https://consent.google.com")
TTL_FOUND = 7 * 24 * 3600     # a sample once released usually stays
TTL_NONE = 24 * 3600          # publishers add samples now and then
TTL_ERROR = 15 * 60           # Google not reachable or limit used up: try again soon, not on every page view
CACHE_MAX = 5000
VOLUME_ID = re.compile(r"[A-Za-z0-9_-]{6,24}")
UNKNOWN = "?"                 # not checked (no key, Google not reachable, daily limit): the viewer decides

_cache: dict[str, tuple[float, str]] = {}     # ISBN-13 → (valid until, volume id / "" none / UNKNOWN)
_lock = threading.Lock()
_quota_at = 0.0               # last time Google answered "daily limit reached" (for the settings page)


def clear_cache() -> None:
    global _quota_at
    with _lock:
        _cache.clear()
    _quota_at = 0.0


def enabled() -> bool:
    return get_setting(SETTING) in ("1", "true", "on", "yes")


def quota_problem(now: float | None = None) -> bool:
    """Did Google refuse a query for the daily limit within the last day (shown on the settings page)?"""
    return _quota_at > 0 and (time.time() if now is None else now) - _quota_at < 24 * 3600


def pick(data: Any) -> str:
    """The first volume of a Google Books answer ("Google knows this book"), else "". accessInfo is not trusted:
    it reports "no pages" for books whose preview the viewer shows."""
    items = data.get("items") if isinstance(data, dict) else None
    for item in items or []:
        volume = str(item.get("id") or "") if isinstance(item, dict) else ""
        if VOLUME_ID.fullmatch(volume):
            return volume
    return ""


def _fetch(isbn13: str) -> str:
    return pick(google_get({"q": f"isbn:{isbn13}", "maxResults": 5,
                            "fields": "items(id)"}))


def _isbn13(isbn: str) -> str | None:
    raw = clean_isbn(isbn or "")
    return to_isbn13(raw) if raw and is_valid_isbn(raw) else None


def check(isbn: str, now: float | None = None, fetch: Callable[[str], str] | None = None) -> str | None:
    """Volume id (Google knows the book), "" (Google does not), UNKNOWN (not checked) – or None without a valid ISBN. Answers are kept for a while (also "none" and errors), so a busy catalog does not ask on every page view."""
    global _quota_at
    key = _isbn13(isbn)
    if key is None:
        return None
    if fetch is None and not google_api_key():
        return UNKNOWN            # anonymous queries: Google allows none (HTTP 429) – do not even try
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        volume = (fetch or _fetch)(key)
        if volume and not VOLUME_ID.fullmatch(volume):
            volume = ""
        ttl = TTL_FOUND if volume else TTL_NONE
    except GoogleBooksError as exc:
        if exc.quota:
            _quota_at = now
            log.warning("Reading sample: Google Books refused the query – daily limit reached (HTTP 429). Check "
                        "the Google API key (Settings → reading samples) and its quota.")
        else:
            log.info("Reading sample: Google Books not reachable for %s (%s)", key, exc)
        volume, ttl = UNKNOWN, TTL_ERROR
    except ValueError:            # not JSON
        log.info("Reading sample: unreadable answer from Google Books for %s", key)
        volume, ttl = UNKNOWN, TTL_ERROR
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = (now + ttl, volume)
    return volume


def available(isbn: str) -> bool:
    """Show the button? Not when Google does not know the book (only possible with a key)."""
    found = check(isbn)
    return found is not None and found != ""


def target(isbn: str) -> str | None:
    """Address of Google's viewer for this book (opened by ISBN – it shows the preview where the publisher allows
    it), or None (no ISBN, or Google does not know the book)."""
    if not check(isbn):
        return None
    key = _isbn13(isbn)
    return f"{EMBED}?vid=ISBN{key}&printsec=frontcover&output=embed" if key else None


def csp(base: str) -> str:
    """The normal policy plus Google Books as the only allowed frame source – only for the page with the viewer."""
    return f"{base}; frame-src {' '.join(FRAME_SOURCES)}"
