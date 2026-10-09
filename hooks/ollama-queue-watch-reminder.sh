#!/bin/bash
# Re-arms the session-local ollama queue watcher (the Monitor tool) across
# sessions. The Monitor that notifies Claude when a queued ollama job
# finishes lives only as long as the session that armed it -- a /clear or a
# new session loses it, so completions go unnoticed. This hook fixes that
# structurally instead of relying on memory:
#   - SessionStart: if any ollama job is running/queued right now, tell the
#     fresh session to arm the watcher.
#   - PostToolUse(Bash): if the command just run was an `ollama-queue.py
#     enqueue`, tell the session to arm the watcher if it hasn't already.
# It only NUDGES (additionalContext) -- arming the Monitor is a tool call
# only the model can make; a hook cannot call it.
input="$(cat)"
event="$(printf '%s' "$input" | jq -r '.hook_event_name // empty')"

# DIAGNOSIS-ONLY HEADLESS SESSION -> self-skip (2026-09-24). This hook's whole payload
# is "arm the Monitor tool now", and a headless `claude -p` review spawned by
# dispatch-escalation-watcher.py runs with read-only --allowedTools and no one to
# approve a tool call, so it CANNOT comply -- it can only apologise about it in place
# of the diagnosis it was spawned to do. Read stdin first (above) so the caller never
# sees a broken pipe, then skip.
if [ -n "${CLAUDE_HEADLESS_DIAGNOSIS:-}" ]; then exit 0; fi

ARM_MSG='OLLAMA QUEUE WATCHER: ollama job(s) are in flight but this session has no queue watcher armed. Arm it now so you get pinged when they finish (covers success AND failure), unless you already have one running this session. Use the Monitor tool, persistent, with this command:
SEEN=$(mktemp); first=1; echo "queue watch armed"; while true; do python3 ~/bin/ollama-queue.py status 2>/dev/null | sed -nE '"'"'s/^\[([a-z]+)[[:space:]]*\][[:space:]]+([0-9a-f]{6,})[[:space:]]+([^[:space:]]+).*/\1 \2 \3/p'"'"' | while read -r st id label; do case "$st" in running|queued|pending|scheduled|held) : ;; *) if ! grep -qx "$id" "$SEEN" 2>/dev/null; then echo "$id" >> "$SEEN"; [ "$first" -eq 0 ] && echo "ollama job finished: $label [$id] -> $st"; fi ;; esac; done; first=0; sleep 30; done'

emit() {
  jq -Rn --arg ev "$1" --arg msg "$ARM_MSG" '{
    hookSpecificOutput: { hookEventName: $ev, additionalContext: $msg }
  }'
}

case "$event" in
  SessionStart)
    # Any non-terminal job? (running/queued/pending/scheduled/held)
    active="$(python3 ~/bin/ollama-queue.py status 2>/dev/null \
      | sed -nE 's/^\[([a-z]+)[[:space:]]*\].*/\1/p' \
      | grep -Ec '^(running|queued|pending|scheduled|held)$' 2>/dev/null)"
    [ "${active:-0}" -gt 0 ] 2>/dev/null && emit "SessionStart"
    ;;
  PostToolUse|PreToolUse)
    cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
    case "$cmd" in
      *ollama-queue.py*enqueue*) emit "$event" ;;
    esac
    ;;
esac
exit 0
