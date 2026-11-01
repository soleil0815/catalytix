#!/usr/bin/env python3
"""How long does the foundation still get security updates? (weekly, .github/workflows/foundation.yml)

Reads the foundation from the Dockerfile (Debian release from the base image tag, Python series from its version) and
asks endoflife.date when security support ends:
  - more than 12 months left: fine
  - 12 months or less:        warning on the run (time to plan raising the foundation)
  - 6 months or less:         the run fails (red) – raise the foundation now
Nobody raises the foundation silently: this only reports.
If endoflife.date cannot be reached, the run shows a warning and stays green (it asks again next week).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock  # noqa: E402
from trivy_scan import annotate, summary  # noqa: E402

DEBIAN_RELEASES = {"bookworm": "12", "trixie": "13", "forky": "14", "duke": "15"}
WARN_DAYS, FAIL_DAYS = 365, 182


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "catalytix-foundation"})
    with urllib.request.urlopen(req, timeout=30) as res:   # noqa: S310 – fixed https host
        return json.loads(res.read().decode())


Fetch = Callable[[str], Any]


def end_of_security_support(product: str, cycle: str, fetch: Fetch = fetch_json) -> dt.date:
    """Date from endoflife.date (API v1, falling back to the older API)."""
    try:
        data = fetch(f"https://endoflife.date/api/v1/products/{product}/releases/{cycle}")
        value = (data.get("result") or {}).get("eolFrom")
    except Exception:  # noqa: BLE001 – try the older API
        value = None
    if not value:
        data = fetch(f"https://endoflife.date/api/{product}/{cycle}.json")
        value = data.get("eol")
    if value is True:
        return dt.date.min
    if not value or value is False:
        raise ValueError(f"endoflife.date has no end date for {product} {cycle}")
    return dt.date.fromisoformat(str(value)[:10])


def foundation() -> list[tuple[str, str, str]]:
    d = lock.read_dockerfile()
    series = ".".join(d["python"].split(".")[:2])
    debian = DEBIAN_RELEASES.get(d["debian"])
    if not debian:
        raise SystemExit(f"Unknown Debian release {d['debian']!r} – add it to DEBIAN_RELEASES")
    return [("Debian", "debian", debian), ("Python", "python", series)]


def evaluate(items: list[tuple[str, str, dt.date]], today: dt.date) -> tuple[int, list[str]]:
    """(exit code, lines) – exit 1 if something has 6 months or less left."""
    code, lines = 0, []
    for label, cycle, end in items:
        days = (end - today).days
        text = f"{label} {cycle}: security updates until {end:%Y-%m-%d} ({days} days left)"
        if days <= FAIL_DAYS:
            annotate("error", "Foundation: raise it now", text + " – plan and test Debian/Python together.")
            code = 1
        elif days <= WARN_DAYS:
            annotate("warning", "Foundation: less than 12 months left", text + " – plan raising the foundation.")
        lines.append(text)
    return code, lines


def main(today: dt.date | None = None, fetch: Fetch = fetch_json) -> int:
    today = today or dt.date.today()
    items = []
    for label, product, cycle in foundation():
        try:
            items.append((label, cycle, end_of_security_support(product, cycle, fetch)))
        except Exception as exc:  # noqa: BLE001
            annotate("warning", "Foundation check not possible", f"{label} {cycle}: {exc} – asking again next week.")
    code, lines = evaluate(items, today)
    summary("### Foundation\n" + "\n".join(f"- {l}" for l in lines))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
