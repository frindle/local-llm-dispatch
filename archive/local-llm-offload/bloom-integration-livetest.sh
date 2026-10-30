#!/bin/bash
# bloom-integration-livetest.sh -- controlled LIVE test of the queue <-> BloomGauge hand-off
# (bloom_control.py). Run ONLY at queue idle. Default is a PLAN (dry run): it shows every action
# it WOULD take and changes nothing. Pass --execute to run it for real.
#
#   bloom-integration-livetest.sh                 # show the plan + pre-flight, change nothing
#   bloom-integration-livetest.sh --execute       # hold -> verify -> (optional queue job) -> release -> verify
#   bloom-integration-livetest.sh --execute --with-queue-job
#                                                 # DAEMON-DRIVEN: you enqueue one tiny pair job; the DAEMON
#                                                 # fire the hooks (needs the restarted daemon); times
#                                                 # enqueue->pair-served and done->Automatic.
# Pre-flight refuses if any queue job is pending/running (the hold bounces the provider).
# To roll back at any time:  touch ~/.ollama-dispatch/bloom-control.disabled ; bloom_control.py release
# Never prints the Darkbloom key or Bloom tokens.
set -u
BIN="$HOME/bin"; BC="python3 $BIN/bloom_control.py"
EXEC=0; WITHJOB=0
for a in "$@"; do case "$a" in --execute) EXEC=1;; --with-queue-job) WITHJOB=1;; *) echo "unknown arg $a"; exit 2;; esac; done
say(){ printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
bloom_mode(){ curl -s -m5 http://127.0.0.1:8765/api/optimizer/control | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["automatic"]["mode"])' 2>/dev/null || echo unreachable; }
served(){ python3 - <<'PY'
import json,urllib.request
try:
    j=json.load(open(__import__("os").path.expanduser("~/.darkbloom/local.json")))
    b=str(j.get("base_url") or "http://127.0.0.1:8000").rstrip("/"); b=b[:-3] if b.endswith("/v1") else b
    r=urllib.request.Request(b+"/v1/models",headers={"Authorization":"Bearer "+str(j.get("api_key"))})
    print(",".join(sorted(m["id"] for m in json.load(urllib.request.urlopen(r,timeout=5))["data"])))
except Exception as e: print("unreadable")
PY
}
busy=$(python3 - <<'PY'
import json,os
try:
    d=json.load(open(os.path.expanduser("~/bin/ollama-queue-state.json")))
    print(sum(1 for j in d["jobs"] if j.get("status") in ("pending","running","paused") and not j.get("user_hold")))
except Exception: print(-1)
PY
)
say "PRE-FLIGHT: queue pending/running/paused rows = $busy ; BloomGauge mode = $(bloom_mode) ; served = $(served)"
$BC state | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  integration enabled=%s pair_ready=%s auto_update=%s bloom_err=%s"%(d["enabled"],d["pair_ready"],d["auto_update"],d["bloom_error"]))'
say "PLAN (what --execute does):"
say "  1. bloom_control hold : BloomGauge -> Manual, darkbloom autoupdate disable, provider.toml normalise,"
say "                          darkbloom start --model <pair> --idle-timeout 0 --local-endpoint --timeout 60; wait /health+/v1/models"
say "  2. verify             : mode=manual, served == pair, state phase=ready"
[ $WITHJOB = 1 ] && say "  3. enqueue one pair job; the DAEMON hook must hold (if not already) and the job must launch only after the pair is served"
say "  4. bloom_control release : release-pin + set-automatic true, autoupdate enable; verify mode=on"
say "  --- dry-run of the hold, for the exact commands:"
$BC hold --dry-run 2>&1 | sed 's/^/     /'
if [ $EXEC = 0 ]; then say "DRY RUN ONLY -- re-run with --execute to apply."; exit 0; fi
if [ "$busy" != "0" ]; then say "REFUSED: queue is not idle (rows=$busy). Run at idle."; exit 1; fi
[ -f "$HOME/.ollama-dispatch/bloom-control.disabled" ] && { say "REFUSED: bloom-control.disabled exists"; exit 1; }
if [ $WITHJOB = 1 ]; then
  say "DAEMON-DRIVEN mode: this script does NOT call hold/release. Enqueue ONE tiny pair job now (a 'reply ok' job"
  say "with your usual enqueue command, bundle'd); the restarted daemon must fire the hooks. Waiting up to 10 min for a row ..."
  rows(){ python3 -c 'import json,os;d=json.load(open(os.path.expanduser("~/bin/ollama-queue-state.json")));print(sum(1 for j in d["jobs"] if j.get("status") in ("pending","running","paused") and not j.get("user_hold")))'; }
  for i in $(seq 1 120); do [ "$(rows)" != 0 ] && break; sleep 5; done
  [ "$(rows)" = 0 ] && { say "no job appeared; nothing measured"; exit 1; }
  te=$(date +%s); say "T0 work visible (queue rows>0)"
  for i in $(seq 1 360); do s=$(served); [ "$s" = "Qwen3.5-9B,qwen3.6-35b-a3b-vl-mtp-mxfp8" ] || [ "$s" = "qwen3.6-35b-a3b-vl-mtp-mxfp8,Qwen3.5-9B" ] && break; sleep 1; done
  say "enqueue -> pair served: $(( $(date +%s) - te ))s (mode=$(bloom_mode); expect manual) ; served=$(served)"
  say "waiting for the queue to finish (max 30 min) ..."
  for i in $(seq 1 360); do [ "$(rows)" = 0 ] && break; sleep 5; done
  tdone=$(date +%s)
  for i in $(seq 1 120); do [ "$(bloom_mode)" = on ] && break; sleep 1; done
  say "queue idle -> BloomGauge Automatic: $(( $(date +%s) - tdone ))s (no timer expected: only API latency) ; mode=$(bloom_mode)"
else
  t0=$(date +%s)
  say "EXECUTE hold ..."; $BC hold || { say "HOLD FAILED"; exit 1; }
  t1=$(date +%s); say "hold took $((t1-t0))s ; mode=$(bloom_mode) ; served=$(served)"
  [ "$(bloom_mode)" = manual ] || say "WARN: BloomGauge not Manual (degraded?) -- see ~/.ollama-dispatch/bloom-control.log"
  t2=$(date +%s); say "EXECUTE release ..."; $BC release || say "RELEASE reported failure (see log; release-pending retries on the next daemon event)"
  say "release took $(( $(date +%s) - t2 ))s ; mode=$(bloom_mode)"
fi
say "FINAL: mode=$(bloom_mode) served=$(served) state=$($BC state | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["control"])')"
