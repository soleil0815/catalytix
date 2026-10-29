#!/bin/sh
# ------------------------------------------------------------------
# Catalytix – update by hand (with automatic way back)
#
#   ./update.sh                      newest version (or the version set in .env)
#   ./update.sh --version=X.Y.Z      exactly this version and stay on it (also a security update X.Y.Z-sN);
#                                    --version=latest follows every new version again
#   ./update.sh --load FILE          without internet: image file from the release
#                                    (catalytix-X.Y.Z-amd64.tar.gz / -arm64), checked against its .sha256
#   ./update.sh --cleanup            installations from before 3.0.0: list program files that are no longer
#                                    needed (old zip installs), ask, then delete only those
#
# Options:
#   -y, --yes     no questions (also: go back automatically if the new version does not start)
#   -h, --help    this help
#
# What happens: the running image is kept as "previous", the new one is pulled and started, and the script
# waits for the health check. The program backs up the database itself before a version change
# (backup "…-vor-X.Y.Z"). If the new version does not start, the script restores that backup and starts the
# previous image again. If the image brings a newer docker-compose.yml, it is put in place and the old one kept
# as docker-compose.yml.alt.
#
# Never touched: ./data (database, covers, keys, backups), .env (except CATALYTIX_TAG),
# docker-compose.override.yml.
# Container managers (Dockhand, Watchtower …) or "docker compose pull && docker compose up -d" work as well.
# ------------------------------------------------------------------
set -eu

SERVICE=catalytix
CONTAINER=catalytix
DEFAULT_IMAGE=ghcr.io/soleil0815/catalytix
HEALTH_TIMEOUT="${CATALYTIX_HEALTH_TIMEOUT:-300}"   # seconds (the image allows 120 s start-up, e.g. large ./data)
HEALTH_POLL="${CATALYTIX_HEALTH_POLL:-3}"

# ---------------------------------------------------------------- Output
if [ -t 1 ]; then
    B=$(printf '\033[1m'); G=$(printf '\033[32m'); Y=$(printf '\033[33m'); R=$(printf '\033[31m'); N=$(printf '\033[0m')
else
    B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ---------------------------------------------------------------- Arguments
YES=0; MODE=update; VERSION_ARG=""; LOAD=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version=*) VERSION_ARG="${1#--version=}" ;;
        --version)   [ $# -ge 2 ] || die "--version needs a value, e.g. --version=3.0.0"; VERSION_ARG="$2"; shift ;;
        --load=*)    LOAD="${1#--load=}" ;;
        --load)      [ $# -ge 2 ] || die "--load needs a file"; LOAD="$2"; shift ;;
        --cleanup)   MODE=cleanup ;;
        -y|--yes)    YES=1 ;;
        -h|--help)   usage ;;
        *)           die "Unknown option: $1  (help: ./update.sh --help)" ;;
    esac
    shift
done

is_version() { printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; }
is_tag()     { [ "$1" = latest ] || printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-s[0-9]+)?$'; }
if [ -n "$VERSION_ARG" ] && ! is_tag "$VERSION_ARG"; then
    die "--version=… expects a version like 3.0.0, a security update like 3.0.0-s1, or latest."
fi
[ -z "$LOAD" ] || [ -z "$VERSION_ARG" ] || die "Either --load or --version, not both."

# Resolve the image file BEFORE changing into the program folder (also relative paths like ../x.tar.gz)
if [ -n "$LOAD" ]; then
    [ -f "$LOAD" ] || die "File not found: $LOAD"
    LOAD="$(cd "$(dirname -- "$LOAD")" && pwd -P)/$(basename -- "$LOAD")"
fi
DIR="$(cd "$(dirname -- "$0")" && pwd -P)" || die "Program folder not accessible."
cd "$DIR" || die "Program folder not accessible: $DIR"

confirm() {   # confirm "Question" [default y|n]
    [ "$YES" = 1 ] && return 0
    # Without a terminal (cron, ssh without -t) and without -y: never assume an answer
    if [ ! -t 0 ]; then echo "$1 – no input possible (use -y to run without questions)." >&2; return 1; fi
    if [ "${2:-y}" = y ]; then printf '%s [Y/n] ' "$1"; else printf '%s [y/N] ' "$1"; fi
    read -r a || a=""
    case "${a:-${2:-y}}" in [yYjJ]*) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- Helpers
