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
    tags="$(gh release list --limit 200 --json tagName --jq '.[].tagName')" \
        || fail "GitHub API not reachable" "Could not list the releases."
    python3 tools/security_update.py highest <<<"$tags" || true
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

docker_login() {   # needs TOKEN in the environment
    echo "$TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin >/dev/null \
        || fail "Login failed" "Could not log in to ghcr.io."
}
