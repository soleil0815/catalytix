"""Check the version of docker-compose.yml.

docker-compose.yml sets CATALYTIX_COMPOSE (a fixed number, not in .env). It only changes when something necessary
changes in the file itself – defaults live in the program. If the file on the server is older than required, admins
see a notice (overview page) and the log shows a warning; startup is not prevented.
The image ships the matching file (/app/dist/docker-compose.yml; update.sh puts it in place).
"""
from __future__ import annotations

import os
import re

COMPOSE_VERSION = 2     # version of docker-compose.yml in this image (line CATALYTIX_COMPOSE)
COMPOSE_MIN = 2         # minimum required version of the file on the server


def in_container() -> bool:
    return os.environ.get("CATALYTIX_IN_CONTAINER") == "1"


def current() -> int:
    """Version of the running docker-compose.yml (0: old file without a version)."""
    m = re.fullmatch(r"\s*(\d+)\s*", os.environ.get("CATALYTIX_COMPOSE", ""))
    return int(m.group(1)) if m else 0


def outdated() -> int | None:
    """Version of the outdated file – or None if everything is fine (or not running in a container)."""
    if not in_container():
        return None
    have = current()
    return have if have < COMPOSE_MIN else None


def log_message(have: int) -> str:
    return (f"docker-compose.yml is outdated (version {have or 'none'}, needed {COMPOSE_MIN}). Run ./update.sh once "
            f"in the program folder on the server – or download docker-compose.yml from the release, replace the old "
            f"one and run: docker compose up -d. Your .env and data stay as they are.")
