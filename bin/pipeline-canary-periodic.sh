#!/bin/bash
# Periodic pipeline canary (proposed launchd job com.example.pipeline-canary; NOT installed
# until the owner approves). Runs ~/bin/pipeline-canary.py (sandboxed: stub models, temp
# HOME, no GPU, no real queue) only when a pipeline file changed since the last run,
# records the result in ~/.ollama-dispatch/canary/last.json (read by the session-start
# panel and `pipeline-canary.py --status`), and raises a macOS notification on FAIL.
set -u
LOG="$HOME/bin/logs/pipeline-canary.log"
mkdir -p "$(dirname "$LOG")"
st="$(python3 "$HOME/bin/pipeline-canary.py" --status 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && [ "${1:-}" != "--force" ]; then
  echo "$(date '+%F %T') skip: $st" >> "$LOG"
  exit 0
fi
out="$(nice -n 10 python3 "$HOME/bin/pipeline-canary.py" 2>&1)"; rc=$?
# Second landing path (a human `git merge` instead of --land-integration), only after
# a green default run, so a red default run's status is never overwritten by a green one.
if [ "$rc" -eq 0 ]; then
  out2="$(nice -n 10 python3 "$HOME/bin/pipeline-canary.py" --hand-land 2>&1)"; rc=$?
  out="$out
----- --hand-land
$out2"
fi
{ echo "===== $(date '+%F %T') rc=$rc"; printf '%s\n' "$out" | tail -40; } >> "$LOG"
if [ "$rc" -ne 0 ]; then
  msg="$(printf '%s\n' "$out" | grep -m3 'FAILED:' | tr '\n' ' ' | cut -c1-200)"
  osascript -e "display notification \"${msg//\"/\'}\" with title \"Pipeline canary FAILED\"" 2>/dev/null || true
fi
exit "$rc"