realpath_of() {   # full path with all links resolved (also for missing last parts)
    readlink -f -- "$1" 2>/dev/null && return 0
    if [ -d "$1" ]; then (cd -P -- "$1" && pwd -P); else
        printf '%s/%s\n' "$(cd -P -- "$(dirname -- "$1")" && pwd -P)" "$(basename -- "$1")"; fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | cut -d' ' -f1
    elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 -r -- "$1" | cut -d' ' -f1
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print(h.hexdigest())' "$1"
    fi
}

ver_gt() {   # ver_gt A B: version A is newer than B
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n1)" = "$1" ]
}

# ---------------------------------------------------------------- .env
env_get() {   # last value of KEY in .env (without quotes), empty if not set
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env 2>/dev/null | tail -n1 | tr -d '"'"'"' \r' || true
}
env_write() {   # replace .env with the content of file $1 – owner and permissions of .env stay
    cat -- "$1" > .env || { rm -f -- "$1"; die "Cannot write .env."; }
    rm -f -- "$1"
}
env_set() {   # set KEY=VALUE: replace an existing (or commented-out) line, otherwise append
    printf '%s\n' "$2" | grep -Eq '^[A-Za-z0-9._/:-]+$' || die "Invalid value for $1: $2"
    _t="$(mktemp "$DIR/.catalytix-env.XXXXXX")" || die "Cannot write .env."
    if grep -qE "^#?$1=" .env 2>/dev/null; then
        awk -v k="$1" -v v="$2" 'BEGIN{done=0} { if (!done && ($0 ~ "^"k"=" || $0 ~ "^#"k"=")) { print k"="v; done=1 } else if ($0 ~ "^"k"=") { print "#"$0 } else print }' .env > "$_t"
    else
        { cat .env 2>/dev/null; printf '%s=%s\n' "$1" "$2"; } > "$_t"
    fi
    env_write "$_t"
}
env_unset() {   # comment out KEY
    grep -qE "^$1=" .env 2>/dev/null || return 0
    _t="$(mktemp "$DIR/.catalytix-env.XXXXXX")" || die "Cannot write .env."
    sed "s/^$1=/#$1=/" .env > "$_t" || { rm -f -- "$_t"; die "Cannot write .env."; }
    env_write "$_t"
}
ensure_env() {   # docker-compose.yml reads .env – Compose aborts if it is missing
    [ -e .env ] || [ -L .env ] && return 0
    # Readable for the owner only – it may later hold keys and passwords
    ( umask 077; if [ -f .env.example ]; then cat .env.example > .env; else : > .env; fi ) \
        || die "Could not create .env (write permission in this folder?)."
    fix_owner .env
    ok "Created .env (everything commented out – the defaults apply)."
}

# As root: new files belong to the owner of the program folder (not root)
OWNER="$(stat -c %u:%g "$DIR" 2>/dev/null || echo "")"
fix_owner() {
    if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != "0:0" ]; then chown -h "$OWNER" -- "$@" 2>/dev/null || true; fi
}

# ---------------------------------------------------------------- Docker
command -v docker >/dev/null 2>&1 || die "Docker is not installed."
docker info >/dev/null 2>&1 || die "No access to Docker. Run as root or as a member of the \"docker\" group."
# "docker compose" (plugin) or the older standalone "docker-compose" (e.g. Synology) from 1.27 on
if docker compose version >/dev/null 2>&1; then DCV=2
elif command -v docker-compose >/dev/null 2>&1; then DCV=1
else die "Docker Compose is missing (neither \"docker compose\" nor \"docker-compose\" found)."; fi
# Reuse the project name of the running container – otherwise e.g. a symlinked folder gives another project name
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER" 2>/dev/null || true)"
dc() {
    if [ "$DCV" = 2 ]; then
        if [ -n "$PROJECT" ]; then docker compose -p "$PROJECT" "$@"; else docker compose "$@"; fi
    else
        if [ -n "$PROJECT" ]; then docker-compose -p "$PROJECT" "$@"; else docker-compose "$@"; fi
    fi
}
label_of() {   # label_of IMAGE KEY
    docker image inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true
}
running_image() { docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true; }
run_previous() {   # run a flask command in the previous image (data and .env as configured), database check off
    ( CATALYTIX_TAG=previous; CATALYTIX_IMAGE="$IMAGE"; export CATALYTIX_TAG CATALYTIX_IMAGE
      dc run --rm --no-deps -e CATALYTIX_SKIP_DB_CHECK=1 "$SERVICE" flask --app wsgi "$@" )
}

