#!/usr/bin/env bash
# ------------------------------------------------------------------
# Catalytix – rebuild and update the container
#
#   ./update.sh catalytix-X.Y.Z.zip install a new package, build the image locally, start
#   ./update.sh --pull              get the newest image built by GitHub (ghcr.io), start it and
#                                   follow "latest" from now on
#   ./update.sh --pull=X.Y.Z        get exactly this version from GitHub and stay on it
#   ./update.sh                     rebuild and restart the current version (with --pull mode:
#                                   pull latest – or the pinned version – again, nothing is built)
#
# Options:
#   --no-backup   don't take a backup first
#   --no-cache    build the image without the Docker cache
#   --prune       thorough cleanup: keep only the current version (image, package)
#   --no-cleanup  don't clean up after the update
#   -y, --yes     no questions
#
# --pull remembers the GitHub image in .env (CATALYTIX_IMAGE, CATALYTIX_TAG=latest), so a container
# manager that updates images automatically can pull new versions too; installing a
# zip package switches back to building locally. The installation files (docker-compose.yml,
# update.sh, .env.example, READMEs) come from the image, so they always match the version.
#   -h, --help    this help
#
# After a successful update the script cleans up: older Catalytix images and
# zip packages (the current and the previous one are kept for the way back),
# work folders of aborted updates (older than 7 days) and Python caches.
#
# Never touched: ./data (database, covers, keys, backups),
# .env and docker-compose.override.yml.
# ------------------------------------------------------------------

# Also works as "sh update.sh": restart with Bash automatically.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "This script needs Bash. Please run it as:  bash update.sh" >&2
    exit 1
fi

set -euo pipefail

# The whole script lives in main(): Bash reads it completely before running it –
# important because a new package replaces this very file.
main() {
SERVICE=catalytix
HEALTH_TIMEOUT=300   # seconds to wait for "healthy" (the image allows 120 s start-up, e.g. for a big ./data)

# ---------------------------------------------------------------- Output
if [ -t 1 ]; then
    B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
else
    B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ---------------------------------------------------------------- Arguments
BACKUP=1; NOCACHE=""; PRUNE=0; CLEANUP=1; YES=0; ZIP=""; PULL=0; PULL_TAG=""
DEFAULT_IMAGE="ghcr.io/soleil0815/catalytix"
for arg in "$@"; do
    case "$arg" in
        --no-backup) BACKUP=0 ;;
        --no-cache)  NOCACHE="--no-cache" ;;
        --prune)     PRUNE=1 ;;
        --no-cleanup) CLEANUP=0 ;;
        --pull)      PULL=1; PULL_EXPLICIT=1 ;;
        --pull=*)    PULL=1; PULL_TAG="${arg#--pull=}"; PULL_EXPLICIT=1 ;;
        -y|--yes)    YES=1 ;;
        -h|--help)   usage ;;
        -*)          die "Unknown option: $arg  (help: ./update.sh --help)" ;;
        *)           [ -z "$ZIP" ] || die "Please give only one package."; ZIP="$arg" ;;
    esac
done

