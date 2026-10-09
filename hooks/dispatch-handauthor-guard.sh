#!/usr/bin/env bash
# PreToolUse (Write|Edit) guard: nudge toward `ollama-dispatch-draft` when about
# to HAND-AUTHOR a dispatch worktree's verify.test.ts. The scaffold/drafter write
# that file via their own Python I/O, so this only fires on a manual Write/Edit —
# i.e. exactly the "I forgot the automated drafter" mistake. Non-blocking.
input=$(cat)
python3 - "$input" <<'PY'
import json, sys, os
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
fp = (d.get("tool_input") or {}).get("file_path") or ""
if not fp.endswith("verify.test.ts"):
    sys.exit(0)
wt = os.path.dirname(fp)
# Only a dispatch worktree: has the scaffold's siblings.
if not (os.path.exists(os.path.join(wt, "TASK.md")) and os.path.exists(os.path.join(wt, "check_literals.py"))):
    sys.exit(0)
msg = (
    "Dispatch guard: you're hand-editing verify.test.ts in a dispatch worktree. "
    "The DEFAULT is to let a local model draft the cases:\n"
    f"  ~/bin/ollama-dispatch-draft {wt} --refimpl-cmd 'python3 {wt}/refimpl.py {wt}'\n"
    "then READ the drafts for relevance and re-run with --confirm. Hand-authoring "
    "is the fallback only when the drafter can't converge "
    "(memory: feedback_dispatch_use_automated_draft_flow)."
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}))
PY
exit 0
