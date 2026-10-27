# Reproducible build – every input is locked (see tools/lock.py, which fixes the versions anew):
# - Base image: exact tag AND digest. The tag names the foundation: Debian 13 "trixie", Python 3.13.
# - Debian packages: exact archive state DEBIAN_SNAPSHOT on snapshot.debian.org – all packages, including those
#   of the base image, are brought to that state.
# - Python packages: requirements.txt with exact versions and SHA-256 hashes (also indirect dependencies);
#   pip itself from docker/pip.lock. pip is removed from the finished image.
# The weekly security update on GitHub sets newer values here – always INSIDE the foundation. Raising the
# foundation (Debian 14, Python 3.14, new major versions) is a deliberate change.
ARG PYTHON_IMAGE=python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
ARG DEBIAN_SNAPSHOT=20261024T000000Z

LABEL org.opencontainers.image.title="Catalytix" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/soleil0815/catalytix" \
      org.opencontainers.image.description="Library management for training institutes (German/English)" \
      org.opencontainers.image.base.name="${PYTHON_IMAGE}" \
      catalytix.debian-snapshot="${DEBIAN_SNAPSHOT}"

# Debian packages: exactly the archive state DEBIAN_SNAPSHOT (signed by Debian as usual). Snapshot Release files
# are old by nature, hence Check-Valid-Until=false. The codename check stops the build if the base image is not the
# expected Debian release.
RUN set -eu; \
    . /etc/os-release; \
    [ "$VERSION_CODENAME" = trixie ] || { echo "Expected Debian trixie, found $VERSION_CODENAME"; exit 1; }; \
    key=""; for k in /usr/share/keyrings/debian-archive-keyring.pgp /usr/share/keyrings/debian-archive-keyring.gpg; do \
      [ -f "$k" ] && { key="$k"; break; }; done; \
    [ -n "$key" ] || { echo "Debian archive keyring not found"; exit 1; }; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      "Types: deb" "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
      "Suites: trixie trixie-updates" "Components: main" "Signed-By: $key" "" \
      "Types: deb" "URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" \
      "Suites: trixie-security" "Components: main" "Signed-By: $key" > /etc/apt/sources.list.d/snapshot.sources; \
    apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 update; \
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 -y --no-install-recommends \
      -o Dpkg::Options::=--force-confold full-upgrade; \
    if apt-get -s full-upgrade | grep -q '^Inst '; then echo "Packages left out of the upgrade"; exit 1; fi; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CATALYTIX_DATA_DIR=/data \
    HOME=/tmp \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY docker/pip.lock requirements.txt ./
# Exactly the locked versions, every file checked against its SHA-256 (--require-hashes), nothing pulled in on top
# (--no-deps), only pre-built packages. Then: do they fit together? Finally remove pip – it is not needed at runtime.
RUN pip install --no-cache-dir --require-hashes --no-deps -r pip.lock \
 && pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r requirements.txt \
 && pip check \
 && python -m pip uninstall -y pip \
 && python -c "import ensurepip, os, shutil; shutil.rmtree(os.path.dirname(ensurepip.__file__))" \
 && rm -f pip.lock requirements.txt
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

# What is inside (compared by the weekly security update; listed in the change log when something changed)
RUN { echo "base ${PYTHON_IMAGE}"; \
      echo "python $(python -c 'import platform; print(platform.python_version())')"; \
      dpkg-query -W -f='deb ${Package} ${Version}\n' | sort; \
      python -c "import importlib.metadata as m; print('\n'.join(sorted('pip %s %s' % (d.metadata['Name'].lower(), d.version) for d in m.distributions())))"; \
    } > /app/image-packages.txt \
 && ! command -v pip >/dev/null \
 && ! python -c "import pip" 2>/dev/null

# Version label late: a new version number doesn't invalidate the cached dependency layers above
LABEL org.opencontainers.image.version="2.20.0"
# start-period: the entrypoint may first adjust owners/permissions in a large ./data
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9300/login')" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:9300", "--workers", "2", "--threads", "4", "--access-logfile", "-", "wsgi:app"]
