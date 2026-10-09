#!/usr/bin/env bash
# PreToolUse (Bash) HARD GATE: block hand-rolling a dispatch worktree.
#
# The owner's rule (2026-09-12): "how do we stop doing that?" -- the recurring reflex
# is to `git worktree add` a scratch worktree by hand before a dispatch, instead
# of letting `ollama-dispatch-scaffold --repo <path>` cut + isolate it (which also
# emits TASK.md/verify.sh/fixtures/refimpl/check_literals). Advisory memories have
# not changed the reflex, so this BLOCKS the manual `git worktree add` at its start.
#
# Why this is safe:
#   - ollama-dispatch-scaffold creates its worktree via a subprocess git call,
#     which does NOT pass through the Bash TOOL, so the scaffold is unaffected.
#   - The harness's own EnterWorktree tool and Agent isolation:"worktree" are
#     separate tools, not `git worktree add`, so general isolation still works.
#   - Only the literal, hand-typed `git worktree add` is denied.
#
# Bypass (rare, legitimate manual worktree): `touch ~/.claude/.worktree-manual`
# (also honours the shared ~/.claude/.qwen-manual token); rm it when done.
input=$(cat)
python3 - "$input" <<'PY'
import json, os, re, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
if (d.get("tool_name") or "") != "Bash":
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""

# Bypass tokens.
for tok in ("~/.claude/.worktree-manual", "~/.claude/.qwen-manual"):
    if os.path.exists(os.path.expanduser(tok)):
        sys.exit(0)

# Only fire on a hand-typed `git worktree add`.
if not re.search(r"\bgit\b[^\n|;&]*\bworktree\s+add\b", cmd):
    sys.exit(0)

# Allow worktree management that is NOT creation (remove/prune/list/move/repair).
# (Those don't contain "worktree add", so they already fell through above.)

reason = (
    "DISPATCH WORKTREE GATE (hard): don't hand-roll a dispatch worktree with "
    "`git worktree add`. The scaffold creates + isolates it for you (and emits "
    "TASK.md / verify.sh / fixtures / refimpl / check_literals in one step):\n\n"
    "  ~/bin/ollama-dispatch-scaffold --label <name> --repo <repo path> \\\n"
    "    --lang <py|ts|js|swift> --target <the one file> \\\n"
    "    --entry-point <file:line> --defect '<confirmed symptom>' --property '<what must hold>'\n\n"
    "Then follow the printed NEXT steps (freeze-literals -> refimpl -> seal-baseline "
    "-> preflight -> enqueue), or just run the /ollama-dispatch skill.\n\n"
    "If you genuinely need a manual worktree (not for a dispatch):\n"
    "  - prefer the EnterWorktree tool or Agent isolation:\"worktree\"; or\n"
    "  - touch ~/.claude/.worktree-manual   # then retry; rm it when done.\n"
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
PY
exit 0
