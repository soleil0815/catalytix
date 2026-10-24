# Base image pinned exactly: Python 3.13.16 on Debian 13 (trixie), frozen to exactly
# this state by its digest (same on amd64 and arm64).
# For a new base image, change tag and digest together and run the tests.
ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="Catalytix" \
      org.opencontainers.image.version="2.17.0" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/soleil0815/catalytix" \
      org.opencontainers.image.description="Library management for training institutes (German/English)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CATALYTIX_DATA_DIR=/data \
    HOME=/tmp \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY requirements.txt .
# Pin pip itself, then install exactly the lock file – without pulling in sub-dependencies
# (--no-deps) – and finally check that everything fits together.
RUN pip install pip==26.2.2 \
 && pip install --no-deps --only-binary=:all: -r requirements.txt \
 && pip check
# Time zone: TZ (e.g. Europe/Berlin) needs /usr/share/zoneinfo (Debian package tzdata, included
# in the python-slim image). If it is missing, the build says so here and Catalytix at start-up.
RUN test -e /usr/share/zoneinfo/Europe/Berlin \
 || echo "WARNING: /usr/share/zoneinfo is missing from the base image – TZ has no effect, times in UTC."

COPY catalytix ./catalytix
COPY wsgi.py LICENSE ./
COPY docs ./docs
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

# The container starts as root, fixes the permissions of /data and then
# switches to PUID:PGID in the entrypoint (no root while running).
VOLUME ["/data"]
EXPOSE 9300

# Installation files for "update.sh --pull": copied from the image into the program folder, so that
# docker-compose.yml and update.sh always match the image version
COPY docker-compose.yml .env.example update.sh README.md README.en.md CHANGELOG.md LICENSE /app/dist/

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9300/login')" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:9300", "--workers", "2", "--threads", "4", "--access-logfile", "-", "wsgi:app"]
