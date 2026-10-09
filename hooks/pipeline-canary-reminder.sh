#!/bin/bash
# PostToolUse hook on Edit|Write (and Bash, via the file list below): when an agent
# edits a dispatch-pipeline tool in ~/bin, tell it to run the end-to-end canary.
#
# WHY (2026-10-06): the pipeline kept breaking at the SEAMS between tools on live
# runs, because each fix was tested only in its own file. ~/bin/pipeline-canary.py
# drives a real 3-slice plan through every tool against stub models in ~2.5 min;
# `pipeline-canary.py --status` reports SKIPPED while any pipeline file differs
# from the last green run. A nudge, not a block.
input="$(cat)"
path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null)"
[ -n "$path" ] || exit 0
case "$path" in
  "$HOME"/bin/*) ;;
  *) exit 0 ;;
esac
base="$(basename "$path")"
case "$base" in
  *.bak*|*.orig|*.tmp) exit 0 ;;
  ollama-queue.py|ollama-worker.py|darkbloom_chat.py|ollama-dispatch-auto|ollama-dispatch-scaffold|\
  ollama-dispatch-plan|ollama-dispatch-slice|ollama-dispatch-preflight|ollama-dispatch-draft|\
  gate-on-complete.py|dispatch-self-heal.py|code-review-agent.py|handoff-emit.py|\
  escalation_index_janitor.py|dispatch-escalation-watcher.py|verify-relevance.py|\
  dispatch-ack-reconcile.py|pipeline-canary.py) ;;
  *) exit 0 ;;
esac
jq -n --arg path "$path" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("PIPELINE CANARY: you edited dispatch-pipeline file " + $path + ". Before calling the change done, run: python3 ~/bin/pipeline-canary.py   (about 2.5 min, fully sandboxed: stub models, temp HOME, no real queue/GPU). It must end PIPELINE_CANARY_OK. If it fails, fix the root cause; do not skip it. Until a green run, `pipeline-canary.py --status` and the session-start panel report PIPELINE E2E: SKIPPED.")
  }
}'
