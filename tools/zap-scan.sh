#!/bin/sh
# ZAP scan of the running program – part of the monthly code audit (same in all programs).
# Starts the image in a throwaway container with test data only and runs ZAP WITHOUT login: spider,
# passive and active scan (zap-full-scan.py) against this container only – never against real servers.
#   sh tools/zap-scan.sh <image> <report directory>
# Env: ZAP_IMAGE (required, pinned by digest), ZAP_MINUTES (active scan limit, default 25)
# Report: zap-report.html / .json / .md in the report directory, the alert list also in the log (between
# ZAP-ALERTS-BEGIN and ZAP-ALERTS-END) for the code audit. Findings do not fail – the audit evaluates them; a scan
# that could not run does. Known false positives: .zap/rules.tsv (IGNORE with reason).
set -eu
IMG="${1:?image}"
OUT="${2:?report directory}"
ZAP="${ZAP_IMAGE:?ZAP_IMAGE (pinned image) missing}"
MINUTES="${ZAP_MINUTES:-25}"
# Program specifics
APP_PORT=9300          # port inside the container
READY_PATH=/login       # answers once the program is up
NET="zap-net-$$"
APP="catalytix-zap-app"
HOST_PORT=18090
DATA="$(mktemp -d)"

cleanup() {
  docker rm -f "$APP" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() {
  # container log without lines that could carry initial passwords or setup codes
  echo "::error title=ZAP scan::$1 | $(docker logs "$APP" 2>&1 | grep -viE 'passw|kennwort|setup code|einrichtungscode|token' | tail -n 8 | tr '\n' '|')"
  exit 1
}

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"   # absolute: docker -v would take a relative name for a named volume
chmod 777 "$OUT" "$DATA"    # ZAP and the program write as their own users
[ ! -f .zap/rules.tsv ] || cp .zap/rules.tsv "$OUT/rules.tsv"
docker network create "$NET" >/dev/null
docker run -d --name "$APP" --network "$NET" -p "127.0.0.1:$HOST_PORT:$APP_PORT" \
  --security-opt no-new-privileges:true "$IMG" >/dev/null || fail "docker run"

# Wait until the program answers (HTTPS with its own certificate or HTTP)
scheme=""
i=0
while [ -z "$scheme" ]; do
  for s in https http; do
    code="$(curl -ks -o /dev/null -w '%{http_code}' --max-time 5 "$s://127.0.0.1:$HOST_PORT$READY_PATH" || true)"
    case "$code" in 2??|3??|401|403) scheme="$s"; break ;; esac
  done
  [ -n "$scheme" ] && break
  [ "$(docker inspect -f '{{.State.Status}}' "$APP")" = running ] || fail "container is no longer running"
  i=$((i + 1))
  [ "$i" -le 90 ] || fail "the program did not answer within 3 minutes"
  sleep 2
done
echo "Program answers on $scheme, port $APP_PORT"

# ZAP: spider (max. 5 min), passive and active scan (max. $MINUTES min), no login. Exit codes of
# zap-full-scan.py: 0 = no warnings, 1 = FAIL rules, 2 = warnings, 3 = scan error.
set -- zap-full-scan.py -t "$scheme://$APP:$APP_PORT" -m 5 -I \
  -r zap-report.html -J zap-report.json -w zap-report.md \
  -z "-config scanner.maxScanDurationInMins=$MINUTES"
[ ! -f "$OUT/rules.tsv" ] || set -- "$@" -c rules.tsv
rc=0
docker run --rm --network "$NET" -v "$OUT:/zap/wrk:rw" "$ZAP" "$@" || rc=$?
[ "$rc" -le 2 ] || fail "ZAP could not run the scan (exit code $rc)"
[ -s "$OUT/zap-report.json" ] || fail "ZAP wrote no report"
echo "ZAP image: $(docker image inspect -f '{{join .RepoDigests " "}}' "$ZAP" 2>/dev/null)"

# Summary: alerts per risk level (High/Medium/Low/Informational)
summary="$(jq -r '[.site[]?.alerts[]? | (.riskdesc | split(" ")[0])] | group_by(.) | map("\(.[0]): \(length)") | join(", ")' "$OUT/zap-report.json")"
echo "ZAP findings: ${summary:-none}"
# The full list in the log as well: the code audit reads it from there (artifact downloads are not always possible)
echo "::group::ZAP alerts"
echo "ZAP-ALERTS-BEGIN"
jq -r '.site[]?.alerts[]? | "\(.riskdesc | split(" ")[0])\t\(.pluginid)\t\(.name)\t\(.count)\t\([.instances[]?.uri] | unique | .[:5] | join(" "))"' "$OUT/zap-report.json"
echo "ZAP-ALERTS-END"
echo "::endgroup::"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### ZAP scan (no login): ${summary:-no findings}"
    echo
    jq -r '.site[]?.alerts[]? | "- **\(.riskdesc | split(" ")[0])** – \(.name) (rule \(.pluginid), \(.count) occurrences)"' "$OUT/zap-report.json"
  } >> "$GITHUB_STEP_SUMMARY"
fi
