"""Version der docker-compose.yml prüfen.

Die docker-compose.yml setzt CATALYTIX_COMPOSE (feste Zahl, nicht in der .env). Sie ändert sich nur, wenn sich an der
Datei selbst etwas Nötiges ändert – Vorgaben stehen im Programm. Ist die Datei auf dem Server älter als nötig, sehen
Admins einen Hinweis (Übersicht) und das Protokoll eine Warnung; der Start wird nicht verhindert.
Das Image bringt die passende Datei mit (/app/dist/docker-compose.yml; update.sh legt sie ab).
"""
from __future__ import annotations

import os
import re

COMPOSE_VERSION = 2     # Version der docker-compose.yml in diesem Image (Zeile CATALYTIX_COMPOSE)
COMPOSE_MIN = 2         # mindestens nötige Version der Datei auf dem Server


def in_container() -> bool:
    return os.environ.get("CATALYTIX_IN_CONTAINER") == "1"


def current() -> int:
    """Version der laufenden docker-compose.yml (0: alte Datei ohne Versionsangabe)."""
    m = re.fullmatch(r"\s*(\d+)\s*", os.environ.get("CATALYTIX_COMPOSE", ""))
    return int(m.group(1)) if m else 0


def outdated() -> int | None:
    """Version der zu alten Datei – oder None, wenn alles passt (oder nicht im Container)."""
    if not in_container():
        return None
    have = current()
    return have if have < COMPOSE_MIN else None


def log_message(have: int) -> str:
    return (f"docker-compose.yml is outdated (version {have or 'none'}, needed {COMPOSE_MIN}). Run ./update.sh once "
            f"in the program folder on the server – or download docker-compose.yml from the release, replace the old "
            f"one and run: docker compose up -d. Your .env and data stay as they are.")