[ "$PULL" = 0 ] || [ -z "$ZIP" ] || die "Either a zip package or --pull, not both."
[ -z "$PULL_TAG" ] || [[ "$PULL_TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$|^latest$ ]] \
    || die "--pull=… expects a version like 2.19.0 (or latest)."

# Resolve the package path BEFORE changing into the program folder (also relative paths like ../x.zip)
if [ -n "$ZIP" ]; then
    [ -f "$ZIP" ] || die "File not found: $ZIP"
    ZIP="$(readlink -f -- "$ZIP")"
fi
INSTALL_DIR="$(dirname "$(readlink -f -- "$0")")"
cd "$INSTALL_DIR" || die "Program folder not accessible: $INSTALL_DIR"

confirm() {
    [ "$YES" = 1 ] && return 0
    # Without a terminal (cron, ssh without -t) and without -y: abort rather than assume "yes"
    [ -t 0 ] || { echo "$1 – no input possible, aborted (use -y to run without questions)." >&2; return 1; }
    read -r -p "$1 [Y/n] " a
    case "${a:-y}" in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- Requirements
command -v docker >/dev/null 2>&1 || die "Docker is not installed."
# "docker compose" (plugin, any version) or the older standalone "docker-compose" (e.g. Synology) from 1.27 on –
# older 1.x versions cannot read a docker-compose.yml without a "version:" line.
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    die "Docker Compose is missing (neither \"docker compose\" nor \"docker-compose\" found)."
fi
# Reuse the project name of the running container – otherwise e.g. a symlinked folder
# (/opt/catalytix → /srv/…) would give a different Compose project and "up" would fail on the container name.
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' catalytix 2>/dev/null || true)"
[ -n "$PROJECT" ] && DC+=(-p "$PROJECT")
docker info >/dev/null 2>&1 || die "No access to Docker. Run as root or as a member of the \"docker\" group."

# ---------------------------------------------------------------- .env keys for the image (--pull mode)
env_get() {   # last value of KEY in .env (without quotes), empty if not set
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env 2>/dev/null | tail -n1 | tr -d '"'"'"' \r' || true
}
env_set() {   # set KEY=VALUE in .env: replace an existing (or commented-out) line, otherwise append
    local key="$1" val="$2" tmp
    [[ "$val" =~ ^[A-Za-z0-9._/:-]+$ ]] || die "Invalid value for $key: $val"
    tmp="$(mktemp "$INSTALL_DIR/.env.XXXXXX")" || die "Cannot write .env."
    if grep -qE "^#?$key=" .env 2>/dev/null; then
        awk -v k="$key" -v v="$val" 'BEGIN{done=0} { if (!done && ($0 ~ "^"k"=" || $0 ~ "^#"k"=")) { print k"="v; done=1 } else if ($0 ~ "^"k"=") { print "#"$0 } else print }' .env > "$tmp"
    else
        { cat .env 2>/dev/null
          grep -q '^# Set by update.sh --pull' .env 2>/dev/null || printf '\n# Set by update.sh --pull\n'
          printf '%s=%s\n' "$key" "$val"; } > "$tmp"
    fi
    cat "$tmp" > .env || { rm -f -- "$tmp"; die "Cannot write .env."; }     # keep owner and permissions of .env
    rm -f -- "$tmp"
}
env_unset() {   # comment out KEY in .env
    [ -f .env ] || return 0
    grep -qE "^$1=" .env || return 0
    local tmp; tmp="$(mktemp "$INSTALL_DIR/.env.XXXXXX")" || die "Cannot write .env."
    sed "s/^$1=/#$1=/" .env > "$tmp" && cat "$tmp" > .env || { rm -f -- "$tmp"; die "Cannot write .env."; }
    rm -f -- "$tmp"
}
image_version() {   # version label of a local image
    docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$1" 2>/dev/null || true
}
running_version() {   # version of the image the existing container was created from (empty if unknown)
    local img v
    img="$(docker inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null || true)"
    [ -n "$img" ] || return 0
    v="$(image_version "$img")"
    if [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo "$v"; fi
}

version_of() {   # read the program version from a Catalytix folder (empty if missing)
    local f="$1/catalytix/__init__.py"
    [ -f "$f" ] || return 0
    sed -n 's/^__version__ = "\(.*\)"/\1/p' "$f" 2>/dev/null | head -n1 || true
}
# Installed version: preferably that of the existing container – files in the folder can be stale (e.g. program
# files of an earlier zip install next to a --pull install) and a pulled "latest" tag says nothing yet
OLD_VERSION="$(running_version)"
[ -n "$OLD_VERSION" ] || OLD_VERSION="$(version_of .)"
# Mode: images from GitHub (--pull, remembered in .env) or built locally (zip package)
REG_IMAGE="$(env_get CATALYTIX_IMAGE)"
[ "$REG_IMAGE" = catalytix ] && REG_IMAGE=""
if [ -z "$ZIP" ] && [ "$PULL" = 0 ] && [ -n "$REG_IMAGE" ]; then
    PULL=1; PULL_TAG="$(env_get CATALYTIX_TAG)"    # --pull mode stays: latest (or the pinned version), nothing is built
fi
if [ "$PULL" = 1 ]; then
    IMAGE="${REG_IMAGE:-$DEFAULT_IMAGE}"
    PULL_TAG="${PULL_TAG:-latest}"
    CUR_TAG="$(env_get CATALYTIX_TAG)"
    if [ -z "$(running_version)" ] && [ -n "$REG_IMAGE" ] && [[ "$CUR_TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        OLD_VERSION="$CUR_TAG"      # no container (stopped/removed): the pinned version is what was installed
    fi
    # Installs from before 2.17.1 always wrote the exact version: follow "latest" from now on unless the version
    # was pinned on purpose with --pull=X.Y.Z (marked with CATALYTIX_PINNED=1)
    if [ -z "${PULL_EXPLICIT:-}" ] && [[ "$PULL_TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [ "$(env_get CATALYTIX_PINNED)" != 1 ]; then
        PULL_TAG=latest
        warn "Following the newest version from now on (CATALYTIX_TAG=latest). Pin one with ./update.sh --pull=X.Y.Z."
    fi
fi
if [ -z "$OLD_VERSION" ]; then
    if [ -z "$ZIP" ] && [ "$PULL" = 0 ]; then
        die "No Catalytix installation found in $INSTALL_DIR (catalytix/__init__.py is missing). Install a package with:  ./update.sh catalytix-X.Y.Z.zip"
    fi
    warn "No complete installation found – everything will be installed."
fi
if [ -z "$ZIP" ] && [ "$PULL" = 0 ]; then
    [ -f docker-compose.yml ] || die "docker-compose.yml not found – this script belongs in the Catalytix folder."
fi

# docker-compose.yml reads .env – Compose aborts if it is missing. So create it before any Compose call.
ensure_env() {
    [ -f docker-compose.yml ] || return 0      # new installation: only after unpacking (then from the template)
    [ -e .env ] || [ -L .env ] && return 0
    # Readable for the owner of the program folder only – it may later hold keys and passwords.
    # (If the folder belongs to a member of the "docker" group, Compose keeps working without root.)
    { ( umask 077; if [ -f .env.example ]; then cat .env.example > .env; else : > .env; fi ) \
        && fix_owner .env; } || die "Could not create .env (write permission in the program folder?)."
    ok "Created .env (everything commented out – the defaults apply)."
}

# As root: newly created files belong to the owner of the program folder (not root)
OWNER="$(stat -c %u:%g "$INSTALL_DIR" 2>/dev/null || echo "")"
fix_owner() {
    if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != "0:0" ]; then
        chown -R -h "$OWNER" -- "$@" 2>/dev/null || true
    fi
}

container_running() {   # without "ps --status", which older Compose versions don't know
    local cid
    cid="$("${DC[@]}" ps -q "$SERVICE" 2>/dev/null | head -n1 || true)"
    [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || true)" = running ]
}

zip_tool() {   # unzip, otherwise python3 (unzip is missing on some NAS)
    if command -v unzip >/dev/null 2>&1; then echo unzip
    elif command -v python3 >/dev/null 2>&1; then echo python3
    fi
}
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | cut -d' ' -f1
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print(h.hexdigest())' "$1"
    fi
}
zip_test() {
    case "$(zip_tool)" in
        unzip)   unzip -tq "$1" >/dev/null 2>&1 ;;
        # not "python3 -m zipfile -t": it only prints damaged files as text, with exit code 0
        python3) python3 -c 'import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    sys.exit(1 if z.testzip() is not None else 0)' "$1" >/dev/null 2>&1 ;;
    esac
}
zip_extract() {
    case "$(zip_tool)" in
        unzip)   unzip -q "$1" -d "$2" ;;
        python3) python3 -c 'import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])' "$1" "$2" ;;
    esac
}

# Never touched when installing
KEEP_RE='^(data|instance|\.env|docker-compose\.override\.yml|\.catalytix-update\..*)$'

STAGE=""; SWAP_STATE=""; MOVED=(); PLACED=(); PULLED_TAG=""; PREV_TAG_ID=""
cleanup() {
    local rc=$?
    if [ "$SWAP_STATE" = running ]; then
        rollback || true
    fi
    if [ -n "$PULLED_TAG" ] && [ "$SWAP_STATE" != done ] && [ -n "$PREV_TAG_ID" ]; then
        docker tag "$PREV_TAG_ID" "$PULLED_TAG" >/dev/null 2>&1 || true   # aborted: tag points to the old image again
    fi
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        if [ "$SWAP_STATE" = failed ]; then
            warn "The previous files are kept for safety in: $STAGE/old"
        else
            rm -rf -- "$STAGE"
        fi
    fi
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

rollback() {   # put the previous entries back
    local i name path ok=1
    warn "Error while installing – restoring the previous files …"
    # 1. remove what this script placed and that did not exist before
    for (( i=${#PLACED[@]}-1; i>=0; i-- )); do
        name="${PLACED[$i]}"
        if [ ! -e "$STAGE/old/$name" ] && [ ! -L "$STAGE/old/$name" ]; then
            rm -rf -- "./$name" || ok=0
        fi
    done
    # 2. everything in old/ goes back (also an entry whose move was interrupted before it was recorded);
    #    whatever is in its place now can only be the new version placed by this script
    while IFS= read -r -d '' path; do
        name="${path##*/}"
        if [ -e "./$name" ] || [ -L "./$name" ]; then rm -rf -- "./$name" || { ok=0; continue; }; fi
        mv -- "$path" "./$name" || ok=0
    done < <(find "$STAGE/old" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
    if [ "$ok" = 1 ]; then
        SWAP_STATE=restored
        warn "Previous version restored – nothing changed."
    else
        SWAP_STATE=failed
        printf '%s✘ Restore incomplete! Previous files: %s/old – please copy them back by hand.%s\n' "$R" "$STAGE" "$N" >&2
    fi
}

compare_versions() {   # show installed vs. new version, warn about downgrades
    echo "Installed: ${OLD_VERSION:-none}   $1: ${NEW_VERSION}"
    if [ -z "$OLD_VERSION" ]; then
        :
    elif [ "$NEW_VERSION" = "$OLD_VERSION" ]; then
        warn "Same version as installed."
    elif [ "$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | tail -n1)" = "$OLD_VERSION" ]; then
        warn "This is OLDER than the installed version. An older version may have problems with a newer database."
    fi
}

# ---------------------------------------------------------------- 1. Check the new package
if [ -n "$ZIP" ]; then
    step "Checking new package: $ZIP"
    [ -n "$(zip_tool)" ] || die "unzip (or python3) is needed to unpack – e.g. sudo apt install unzip."
    # Authenticity: compare with the checksum in catalytix-X.Y.Z.zip.sha256 next to the package
    SUM="$(sha256_of "$ZIP")"
    if [ -f "$ZIP.sha256" ]; then
        EXPECTED="$(tr -d '\r' < "$ZIP.sha256" | grep -oiE '^[0-9a-f]{64}' | head -n1 | tr 'A-F' 'a-f')"
        [ -n "$EXPECTED" ] || die "Checksum file $ZIP.sha256 is unreadable – nothing changed."
        [ "$SUM" = "$EXPECTED" ] || die "Checksum does NOT match (package damaged or modified) – nothing changed."
        ok "Checksum matches (SHA-256 ${SUM:0:16}…)."
    else
        warn "No checksum file ($(basename -- "$ZIP").sha256) found. SHA-256 of the package – please compare with the one from your source:"
        echo "    ${SUM:-unknown}"
    fi
    # Work folder inside the program folder: same file system, so swapping is just renaming
    STAGE="$(mktemp -d "$INSTALL_DIR/.catalytix-update.XXXXXX")" \
        || die "No write access to $INSTALL_DIR – nothing changed."
    mkdir "$STAGE/new" "$STAGE/old"
    zip_test "$ZIP" || die "The package is damaged (zip test failed) – nothing changed."
    zip_extract "$ZIP" "$STAGE/new" || die "Could not unpack the package (disk space?) – nothing changed."
    SRC=""
    if [ -f "$STAGE/new/catalytix/__init__.py" ]; then
        SRC="$STAGE/new"                     # package without a top-level folder
    else
        for d in "$STAGE/new"/*/; do
            if [ -f "${d}catalytix/__init__.py" ]; then
                [ -z "$SRC" ] || die "Unexpected package layout (several program folders) – nothing changed."
                SRC="${d%/}"
            fi
        done
    fi
    [ -n "$SRC" ] || die "The package contains no Catalytix program – nothing changed."
    for f in docker-compose.yml Dockerfile docker-entrypoint.sh wsgi.py requirements.txt update.sh; do
        [ -f "$SRC/$f" ] || die "The package is incomplete ($f is missing) – nothing changed."
    done
    bash -n "$SRC/update.sh" 2>/dev/null || die "The package is damaged (update.sh) – nothing changed."
    NEW_VERSION="$(version_of "$SRC")"
    [ -n "$NEW_VERSION" ] || die "Cannot read the version number in the package – nothing changed."
    compare_versions "Package"
    confirm "Replace the files now?" || die "Aborted – nothing changed."
fi

# ---------------------------------------------------------------- 1b. Pull the image built by GitHub (--pull)
if [ "$PULL" = 1 ]; then
    step "Pulling image ${IMAGE}:${PULL_TAG}"
    # Remember what the tag pointed to: if the update is aborted before the files are swapped, the tag is put back,
    # so "docker compose up" (or a container manager) doesn't start the new version without the backup
    PREV_TAG_ID="$(docker image inspect -f '{{.Id}}' "${IMAGE}:${PULL_TAG}" 2>/dev/null || true)"
    PULLED_TAG="${IMAGE}:${PULL_TAG}"
    docker pull "${IMAGE}:${PULL_TAG}" \
        || die "Could not pull ${IMAGE}:${PULL_TAG} (network? version exists? private image → docker login ghcr.io) – nothing changed."
    NEW_VERSION="$(image_version "${IMAGE}:${PULL_TAG}")"
    [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "The image has no valid version label – nothing changed."
    DIGEST="$(docker image inspect -f '{{index .RepoDigests 0}}' "${IMAGE}:${PULL_TAG}" 2>/dev/null || true)"
    echo "Image: ${DIGEST:-${IMAGE}:${PULL_TAG}}"
    compare_versions "Image"
    if [ "$NEW_VERSION" != "$OLD_VERSION" ]; then
        confirm "Switch to version ${NEW_VERSION} now?" || die "Aborted – nothing changed."
    fi
    # Installation files (docker-compose.yml, update.sh, …) that belong to this image
    STAGE="$(mktemp -d "$INSTALL_DIR/.catalytix-update.XXXXXX")" \
        || die "No write access to $INSTALL_DIR – nothing changed."
    mkdir "$STAGE/new" "$STAGE/old"
    CID="$(docker create "${IMAGE}:${PULL_TAG}")" || die "Could not read the image – nothing changed."
    docker cp "$CID:/app/dist/." "$STAGE/new/" >/dev/null 2>&1; CP_RC=$?
    docker rm -f "$CID" >/dev/null 2>&1 || true
    [ "$CP_RC" = 0 ] || die "Could not copy the installation files from the image – nothing changed."
    for f in docker-compose.yml update.sh; do
        [ -f "$STAGE/new/$f" ] || die "The image contains no installation files ($f is missing) – nothing changed."
    done
    bash -n "$STAGE/new/update.sh" 2>/dev/null || die "update.sh in the image is damaged – nothing changed."
    SRC="$STAGE/new"
fi

# ---------------------------------------------------------------- 2. Backup (before replacing anything)
if [ "$BACKUP" = 1 ]; then
    step "Backup before the update"
    ensure_env
    if container_running; then
        # Through the entrypoint: it drops root, so the backup belongs to PUID:PGID
        if "${DC[@]}" exec -T "$SERVICE" docker-entrypoint.sh flask --app wsgi backup; then
            ok "Backup created (Administration → Backup)."
        else
            warn "Backup failed (normal with MySQL/PostgreSQL – back those up with mysqldump/pg_dump)."
            confirm "Continue anyway?" || die "Aborted – nothing changed."
        fi
    else
        warn "Container is not running – no backup possible (first installation?)."
    fi
fi

# ---------------------------------------------------------------- 3. Swap the files
if [ -n "$ZIP" ] || [ "$PULL" = 1 ]; then
    step "Installing files of version ${NEW_VERSION}"
    # Replace the program files completely (deleted files disappear too),
    # everything personal stays: data, .env, docker-compose.override.yml, instance.
    # Only entries inside the program folder are renamed – the folder itself (permissions, owner) stays.
    ENTRIES=()
    while IFS= read -r -d '' path; do
        name="${path##*/}"
        [[ "$name" =~ $KEEP_RE ]] || ENTRIES+=("$name")
    done < <(find "$SRC" -mindepth 1 -maxdepth 1 -print0)
    if [ -n "$ZIP" ]; then
        STALE=(catalytix tests)             # also remove old program folders the package no longer has
    else
        # --pull: the program is in the image – program files of an earlier local install would only be stale
        STALE=(catalytix tests docs Dockerfile docker-entrypoint.sh wsgi.py requirements.txt requirements.in .dockerignore)
    fi
    for name in "${STALE[@]}"; do
        if [ -e "./$name" ] && [ ! -e "$SRC/$name" ]; then ENTRIES+=("$name"); fi
    done
    SWAP_STATE=running
    for name in "${ENTRIES[@]}"; do
        if [ -e "./$name" ] || [ -L "./$name" ]; then
            mv -- "./$name" "$STAGE/old/$name" || { rollback; die "Installation failed ($name) – the previous version is unchanged."; }
            MOVED+=("$name")
        fi
        if [ -e "$SRC/$name" ]; then
            mv -- "$SRC/$name" "./$name" || { rollback; die "Installation failed ($name) – the previous version is unchanged."; }
            PLACED+=("$name")
        fi
    done
    if [ -n "$ZIP" ]; then
        [ "$(version_of .)" = "$NEW_VERSION" ] || { rollback; die "Installation failed (check) – the previous version is unchanged."; }
    else
        [ -f docker-compose.yml ] && [ -f update.sh ] || { rollback; die "Installation failed (check) – the previous version is unchanged."; }
    fi
    SWAP_STATE=done
    # Don't take permissions from the package blindly: nothing may be writable for others
    for name in "${ENTRIES[@]}"; do
        if [ -e "./$name" ] && [ ! -L "./$name" ]; then
            chmod -R go-w -- "./$name" 2>/dev/null || true
            fix_owner "./$name"
        fi
    done
    chmod +x ./update.sh ./docker-entrypoint.sh 2>/dev/null || true
    rm -rf -- "$STAGE"; STAGE=""
    ok "Files of version ${NEW_VERSION} installed."
fi
[ -f docker-compose.yml ] || die "docker-compose.yml not found – this script belongs in the Catalytix folder."
ensure_env
if [ "$PULL" = 1 ]; then
    VERSION="$NEW_VERSION"
    # Remember the image in .env. --pull follows "latest" (container managers that update images can then pull
    # new versions themselves); --pull=X.Y.Z pins exactly that version until the next --pull.
    [ "$PULL_TAG" = "$VERSION" ] || docker tag "${IMAGE}:${PULL_TAG}" "${IMAGE}:${VERSION}"
    env_set CATALYTIX_IMAGE "$IMAGE"
    if [ "$PULL_TAG" = latest ]; then
        env_set CATALYTIX_TAG latest
        env_unset CATALYTIX_PINNED
    else
        env_set CATALYTIX_TAG "$VERSION"
        env_set CATALYTIX_PINNED 1
        warn "Pinned to version ${VERSION} – \"./update.sh --pull\" follows the newest version again."
    fi
else
    VERSION="$(version_of .)"
    if [ -n "$REG_IMAGE" ]; then
        env_unset CATALYTIX_IMAGE
        env_unset CATALYTIX_TAG
        warn "Switched from GitHub images back to building locally (CATALYTIX_IMAGE commented out in .env)."
    fi
fi

# ---------------------------------------------------------------- 4. Build
if [ "$PULL" = 1 ]; then
    step "Image ${IMAGE}:${VERSION}"
    ok "Built by GitHub – nothing to build here."
else
    step "Building image catalytix:${VERSION}"
    # No --pull needed: the base image is pinned by digest in the Dockerfile.
    "${DC[@]}" build $NOCACHE "$SERVICE" || die "Build failed – the running container is unchanged."
    ok "Image built."
fi

way_back() {
    if [ "$PULL" = 1 ] && [ -n "$OLD_VERSION" ]; then
        echo "./update.sh --pull=${OLD_VERSION}"
    else
        echo "install the old package with ./update.sh catalytix-OLD.zip"
    fi
}

# ---------------------------------------------------------------- 5. Start
step "Restarting the container"
UP_OPTS=(-d --remove-orphans)
[ "$PULL" = 1 ] && UP_OPTS+=(--no-build)
"${DC[@]}" up "${UP_OPTS[@]}" "$SERVICE" \
    || die "Could not start the container (see message above). To go back: $(way_back)"

printf 'Waiting for start '
STATUS=""
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
    CID="$("${DC[@]}" ps -q "$SERVICE" 2>/dev/null || true)"
    if [ -n "$CID" ]; then
        STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CID" 2>/dev/null || true)"
        case "$STATUS" in
            healthy) break ;;
            unhealthy|exited|dead) break ;;
        esac
    fi
    printf '.'
    sleep 1
done
echo

if [ "$STATUS" = healthy ]; then
    ok "Catalytix ${VERSION} is running."
    # First-time setup still open? Then show the setup code (asked for on the setup page)
    SETUP_CODE="$("${DC[@]}" exec -T "$SERVICE" docker-entrypoint.sh flask --app wsgi setup-code 2>/dev/null \
                  | grep -oE '^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$' | head -n1 || true)"
    if [ -n "$SETUP_CODE" ]; then
        printf '\n%sSetup code for the first-time setup: %s%s\n' "$B" "$SETUP_CODE" "$N"
        echo "Open http://SERVER:PORT in a browser and create the administrator account with this code."
    fi
else
    warn "Status: ${STATUS:-unknown}. The last log lines:"
    "${DC[@]}" logs --tail 40 "$SERVICE" || true
    die "The container did not start healthy. To go back: $(way_back)"
fi

# ---------------------------------------------------------------- 6. Clean up
ver_gt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]; }
keep_version() {   # keep this version? current, previous (way back) and newer ones (after going back)
    local v="$1"
    [ "$v" = "$VERSION" ] && return 0
    [ "$PRUNE" = 1 ] && return 1
    [ -n "$OLD_VERSION" ] && [ "$v" = "$OLD_VERSION" ] && return 0
    ver_gt "$v" "$VERSION"
}
human() { awk -v b="$1" 'BEGIN { split("B KB MB GB TB", u); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ } printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i] }'; }

if [ "$CLEANUP" = 1 ] || [ "$PRUNE" = 1 ]; then
    step "Cleaning up"
    FREED=0; REMOVED=0

    # Images: <repository>:<version> except the kept ones (local builds and GitHub images),
    # then unnamed Catalytix leftovers from building
    REPOS=(catalytix)
    [ "$PULL" = 1 ] && REPOS+=("$IMAGE")
    for repo in "${REPOS[@]}"; do
        while read -r ref; do
            [ -n "$ref" ] || continue
            tag="${ref##*:}"
            keep_version "$tag" && continue
            if docker image rm "$ref" >/dev/null 2>&1; then
                echo "  Removed image: $ref"; REMOVED=$((REMOVED + 1))
            fi
        done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' "$repo" 2>/dev/null \
                 | grep -E "^${repo//./\\.}:[0-9]+\.[0-9]+\.[0-9]+\$" || true)
    done
    docker image prune -f --filter "label=org.opencontainers.image.title=Catalytix" >/dev/null 2>&1 || true

    # Zip packages in the program folder and next to the installed package (only real files named catalytix-X.Y.Z.zip)
    ZIP_DIRS=("$INSTALL_DIR")
    if [ -n "$ZIP" ] && [ "$(dirname -- "$ZIP")" != "$INSTALL_DIR" ]; then ZIP_DIRS+=("$(dirname -- "$ZIP")"); fi
    for dir in "${ZIP_DIRS[@]}"; do
        # Don't touch shared folders (e.g. /tmp: writable for everyone)
        if [ -k "$dir" ] || [ -n "$(find "$dir" -maxdepth 0 -perm -o+w -print 2>/dev/null)" ]; then
            continue
        fi
        while IFS= read -r -d '' f; do
            name="${f##*/}"
            v="${name#catalytix-}"; v="${v%.zip}"
            [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
            keep_version "$v" && continue
            size="$(stat -c %s -- "$f" 2>/dev/null || echo 0)"
            if rm -f -- "$f"; then
                echo "  Removed package: $f"; FREED=$((FREED + size)); REMOVED=$((REMOVED + 1))
            fi
        done < <(find "$dir" -maxdepth 1 -type f -name 'catalytix-*.zip' -user "$(id -u)" -print0 2>/dev/null)
    done

    # Work folders of aborted updates: removed after 7 days (until then they may hold old files for safety)
    while IFS= read -r -d '' d; do
        size="$(du -sb -- "$d" 2>/dev/null | cut -f1 || echo 0)"
        if rm -rf -- "$d"; then
            echo "  Removed work folder: $d"; FREED=$((FREED + ${size:-0})); REMOVED=$((REMOVED + 1))
        fi
    done < <(find "$INSTALL_DIR" -maxdepth 1 -type d -name '.catalytix-update.*' -mtime +7 -print0 2>/dev/null)
    while IFS= read -r -d '' d; do
        warn "Work folder of an aborted update (kept for 7 days in case files from it are needed): $d"
    done < <(find "$INSTALL_DIR" -maxdepth 1 -type d -name '.catalytix-update.*' -print0 2>/dev/null)

    # Python caches in the program folder (e.g. from test runs) – never in ./data
    while IFS= read -r -d '' d; do
        rm -rf -- "$d" && REMOVED=$((REMOVED + 1))
    done < <(find "$INSTALL_DIR" -path "$INSTALL_DIR/data" -prune -o -type d \( -name __pycache__ -o -name .pytest_cache \) -print0 2>/dev/null)

    if [ "$REMOVED" = 0 ]; then
        ok "Nothing to clean up."
    else
        ok "Cleaned up: $REMOVED entries removed, $(human "$FREED") freed (in addition to the images)."
    fi
    if [ "$PRUNE" != 1 ] && [ -n "$OLD_VERSION" ] && [ "$OLD_VERSION" != "$VERSION" ]; then
        echo "Image and package of ${OLD_VERSION} are kept for the way back (delete everything old: ./update.sh --prune)."
    fi
fi

PORT=""
[ -f .env ] && PORT="$(sed -n 's/^CATALYTIX_PORT=\([0-9]*\).*/\1/p' .env 2>/dev/null | tail -n1 || true)"
printf '\n%sDone.%s  %s → %s   http://%s:%s\n' "$B" "$N" "${OLD_VERSION:-new}" "$VERSION" "$(hostname -f 2>/dev/null || hostname)" "${PORT:-9300}"
}

main "$@"
exit
