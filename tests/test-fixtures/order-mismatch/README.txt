Replay inputs for the smoke-order-mismatch situation (2026-10-09), copied READ-ONLY from
~/.ollama-dispatch/worktrees/wt-smoke-order-mismatch (base commit 5398b72):
  TASK.md refimpl.py verify.sh .dispatch-harness.json   the (untracked) harness, as authored
  base/                                                 `git show 5398b72:<path>` of the tracked files
The task's INTENT says "create lib/payoutMismatch.ts" but base/lib/payoutMismatch.ts is already a
real implementation. Used by test-spec-defect-gate.py (part B). Do not "fix" these files.
