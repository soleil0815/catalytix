# Reproducible build – every input is locked (see tools/lock.py, which fixes the versions anew):
# - Base image: exact tag AND digest. The tag names the foundation: Debian 13 "trixie", Python 3.13.
# - Debian packages: exact archive state DEBIAN_SNAPSHOT on snapshot.debian.org – all packages, including those
#   of the base image, are brought to that state.
# - Python packages: requirements.txt with exact versions and SHA-256 hashes (also indirect dependencies);
#   pip itself from docker/pip.lock. pip is removed from the finished image.
# The weekly security update on GitHub sets newer values here – always INSIDE the foundation. Raising the
# foundation (Debian 14, Python 3.14, new major versions) is a deliberate change.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
ARG DEBIAN_SNAPSHOT=20261108T000000Z

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
    CATALYTIX_IN_CONTAINER=1 \
    TZ=Europe/Berlin \
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
# Remove what the running program never uses (package manager, Perl, login/user tools, PAM …) – fewer findings that
# do not concern the program, nothing to install software with. dpkg stays, so scanners still see everything.
COPY docker/slim.sh /tmp/slim.sh
RUN sh /tmp/slim.sh && rm -f /tmp/slim.sh

COPY catalytix ./catalytix
COPY wsgi.py LICENSE ./
COPY docs ./docs
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

# The container starts as root, fixes the permissions of /data and then
# switches to PUID:PGID in the entrypoint (no root while running).
VOLUME ["/data"]
EXPOSE 9300

# Files for the host, matching this image: update.sh places docker-compose.yml when the image brings a newer one
# (the old one is kept as docker-compose.yml.alt), and keeps update.sh and .env.example current.
# (Installations from before 3.0.0 get them with their old "update.sh --pull" once.)
COPY docker-compose.yml .env.example update.sh /app/dist/

# What is inside (compared by the weekly security update; listed in the change log when something changed)
# set -eu and no pipe: a failing dpkg-query or python must stop the build instead of leaving a shortened list
RUN set -eu; \
    dpkg-query -W -f='deb ${Package} ${Version}\n' > /tmp/deb.txt; \
    pyver="$(python -c 'import platform; print(platform.python_version())')"; \
    { echo "base ${PYTHON_IMAGE}"; \
      echo "python $pyver"; \
      sort /tmp/deb.txt; \
      python -c "import importlib.metadata as m; print('\n'.join(sorted('pip %s %s' % (d.metadata['Name'].lower(), d.version) for d in m.distributions())))"; \
    } > /app/image-packages.txt; \
    rm -f /tmp/deb.txt; \
    grep -q '^deb ' /app/image-packages.txt; \
    grep -q '^pip flask ' /app/image-packages.txt; \
    if command -v pip >/dev/null || python -c "import pip" 2>/dev/null; then echo "pip must not be in the image" >&2; exit 1; fi

# Version label late: a new version number doesn't invalidate the cached dependency layers above
LABEL org.opencontainers.image.version="3.1.6" \
      catalytix.compose="2"
# Exact tag (e.g. 3.0.3-s1) and source commit, set by the GitHub workflows (empty in local builds): update.sh goes
# back to exactly this tag, the release is created for exactly this commit
ARG CATALYTIX_TAG=""
ARG CATALYTIX_REVISION=""
LABEL catalytix.tag="${CATALYTIX_TAG}" \
      org.opencontainers.image.revision="${CATALYTIX_REVISION}"
# start-period: the entrypoint may first adjust owners/permissions in a large ./data
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9300/login')" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:9300", "--workers", "2", "--threads", "4", "--access-logfile", "-", "wsgi:app"]
