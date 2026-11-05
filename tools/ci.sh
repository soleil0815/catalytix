# Shell helpers for the GitHub workflows – sourced with ". tools/ci.sh" (bash, set -e -o pipefail).
# Every failure is reported as a readable annotation on the run ("::error title=…::…").
# The workflows run with "shell: bash" (bash -eo pipefail); set here as well so that a pipe can never hide a failure.
set -o pipefail

fail() {   # fail "Title" "Message"
    echo "::error title=$1::$2"
    exit 1
}

image_name() {
    echo "ghcr.io/${GITHUB_REPOSITORY,,}"
}

# 0 = the tag exists, 1 = it does not exist; any other answer of the registry stops the run (a hiccup must never be
# taken for "not published" – that could overwrite a published version).
image_exists() {
    local out rc
    out="$(docker buildx imagetools inspect "$1" 2>&1)" && return 0
    rc=$?
    if grep -qiE "not found|manifest unknown|404" <<<"$out"; then
        return 1
    fi
    fail "Registry not reachable" "Could not check $1 (exit $rc): $(head -c 300 <<<"$out")"
}

# 0 = release v$1 exists, 1 = it does not; other API errors stop the run
release_exists() {
    local out
    out="$(gh api "repos/$GITHUB_REPOSITORY/releases/tags/v$1" --jq .tag_name 2>&1)" && return 0
    if grep -q "HTTP 404" <<<"$out"; then
        return 1
    fi
    fail "GitHub API not reachable" "Could not check release v$1: $(head -c 300 <<<"$out")"
}

# Highest released version (by version number – not GitHub's "latest" flag, which marks the newest release)
highest_release() {
    local tags
    tags="$(gh release list --limit 1000 --json tagName --jq '.[].tagName')" \
        || fail "GitHub API not reachable" "Could not list the releases."
    # Empty only when there really is no release yet – an unreadable list must never count as "first release"
    # (that would skip the downgrade check for :latest)
    [ -n "$tags" ] || return 0
    python3 tools/security_update.py highest <<<"$tags" \
        || fail "Release list unreadable" "No version number found among the releases: $(head -c 300 <<<"$tags")"
}

prepare_builder() {   # prepare_builder [multi] – buildx builder; "multi" also enables arm64 emulation
    if [ "${1:-}" = multi ]; then
        sudo apt-get update -qq >/dev/null && sudo apt-get install -y -qq qemu-user-static >/dev/null \
            || fail "Preparation failed" "Could not install qemu-user-static for arm64 builds."
    fi
    docker buildx create --name ci --driver docker-container --use >/dev/null
    docker buildx inspect --bootstrap >/dev/null
}

# Tests in exactly the given image, with the given checkout mounted (the same code that is in the image)
test_in_image() {   # test_in_image IMAGE SRC_DIR
    local rc=0
    docker run --rm --user "$(id -u):$(id -g)" --entrypoint python -e CATALYTIX_DATA_DIR=/tmp/catalytix-test \
        -v "$2:/src" -w /src/tests "$1" -m unittest discover -q > test-output.txt 2>&1 || rc=$?
    cat test-output.txt
    if [ "$rc" != 0 ]; then
        tail -n 40 test-output.txt
        fail "Tests failed in the image" "See the log of this step – nothing is published."
    fi
    { echo "### Tests in the image"; echo '```'; tail -n 3 test-output.txt; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
}

# Label of a published image (empty if it has none); registry errors stop the run
image_label() {   # image_label REF KEY
    local out
    out="$(docker buildx imagetools inspect "$1" --format '{{json .Image}}' 2>&1)" \
        || fail "Registry not reachable" "Could not read $1: $(head -c 300 <<<"$out")"
    python3 tools/security_update.py label "$2" <<<"$out" \
        || fail "Registry answer unreadable" "Could not read the labels of $1."
}

# 0 if :latest may be moved to VERSION: :latest does not exist yet, or its version is not higher. Guards against
# a downgrade when the build and the security update run close to each other (a release can lag behind the image).
latest_allows() {   # latest_allows VERSION
    local cur
    image_exists "$(image_name):latest" || return 0
    # image_label runs in a subshell: its "fail" ends only that – pass the error on and stop here (also when this
    # function is called in an "if", where set -e does not apply)
    cur="$(image_label "$(image_name):latest" org.opencontainers.image.version)" || { printf '%s\n' "$cur"; exit 1; }
    [ -n "$cur" ] || return 0
    [[ "$cur" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Registry answer unreadable" ":latest has the version label '$cur'."
    python3 tools/security_update.py is-highest "$1" "$cur"
}

docker_login() {   # needs TOKEN in the environment
    echo "$TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin >/dev/null \
        || fail "Login failed" "Could not log in to ghcr.io."
}

# Type check (mypy) in the pinned base image (ARG PYTHON_IMAGE – Python 3.13 like the finished image). Everything is
# installed with --require-hashes from the locks: pip (docker/pip.lock), the program's packages (requirements.txt)
# and mypy (tools/mypy.lock). The checkout is mounted read-only.
#   mypy.ini         strict modules – MUST pass, otherwise the run stops
#   mypy-report.ini  all modules – only listed in the step summary, never fails
run_mypy() {
    local image out rc
    image="$(sed -n 's/^ARG PYTHON_IMAGE=//p' Dockerfile)"
    [ -n "$image" ] || fail "Type check failed" "No ARG PYTHON_IMAGE in the Dockerfile."
    out="$(mktemp -d)"
    chmod 777 "$out"
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_NO_CACHE_DIR=1 \
        -v "$PWD:/src:ro" -v "$out:/out" -w /src --entrypoint sh "$image" -c '
            set -e
            python -m venv /tmp/venv
            . /tmp/venv/bin/activate
            pip install -q --require-hashes --no-deps -r docker/pip.lock
            pip install -q --require-hashes --no-deps --only-binary=:all: -r requirements.txt
            pip install -q --require-hashes --no-deps --only-binary=:all: -r tools/mypy.lock
            python -m mypy --version > /out/version.txt
            rc=0; python -m mypy --cache-dir /tmp/mypy-cache > /out/strict.txt 2>&1 || rc=$?
            echo "$rc" > /out/strict.rc
            python -m mypy --config-file mypy-report.ini --cache-dir /tmp/mypy-report > /out/report.txt 2>&1 || true
        ' || fail "Type check failed" "mypy could not be installed in $image – see the log of this step."
    rc="$(cat "$out/strict.rc")"
    cat "$out/strict.txt"
    {
        echo "### Type check ($(cat "$out/version.txt"))"
        echo "**Strict modules (mypy.ini):** $(tail -n 1 "$out/strict.txt")"
        echo
        echo "<details><summary>All modules (mypy-report.ini, for information only): $(tail -n 1 "$out/report.txt")</summary>"
        echo
        echo '```'
        grep ': error:' "$out/report.txt" | head -n 300 || true
        echo '```'
        echo "</details>"
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    if [ "$rc" != 0 ]; then
        grep ': error:' "$out/strict.txt" | head -n 50 | while IFS= read -r line; do
            file="${line%%:*}"; rest="${line#*:}"; lineno="${rest%%:*}"
            echo "::error file=$file,line=$lineno,title=mypy::${line#*error: }"
        done
        fail "Type check failed" "mypy found errors in the strictly checked modules (mypy.ini) – see the log of this step."
    fi
}
