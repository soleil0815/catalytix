"""Is there a newer Catalytix? (3.5.0)

Once a day the server asks GitHub for the latest published release (public repository: no account, no token; only
the request itself leaves the server, no library data). Administrators see a notice on the overview – but only once
the new version has been out for a day: installations that update automatically (Watchtower, Dockhand) are on the
new version by then and never see it. Catalytix never updates or restarts itself; it only says so.

Switch: Settings → Wartung ("update_check", on by default). Without internet access there is simply no notice; the
maintenance tab shows the last result.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from .models import db, get_bool, get_setting, set_setting

log = logging.getLogger(__name__)

REPO = "soleil0815/catalytix"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TIMEOUT = 10
GRACE = timedelta(hours=24)          # automatic updaters are on the new version by then
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "Catalytix (Bibliotheksverwaltung)"}
_VERSION_RX = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")

# Settings (all PLAIN in the diagnostic package): switch, result of the last check, day claim of the scheduler
KEY_ON = "update_check"
KEY_RESULT = "update_check_result"     # JSON: checked, ok, latest, published, url, error
KEY_DAY = "update_check_last_run"


def parse(version: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RX.match((version or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _current() -> str:
    from . import __version__
    return __version__


def fetch() -> dict:
    """Ask GitHub for the latest release. Raises on any problem (network, answer not understood)."""
    r = requests.get(API_URL, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise CheckError("ratelimit")
    if r.status_code != 200:
        raise CheckError("http", str(r.status_code))
    data = r.json()
    tag = str(data.get("tag_name") or "")[:40]
    if not parse(tag) or data.get("draft") or data.get("prerelease"):
        raise CheckError("answer")
    url = str(data.get("html_url") or "")
    return {"latest": tag.lstrip("v"), "published": str(data.get("published_at") or "")[:40],
            "url": url if url.startswith(f"https://github.com/{REPO}/") else RELEASES_URL}


def result() -> dict:
    try:
        data = json.loads(get_setting(KEY_RESULT) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def check_now(now: datetime | None = None) -> dict:
    """Check and remember the result (also used by "Jetzt prüfen")."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        res = {"checked": stamp, "ok": True, **fetch()}
        newer = newer_than_running(res["latest"])
        log.info("Update check: latest release %s%s", res["latest"], " (newer than this one)" if newer else "")
    except Exception as exc:  # noqa: BLE001 – no internet, proxy, GitHub down: just no notice
        code, detail = _error_code(exc)
        old = result()
        # keep what we knew – a failed check must not hide a known newer version
        res = {**{k: old[k] for k in ("latest", "published", "url") if k in old},
               "checked": stamp, "ok": False, "error": code, "detail": detail}
        log.warning("Update check failed: %s %s", code, detail)
    set_setting(KEY_RESULT, json.dumps(res))
    db.session.commit()
    return res


class CheckError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _error_code(exc: Exception) -> tuple[str, str]:
    """Short code for the UI (translated there) + harmless detail – never the exception text (could carry a proxy
    address with credentials)."""
    if isinstance(exc, CheckError):
        return exc.code, exc.detail[:20]
    if isinstance(exc, requests.Timeout):
        return "timeout", ""
    if isinstance(exc, requests.ConnectionError):
        return "connection", ""
    return "other", type(exc).__name__[:40]


def run_daily(now: datetime | None = None) -> dict | None:
    """Called by the scheduler every few minutes: checks once a day."""
    from .mailer import _claim_today
    now = now or datetime.now()
    if not get_bool(KEY_ON):
        return None
    if not _claim_today(now.date().isoformat(), key=KEY_DAY):
        return None
    return check_now(now)


def newer_than_running(latest: str | None) -> bool:
    new, have = parse(latest), parse(_current())
    return bool(new and have and new > have)


def _published(res: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(res.get("published", "")).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def notice(user, now: datetime | None = None) -> dict | None:
    """The overview notice for an admin: {"latest", "current", "url"} or None."""
    if user is None or not getattr(user, "is_admin", False) or not get_bool(KEY_ON):
        return None
    res = result()
    latest = res.get("latest")
    if not newer_than_running(latest):
        return None
    pub = _published(res)
    if pub is None or (now or datetime.now(timezone.utc)) - pub < GRACE:
        return None
    if user.update_hidden and parse(user.update_hidden) and parse(user.update_hidden) >= parse(latest):
        return None
    return {"latest": latest, "current": _current(), "url": res.get("url") or RELEASES_URL}


def status() -> dict:
    """For Settings → Wartung."""
    res = result()
    return {"on": get_bool(KEY_ON), "current": _current(), "checked": res.get("checked", ""),
            "ok": res.get("ok"), "error": res.get("error", ""), "detail": res.get("detail", ""), "latest": res.get("latest", ""),
            "newer": newer_than_running(res.get("latest")), "url": res.get("url") or RELEASES_URL}
