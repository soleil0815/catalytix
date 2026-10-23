"""Einrichtungscode für die Ersteinrichtung.

Solange es kein Konto gibt, darf nur das Administratorkonto anlegen, wer den Code kennt. Er steht im
Protokoll des Containers und wird von update.sh nach der Erstinstallation angezeigt – wer nur den Port
erreicht, kann die Einrichtung so nicht an sich reißen."""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)
FILE = "setup_code"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # ohne 0/O, 1/I – gut abzutippen


def _path() -> Path:
    return Path(current_app.instance_path) / FILE


def normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def ensure() -> str:
    """Code anlegen (falls nötig) und zurückgeben. Atomar, damit mehrere Worker denselben Code nutzen."""
    p = _path()
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return current()
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    with os.fdopen(fd, "w") as fh:
        fh.write(code + "\n")
    return code


def current() -> str:
    try:
        return _path().read_text().strip()
    except OSError:
        return ""


def check(given: str) -> bool:
    code = current()
    return bool(code) and hmac.compare_digest(normalize(given).encode(), normalize(code).encode())


def remove() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def announce() -> None:
    code = current()
    if code:
        log.warning("First-time setup pending – setup code: %s  (asked for on the setup page)", code)
