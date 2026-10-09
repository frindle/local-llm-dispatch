#!/usr/bin/env bash
# Run the self-contained test-*.py suite against the repo copy, SANDBOXED.
# The tests expect to sit next to the scripts they test (as in ~/bin), so this builds an overlay
# (bin/ + tests/ + dashboard/src merged) under a throwaway fake $HOME and runs there. Nothing under
# the real ~/bin, ~/.ollama-dispatch or the live queue is read or written.
# Tests read frozen fixtures (tests/test-fixtures-live-artifacts) and a fake $HOME with installed-layout hooks; none read the live tree.
# Usage: ./run-tests.sh [-j N] [-t SECS] [name-glob ...]     (default: all except the SKIP list)
set -u
REPO="$(cd "$(dirname "$0")" && pwd)"
J=4; T=90
while getopts j:t: o; do case $o in j) J=$OPTARG;; t) T=$OPTARG;; esac; done; shift $((OPTIND-1))
# Heavier/side-effecting tests (spawn servers, kill process groups, hit hosts; test-selfcheck-hang runs ~20 min): run these by hand.
SKIP='test-pipeline-e2e|test-goose-queue-proxy|test-worker-openai-streaming|test-queue-gpu-exclusive|test-darkbloom-|test-verify-hang-groupkill|test-verify-orphan-reap|test-dispatch-worktree-reap-nvme|test-pipeline-watch|test-vault-callers|test-selfcheck-hang'
REAL_HOME="$HOME"
FH="$(mktemp -d "${TMPDIR:-/tmp}/lld-home.XXXXXX")"; OV="$FH/bin"; mkdir -p "$OV"
cp -R "$REPO"/bin/. "$OV"/; cp -R "$REPO"/tests/. "$OV"/; cp -R "$REPO"/dashboard/src/. "$OV"/
# ts-mutator needs its npm deps (not tracked): link the live ones read-only if present
[ -d "$REAL_HOME/bin/ts-mutator/node_modules" ] && ln -s "$REAL_HOME/bin/ts-mutator/node_modules" "$OV/ts-mutator/node_modules"
# dashboard tests expect ../src next to tests/ (as in the dashboard repo), so they get their own overlay + a ~/bin-style copy
mkdir -p "$FH/dash"; cp -R "$REPO"/dashboard/src "$REPO"/dashboard/tests "$FH/dash"/
for t in "$REPO"/dashboard/tests/test*.py; do cp "$t" "$OV"/; done
# minimal identity + config the tests expect (git commits in temp repos, default model)
printf '[user]\n\tname = test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n' > "$FH/.gitconfig"
mkdir -p "$FH/.ollama-dispatch"; cp "$REPO"/config/defaults.json "$REPO"/config/model-ladder.json "$FH/.ollama-dispatch"/
# installed-layout hooks (~/.claude/hooks) for the self-tests that run the real SessionStart hooks
mkdir -p "$FH/.claude/hooks"; cp "$REPO"/hooks/*.sh "$FH/.claude/hooks"/
export HOME="$FH" OLLAMA_QUEUE_NO_NOTIFY=1 PYTHONDONTWRITEBYTECODE=1
cd "$OV"
PAT=("$@"); [ ${#PAT[@]} -eq 0 ] && PAT=('test-*.py')
LIST=$(for p in "${PAT[@]}"; do ls $p 2>/dev/null; done | grep -vE "$SKIP" | sort -u)
run1() { f=$1; d=.; [ -f "$FH/dash/tests/$f" ] && d="$FH/dash/tests"; if (cd "$d" && timeout "$T" python3 "$f" >"$FH/out.$f" 2>&1); then echo "PASS $f"; else echo "FAIL($?) $f"; fi; }
export -f run1; export T FH
echo "$LIST" | xargs -P "$J" -I{} bash -c 'run1 {}' | sort | tee "$FH/summary.txt"
echo "passed=$(grep -c ^PASS "$FH/summary.txt") failed=$(grep -c ^FAIL "$FH/summary.txt")  (outputs: $FH/out.*)"
