#!/bin/sh
# Patch day? – the second Tuesday of the month (like Microsoft's Patch Tuesday).
#
# Security updates are published only then: once a month, predictable for admins, the same day in Catalytix,
# AssetLotse, Pocket Knife, SAMAUDIT and SAMonboarding. A cron line cannot say "second Tuesday" (day of month and
# weekday are OR-ed), so the security update workflow starts every Tuesday and this script decides.
#
#   patchday.sh              today (UTC)
#   patchday.sh 2026-11-10   a given day
#
# Exit code 0 = patch day, 1 = not patch day, 2 = bad argument. POSIX sh, no GNU date needed.
set -eu

day=${1:-$(date -u +%Y-%m-%d)}
case $day in
  [0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]) ;;
  *) echo "patchday.sh: expected YYYY-MM-DD, got '$day'" >&2; exit 2 ;;
esac
y=${day%%-*}
m=${day#*-}; m=${m%-*}; m=${m#0}
d=${day##*-}; d=${d#0}
if [ "$m" -lt 1 ] || [ "$m" -gt 12 ] || [ "$d" -lt 1 ] || [ "$d" -gt 31 ]; then
  echo "patchday.sh: no such date '$day'" >&2
  exit 2
fi

# weekday by Sakamoto's method: 0 = Sunday, 2 = Tuesday
case $m in
  1|5) t=0 ;; 2|6) t=3 ;; 3) t=2 ;; 4|7) t=5 ;; 8) t=1 ;; 9|12) t=4 ;; 10) t=6 ;; 11) t=2 ;;
esac
[ "$m" -lt 3 ] && y=$((y - 1))
w=$(( (y + y / 4 - y / 100 + y / 400 + t + d) % 7 ))

[ "$w" -eq 2 ] && [ "$d" -ge 8 ] && [ "$d" -le 14 ]
