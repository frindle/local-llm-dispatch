#!/usr/bin/env bash
# Submit the 6 read-only qagent battery tasks (each is its own bundle via qagent's own convention).
# They queue behind whatever bundle is running -- expected.
# Usage: ./run-battery.sh [--dry-run]     env: QAGENT (default ~/bin/qagent), BATTERY_MAX_WALL (default 1500)
# Prints "<task-id> <job-id>" per task, then a ready-to-run grade-battery.py command; also saves
# the pairs to last-run.txt (next to this script).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
QAGENT="${QAGENT:-$HOME/bin/qagent}"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
OUT="$HERE/last-run.txt"; : > "$OUT.tmp"
python3 - "$HERE/tasks.json" <<'PY' | while IFS=$'\t' read -r tid prompt; do
import json, os, sys
for t in json.load(open(sys.argv[1]))["tasks"]:
    print(t["id"] + "\t" + t["prompt"].replace("$HOME", os.path.expanduser("~")).replace("\n", " "))
PY
  if [ -n "$DRY" ]; then
    echo "# $tid: $QAGENT run --readonly --max-wall ${BATTERY_MAX_WALL:-1500} --label bat-$tid --dry-run \"${prompt:0:70}...\"" >&2
    "$QAGENT" run --readonly --max-wall "${BATTERY_MAX_WALL:-1500}" --label "bat-$tid" --dry-run "$prompt" >/dev/null
    jid="DRYRUN-$tid"
  else
    jid="$("$QAGENT" run --readonly --max-wall "${BATTERY_MAX_WALL:-1500}" --label "bat-$tid" "$prompt" | tail -1)"
  fi
  echo "$tid $jid" | tee -a "$OUT.tmp"
done
IDS="$(awk '{printf "%s ", $2}' "$OUT.tmp")"
if [ -n "$DRY" ]; then rm -f "$OUT.tmp"; else mv "$OUT.tmp" "$OUT"; fi
echo "grade with: python3 \"$HERE/grade-battery.py\" $IDS"
