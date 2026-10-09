#!/usr/bin/env bash
# One-way refresh: copy LIVE files listed in MANIFEST.txt into this repo (live -> repo only; never writes to
# ~/bin, ~/.claude, ~/Library or ~/.ollama-dispatch). Review with `git diff` afterwards, then commit.
# Usage: ./SYNC.sh [--dry-run]
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"; cd "$REPO"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
EXCL=(--exclude='.git' --exclude='.venv' --exclude='.pytest_cache' --exclude='*.bak*' --exclude='*.orig' --exclude='__pycache__'
      --exclude='node_modules' --exclude='.DS_Store' --exclude='*.log' --exclude='*.pyc' --exclude='graphify-out' --exclude='.env*' --exclude='*.lock')
miss=0
sync_one() {  # $1=dest dir, $2=resolved live source
  local dest="$1" src="$2" tgt
  if [ ! -e "$src" ]; then echo "MISSING: $src" >&2; miss=$((miss+1)); return; fi
  mkdir -p "$dest"
  if [ -d "$src" ]; then
    case "$dest/$(basename "$src")" in harness/ollama-dispatch-pkg/*|harness/agent-dispatch/*) tgt="$dest/";; *) tgt="$dest/$(basename "$src")/";; esac
    [ "$dest" = harness/ollama-dispatch-pkg ] && tgt="$dest/"
    [ "$dest" = harness/agent-dispatch ] && tgt="$dest/" && EXTRA=(--exclude=results --exclude=fixtures --exclude=config.toml) || EXTRA=()
    mkdir -p "$tgt"; rsync -a $DRY "${EXCL[@]}" "${EXTRA[@]}" "$src/" "$tgt"
  else
    rsync -aL $DRY "$src" "$dest/$(basename "$src")"   # -L: resolve symlinks (e.g. ~/bin symlinks into the dashboard repo)
  fi
}
while IFS='|' read -r dest src; do
  case "$dest" in ''|'#'*) continue;; esac
  src="${src/#\~/$HOME}"; src="${src%% (*}"          # strip trailing "(note)" annotations
  case "$src" in
    *[\*\?]*)  # glob in MANIFEST (keeps owner-specific live names out of the tracked tree; scrub.sh renames the copies)
      found=0; for s in $src; do [ -e "$s" ] || continue; found=1; sync_one "$dest" "$s"; done
      [ $found -eq 1 ] || { echo "MISSING: $src" >&2; miss=$((miss+1)); } ;;
    *) sync_one "$dest" "$src" ;;
  esac
done < MANIFEST.txt
[ -z "$DRY" ] && "$REPO/scrub.sh" "$REPO"   # personal data -> generic placeholders (names, labels, LAN IPs, hosts); renames glob-synced files
# Guard: refuse to leave obvious secrets in the tree.
if grep -rIlE -e '-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{32,}' --exclude-dir=.git . | grep -vE '^./tests/(test-dispatch-diagnostics|test-failure-ledger).py$'; then
  echo "SECRET-LIKE CONTENT FOUND (listed above) - do not commit" >&2; exit 2; fi
[ $miss -eq 0 ] || echo "WARNING: $miss manifest source(s) missing" >&2
echo "sync done${DRY:+ (dry run)}"; git status --short | head -20
