"""Automatic sign-out after a period without activity.

Settings (Administration → Settings → "Automatische Abmeldung"):
- session_hours            limit for everyone, in hours (unset: CATALYTIX_SESSION_HOURS, otherwise 12)
- session_admin_minutes    own, shorter limit for admins (default 60 minutes; empty = same as everyone)
- session_external_minutes optional, shorter limit for sign-ins from outside the internal network (empty = off)
- session_warning          show "you will be signed out in 2 minutes – keep working?" before the limit
- session_browser_close    sign out when the browser is closed: session cookie without expiry date and no
                           "stay signed in" (remember cookie)

"Activity" is any request of the signed-in user except the status query of the warning dialog. The server decides
(timestamp in the signed session cookie) – an open, forgotten tab does not keep the session alive by itself.
Where a shorter limit applies (admins, sign-ins from outside), "stay signed in" is not offered, otherwise the
remember cookie would bypass the limit after a browser restart.
"""
from __future__ import annotations

import os
import time
from typing import Any

from flask import session

from .models import get_setting

DEFAULT_HOURS = 12
MAX_HOURS = 168             # one week
MIN_MINUTES = 5
MAX_MINUTES = 720           # 12 hours
WARN_SECONDS = 120          # the warning appears 2 minutes before the limit
SEEN_KEY = "seen_at"


def env_hours() -> int:
    """Start value from CATALYTIX_SESSION_HOURS (1–168), otherwise 12."""
    raw = (os.environ.get("CATALYTIX_SESSION_HOURS") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= MAX_HOURS:
        return int(raw)
    return DEFAULT_HOURS


def general_hours() -> int:
    raw = get_setting("session_hours", "")
    if raw.isdigit() and 1 <= int(raw) <= MAX_HOURS:
        return int(raw)
    return env_hours()


def _minutes(key: str) -> int | None:
    raw = get_setting(key)
    if raw.isdigit() and MIN_MINUTES <= int(raw) <= MAX_MINUTES:
        return int(raw)
    return None


def admin_minutes() -> int | None:
    return _minutes("session_admin_minutes")


def external_minutes() -> int | None:
    return _minutes("session_external_minutes")


def warning_enabled() -> bool:
    return get_setting("session_warning") in ("1", "true", "on", "yes")


def browser_close() -> bool:
    return get_setting("session_browser_close") in ("1", "true", "on", "yes")


def _is_admin(user: Any) -> bool:
    return bool(getattr(user, "is_admin", False))


def limit_seconds(user: Any, internal: bool) -> int:
    """Time without activity after which this user is signed out (the shortest limit that applies)."""
    limit = general_hours() * 3600
    admin = admin_minutes()
    if _is_admin(user) and admin is not None:
        limit = min(limit, admin * 60)
    external = external_minutes()
    if not internal and external is not None:
        limit = min(limit, external * 60)
    return limit


def remember_allowed(user: Any, internal: bool) -> bool:
    """May this sign-in use "stay signed in"? Not where a shorter limit applies – the remember cookie would
    otherwise restore the session after a browser restart and bypass the limit."""
    if browser_close():
        return False
    return limit_seconds(user, internal) >= general_hours() * 3600


def touch(now: float | None = None) -> None:
    """Record activity now (called on every request of a signed-in user and right after signing in)."""
    session[SEEN_KEY] = int(now if now is not None else time.time())


def seconds_left(user: Any, internal: bool, now: float | None = None) -> int | None:
    """Seconds until sign-out, or None when no activity is recorded in this session yet."""
    seen = session.get(SEEN_KEY)
    if not isinstance(seen, int):
        return None
    now = now if now is not None else time.time()
    return int(limit_seconds(user, internal) - (now - seen))


def describe_limit(seconds: int) -> tuple[int, str]:
    """(number, unit) for messages: whole hours when possible, otherwise minutes."""
    if seconds % 3600 == 0:
        return seconds // 3600, "h"
    return max(1, seconds // 60), "min"
