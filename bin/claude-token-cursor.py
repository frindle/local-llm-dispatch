#!/usr/bin/env python3
"""Print the cumulative output-token count for the current Claude Code
session, up to right now. Used to compute a clean before/after delta around
a piece of work (e.g. prepping an Ollama dispatch) -- run once before, once
after, subtract.

Deliberately sums `output_tokens` only (which already includes thinking
tokens, per Claude Code's transcript format), not input/cache tokens. The
comparison this exists for is "Claude's own generated tokens for
prep/orchestration" vs "the local model's own generated (completion) tokens
for the actual work" -- both sides of that comparison are a model's own
output, which is what makes them comparable. Input/cache-read tokens mostly
reflect how much context has accumulated in the conversation, not the cost
of the specific work being measured.

Usage: claude-token-cursor.py
  Prints the raw cumulative total. Original two-call (before/after,
  subtract yourself) usage -- kept for manual use, but see --since-checkpoint
  below for why this stopped being the recommended mode.

Usage: claude-token-cursor.py --since-checkpoint
  Added 2026-08-28 after the two-call mode went unused all night despite
  existing -- the manual "run it once before you start, remember to run it
  again right before dispatching" flow requires remembering a step days
  apart in the middle of unrelated work, and in practice never happened even
  once on a real dispatch. Prints the delta since the LAST time this mode
  was called (in this session), THEN updates the checkpoint to the current
  total -- so there's only one call to make, right before a dispatch, and it
  auto-computes "tokens since the last checkpoint" with no separate "start"
  step to forget. Meant to be called from a PreToolUse hook that auto-injects
  the result as --claude-prep-tokens, not manually -- see
  ~/.claude/hooks/ollama-prep-tokens-inject.sh.
  Checkpoint is stored per-session at
  ~/.claude/ollama-worker-token-checkpoints/<session-id>.txt so concurrent
  sessions never clobber each other's state. First call in a session (no
  checkpoint file yet) reports a delta of 0 (nothing to compare against yet)
  rather than the full session total, which would badly overcount "prep for
  this one dispatch" as "everything since the session started."

Relies on $CLAUDE_CODE_SESSION_ID (set by the Claude Code harness) and the
current working directory to locate the session's transcript under
~/.claude/projects/<slugified-cwd>/<session-id>.jsonl -- same layout Claude
Code itself uses. Prints a single integer to stdout, or "0" if the
transcript can't be found (never raises -- a missing session shouldn't
break whatever script is computing a delta around this)."""
import json
import os
import sys
from pathlib import Path

CHECKPOINT_DIR = Path.home() / ".claude" / "ollama-worker-token-checkpoints"


def find_transcript() -> Path | None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    cwd_slug = str(Path.cwd()).replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / cwd_slug / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Fall back to a search, in case cwd has drifted from the session's
    # original project dir since it started.
    for hit in (Path.home() / ".claude" / "projects").rglob(f"{session_id}.jsonl"):
        return hit
    return None


def cumulative_total() -> int:
    path = find_transcript()
    if path is None:
        return 0
    total = 0
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                usage = msg.get("usage") or {}
                total += usage.get("output_tokens") or 0
    return total


def main():
    since_checkpoint = "--since-checkpoint" in sys.argv[1:]
    total = cumulative_total()

    if not since_checkpoint:
        print(total)
        return

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown-session"
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = CHECKPOINT_DIR / f"{session_id}.txt"
    try:
        previous = int(checkpoint_path.read_text().strip())
    except Exception:
        previous = total  # first call this session: delta is 0, not the whole session so far
    delta = max(0, total - previous)
    checkpoint_path.write_text(str(total))
    print(delta)


if __name__ == "__main__":
    main()