compose_version_of() {   # number in the line CATALYTIX_COMPOSE: "N" (0 = old file without it)
    _v="$(sed -n 's/^[[:space:]]*CATALYTIX_COMPOSE:[[:space:]]*"\{0,1\}\([0-9][0-9]*\)"\{0,1\}.*/\1/p' "$1" 2>/dev/null | head -n1)"
    echo "${_v:-0}"
}

# ---------------------------------------------------------------- Clean up (installations from before 3.0.0)
# Fixed list of program files of the old zip installation – nothing else is ever offered for deletion.
KNOWN="catalytix tests docs tools docker Dockerfile docker-entrypoint.sh wsgi.py requirements.txt requirements.in
.dockerignore .trivyignore .trivy .github .gitignore README.md README.en.md CHANGELOG.md LICENSE __pycache__"

protected_paths() {   # never deleted, never entered: data, settings, Compose files, everything a container mounts
    for p in data instance backups .env docker-compose.yml docker-compose.override.yml docker-compose.yml.alt \
             update.sh; do
        [ -e "$p" ] || [ -L "$p" ] || continue
        realpath_of "$p"
    done
    # Links inside the data folders point to data as well (e.g. data/backups → another folder)
    for d in data instance; do
        [ -d "$d" ] || continue
        find "$d" -type l 2>/dev/null | while IFS= read -r l; do realpath_of "$l"; done
    done
    for c in $(docker ps -aq 2>/dev/null); do
        docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$c" 2>/dev/null | while IFS= read -r m; do
            if [ -n "$m" ]; then realpath_of "$m"; fi
        done
    done
}
mount_points() {   # everything mounted on the host (a NAS share inside a folder must never be emptied)
    [ -r /proc/mounts ] || return 0
    awk '{ gsub(/\\040/, " ", $2); print $2 }' /proc/mounts
}
touches() {   # touches A B: one path is the other or lies inside it
    case "$1/" in "$2"/*) return 0 ;; esac
    case "$2/" in "$1"/*) return 0 ;; esac
    return 1
}
safe_to_delete() {   # safe_to_delete NAME → 0 if it may go; prints the reason otherwise
    _r="$(realpath_of "$1")" || { echo "path cannot be resolved"; return 1; }
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        if touches "$_r" "$p"; then echo "contains or is inside $p (data / mounted)"; return 1; fi
    done < "$PROT"
    if [ ! -L "$1" ]; then
        case "$_r" in "$DIR"/*) ;; *) echo "lies outside the program folder ($_r)"; return 1 ;; esac
        while IFS= read -r m; do
            [ -n "$m" ] && [ "$m" != / ] || continue
            case "$m/" in "$_r"/*) echo "contains the mount point $m"; return 1 ;; esac
        done <<EOF
$(mount_points)
EOF
    fi
    return 0
}

do_cleanup() {
    step "Program files no longer needed (since 3.0.0 the program is completely in the image)"
    if [ -d .git ]; then die "This folder is a Git checkout (.git) – not cleaning up here."; fi
    PROT=""; LIST=""
    PROT="$(mktemp "$DIR/.catalytix-protect.XXXXXX")" || die "Cannot write in $DIR."
    trap 'rm -f -- "$PROT" "$LIST"' EXIT
    LIST="$(mktemp "$DIR/.catalytix-list.XXXXXX")" || die "Cannot write in $DIR."
    protected_paths > "$PROT"
    found=0
    for name in $KNOWN catalytix-*.zip catalytix-*.zip.sha256 .catalytix-update.*; do
        [ -e "$name" ] || [ -L "$name" ] || continue
        if why="$(safe_to_delete "$name")"; then
            printf '%s\n' "$name" >> "$LIST"; found=$((found + 1))
            if [ -L "$name" ]; then echo "  $name  (link – only the link is removed)"
            else echo "  $name  ($(du -sh -- "$name" 2>/dev/null | cut -f1))"; fi
        else
            warn "  $name  – skipped: $why"
        fi
    done
    IMAGES="$(docker image ls --format '{{.Repository}}:{{.Tag}}' catalytix 2>/dev/null | grep -E '^catalytix:[0-9]+\.[0-9]+\.[0-9]+$' || true)"
    for i in $IMAGES; do echo "  image $i  (built locally by the old zip installation)"; done
    if [ "$found" = 0 ] && [ -z "$IMAGES" ]; then ok "Nothing to clean up."; return 0; fi
    echo "Never touched: data, backups, .env, docker-compose.yml (and .alt/.override), mounted folders."
    confirm "Delete the entries listed above?" n || { echo "Nothing deleted."; return 0; }
    while IFS= read -r name; do
        if [ -L "$name" ]; then rm -f -- "$name"; else rm -rf -- "$name"; fi && echo "  deleted: $name"
    done < "$LIST"
    for i in $IMAGES; do docker image rm "$i" >/dev/null 2>&1 && echo "  deleted: image $i" || warn "  $i is in use – kept"; done
    ok "Cleaned up."
}

if [ "$MODE" = cleanup ]; then
    do_cleanup
    exit 0
fi

# ---------------------------------------------------------------- Update
TMP=""; ENV_BAK=""; COMPOSE_SWAPPED=0
cleanup_tmp() { [ -z "$TMP" ] || rm -rf -- "$TMP"; [ -z "$ENV_BAK" ] || rm -f -- "$ENV_BAK"; }
trap cleanup_tmp EXIT
trap 'exit 130' INT TERM
TMP="$(mktemp -d "$DIR/.catalytix-update.XXXXXX")" || die "No write access to $DIR."

ensure_env
IMAGE="$(env_get CATALYTIX_IMAGE)"
case "$IMAGE" in ""|catalytix) IMAGE="$DEFAULT_IMAGE" ;; esac   # "catalytix" = old locally built image
OLD_TAG="$(env_get CATALYTIX_TAG)"
is_tag "${OLD_TAG:-latest}" || OLD_TAG=""
WANT_TAG="${VERSION_ARG:-${OLD_TAG:-latest}}"

PREV_IMG="$(running_image)"
PREV_VER=""
if [ -n "$PREV_IMG" ]; then
    PREV_VER="$(label_of "$PREV_IMG" org.opencontainers.image.version)"
    is_version "$PREV_VER" || PREV_VER=""
    docker tag "$PREV_IMG" "$IMAGE:previous" || die "Could not keep the running image as $IMAGE:previous."
fi
START="$(date +%s)"
cp -p .env "$TMP/env" 2>/dev/null || cat .env > "$TMP/env"
ENV_BAK="$TMP/env"

# ---- 1. Get the image
if [ -n "$LOAD" ]; then
    step "Loading image file $(basename -- "$LOAD")"
    SUM="$(sha256_of "$LOAD")"
    EXPECT=""
    if [ -f "$LOAD.sha256" ]; then EXPECT="$(cut -d' ' -f1 < "$LOAD.sha256")"
    elif [ -f "$(dirname -- "$LOAD")/SHA256SUMS" ]; then
        EXPECT="$(grep -F " $(basename -- "$LOAD")" "$(dirname -- "$LOAD")/SHA256SUMS" | grep -v '\.sha256$' | cut -d' ' -f1 | head -n1)"
    fi
    if [ -z "$EXPECT" ]; then
        warn "No checksum file next to it (.sha256 or SHA256SUMS from the release). SHA-256: $SUM"
        confirm "Compare it with the release page – load anyway?" n || die "Aborted – nothing changed."
    elif [ "$EXPECT" != "$SUM" ]; then
        die "Checksum does NOT match (file damaged or not from the release) – nothing changed."
    else
        ok "Checksum matches."
    fi
    OUT="$(docker load -i "$LOAD")" || die "Could not load the image file – nothing changed."
    NEW_REF="$(printf '%s\n' "$OUT" | sed -n 's/^Loaded image: //p' | tail -n1)"
    [ -n "$NEW_REF" ] || die "The file contains no tagged image – nothing changed."
    IMAGE="${NEW_REF%:*}"; WANT_TAG="${NEW_REF##*:}"
    is_tag "$WANT_TAG" || die "Unexpected image in the file: $NEW_REF – nothing changed."
    VERSION_ARG="$WANT_TAG"           # stays on exactly this version (no registry to follow without internet)
else
    step "Getting $IMAGE:$WANT_TAG"
    docker pull "$IMAGE:$WANT_TAG" \
        || die "Could not get $IMAGE:$WANT_TAG (network? does the version exist?) – nothing changed."
    NEW_REF="$IMAGE:$WANT_TAG"
fi
NEW_IMG="$(docker image inspect -f '{{.Id}}' "$NEW_REF" 2>/dev/null || true)"
NEW_VER="$(label_of "$NEW_REF" org.opencontainers.image.version)"
is_version "$NEW_VER" || die "The image has no valid version label – nothing changed."
echo "Running: ${PREV_VER:-none}   new: $NEW_VER"
[ "$NEW_IMG" != "$PREV_IMG" ] || ok "This image is already running – checking the files and the start."

# ---- 2. Going back to an older version: the database must be restored to the state before the newer version
if [ -n "$PREV_VER" ] && ver_gt "$PREV_VER" "$NEW_VER"; then
    warn "$NEW_VER is OLDER than the running $PREV_VER. The database is put back to the backup taken before the"
    warn "update to $PREV_VER – everything entered since then is lost."
    confirm "Go back to $NEW_VER?" n || die "Aborted – nothing changed."
    dc stop "$SERVICE" >/dev/null 2>&1 || docker stop "$CONTAINER" >/dev/null 2>&1 || true
    if ver_gt 3.0.0 "$PREV_VER"; then
        warn "$PREV_VER cannot restore backups by version (before 3.0.0) – starting $NEW_VER on the current database."
    else
        set +e
        run_previous restore-for-version "$NEW_VER" --yes
        RC=$?
        set -e
        if [ "$RC" != 0 ]; then
            dc up -d "$SERVICE" >/dev/null 2>&1 || true
            die "No backup a version up to $NEW_VER can use (MySQL/PostgreSQL, or deleted) – $PREV_VER keeps running. Restore your own dump first, then run this again."
        fi
        ok "Database restored to a backup taken under version $NEW_VER or older."
    fi
fi

# ---- 3. Files from the image: newer docker-compose.yml (old one kept as .alt), update.sh, .env.example
CID="$(docker create "$NEW_REF" 2>/dev/null)" || die "Could not read the image – nothing changed."
docker cp "$CID:/app/dist/." "$TMP/" >/dev/null 2>&1 || true
docker rm -f "$CID" >/dev/null 2>&1 || true
if [ -f "$TMP/docker-compose.yml" ]; then
    NEW_CV="$(compose_version_of "$TMP/docker-compose.yml")"
    CUR_CV=0; [ -f docker-compose.yml ] && CUR_CV="$(compose_version_of docker-compose.yml)"
    if [ ! -f docker-compose.yml ] || [ "$NEW_CV" -gt "$CUR_CV" ]; then
        if [ -f docker-compose.yml ]; then
            cp -p docker-compose.yml docker-compose.yml.alt
            ok "New docker-compose.yml (version $NEW_CV) – the previous one is kept as docker-compose.yml.alt."
        fi
        cat "$TMP/docker-compose.yml" > "$TMP/compose.new" && mv -f "$TMP/compose.new" docker-compose.yml
        chmod 644 docker-compose.yml 2>/dev/null || true; fix_owner docker-compose.yml
        COMPOSE_SWAPPED=1
    fi
fi
[ -f docker-compose.yml ] || die "docker-compose.yml is missing and the image brings none."
if [ -f "$TMP/.env.example" ] && ! cmp -s "$TMP/.env.example" .env.example 2>/dev/null; then
    cat "$TMP/.env.example" > .env.example && chmod 644 .env.example 2>/dev/null; fix_owner .env.example
fi

# ---- 4. Remember the wanted version in .env (only when chosen explicitly)
if [ -n "$VERSION_ARG" ]; then
    env_set CATALYTIX_TAG "$VERSION_ARG"
    [ "$VERSION_ARG" = latest ] || warn "Fixed to $VERSION_ARG – \"./update.sh --version=latest\" follows every new version again."
fi
env_unset CATALYTIX_PINNED                         # no longer used (before 3.0.0)
[ "$(env_get CATALYTIX_IMAGE)" != catalytix ] || env_unset CATALYTIX_IMAGE

# ---- 5. Start and wait for the health check
step "Starting version $NEW_VER"
wait_healthy() {
    _waited=0
    while [ "$_waited" -lt "$HEALTH_TIMEOUT" ]; do
        st="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo "missing")"
        set -- $st
        case "${1:-missing} ${2:-}" in
            "running healthy") return 0 ;;
            *unhealthy*|exited*|dead*|missing*) return 1 ;;
        esac
        [ "${3:-0}" -lt 3 ] 2>/dev/null || return 1     # keeps crashing and restarting
        sleep "$HEALTH_POLL"; _waited=$((_waited + HEALTH_POLL))
    done
    return 1
}
STARTED=0
if dc up -d --remove-orphans "$SERVICE"; then
    wait_healthy && STARTED=1
fi
if [ "$STARTED" = 1 ]; then
    ok "Version $NEW_VER is running."
    [ -z "$PREV_VER" ] || [ "$PREV_VER" = "$NEW_VER" ] || echo "Way back if needed: ./update.sh --version=$PREV_VER"
    if [ -f "$TMP/update.sh" ] && ! cmp -s "$TMP/update.sh" "$DIR/update.sh"; then
        # The running shell keeps reading the old file (it stays open); the new one is used next time
        cat "$TMP/update.sh" > "$TMP/update.new" && chmod 755 "$TMP/update.new" && mv -f "$TMP/update.new" "$DIR/update.sh"
        fix_owner "$DIR/update.sh"
    fi
    for name in $KNOWN catalytix-*.zip; do
        if [ -e "$name" ]; then echo "Files of the old zip installation are still here – remove them with:  ./update.sh --cleanup"; break; fi
    done
    exit 0
fi

# ---- 6. Did not start: back to the previous version
warn "Version $NEW_VER did not start properly. Last log lines:"
docker logs --tail 30 "$CONTAINER" 2>&1 || true
[ -n "$PREV_IMG" ] || die "There is no previous version to go back to (first installation). See the log above."
confirm "Go back to version ${PREV_VER:-before}?" y || die "Left as it is. Back by hand: ./update.sh --version=${PREV_VER:-X.Y.Z}"
step "Going back to ${PREV_VER:-the previous version}"
dc stop "$SERVICE" >/dev/null 2>&1 || docker stop "$CONTAINER" >/dev/null 2>&1 || true
env_write_copy() { cp "$ENV_BAK" "$TMP/env.restore" && env_write "$TMP/env.restore"; }
env_write_copy
if [ "$COMPOSE_SWAPPED" = 1 ] && [ -f docker-compose.yml.alt ]; then
    cat docker-compose.yml.alt > docker-compose.yml
fi
# The program saved the database before changing it ("…-vor-$NEW_VER") – put that back (only a backup from
# this run). Images before 3.0.0 don't know the command; they don't check the database version either.
if [ -n "$PREV_VER" ] && ! ver_gt 3.0.0 "$PREV_VER"; then
    set +e
    run_previous restore-before "$NEW_VER" --since "$START" --yes
    RC=$?
    set -e
    case "$RC" in
        0) ok "Database restored to the state before $NEW_VER." ;;
        3) ok "The database was not changed by $NEW_VER." ;;
        *) warn "Could not restore the backup (see above) – check Administration → Backup after the start." ;;
    esac
fi
# Stay on the previous version, so a container manager does not install the failing one again right away
BACK_TAG="${OLD_TAG:-latest}"
if [ "$BACK_TAG" = latest ] && [ -n "$PREV_VER" ]; then
    BACK_TAG="$PREV_VER"
    env_set CATALYTIX_TAG "$BACK_TAG"
    warn "Fixed to $PREV_VER for now. When a newer version is out: ./update.sh --version=latest"
fi
docker tag "$IMAGE:previous" "$IMAGE:$BACK_TAG" || true
dc up -d "$SERVICE" || true
if wait_healthy; then
    ok "Back on version ${PREV_VER:-before}. $NEW_VER did not start – see the log lines above."
else
    warn "The previous version does not start either – see: docker logs $CONTAINER"
fi
exit 1
