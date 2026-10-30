#!/usr/bin/env python3
"""Standalone fixes for ollama-queue.py / ollama-worker.py.

Built as pure functions with no imports from either harness file so they can be
tested independently and pasted in by the harness owner. Each function names its
integration point. Nothing here edits the live files.

Covers handoff items 2 (resource sizing) and 3 (three known bugs).
"""
import math
import re

# ---------------------------------------------------------------------------
# ITEM 3a -- hf.co/ label sanitisation
# Integration: ollama-queue.py:683, the LIVE_LOG_DIR path build.
# Bug: a label derived from a model name like `hf.co/unsloth/Qwen3-30B:Q4_K_M`
# puts `/` into a filename -> the parent dir doesn't exist -> the queue CRASHES
# at enqueue for any hf.co model.
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[/:\\\s]+")

def safe_label(label, maxlen=80):
    """Filesystem-safe livelog component. Collapses / : \\ and whitespace to '-'."""
    s = _UNSAFE.sub("-", str(label or "job")).strip("-.")
    s = re.sub(r"-{2,}", "-", s)
    if not s or set(s) <= {"."}:
        s = "job"
    return s[:maxlen]


# ---------------------------------------------------------------------------
# ITEM 3b -- --capture-final-as never writes for a PAUSED run
# Integration: ollama-worker.py:3944 -- `and not paused_for_review`.
# The guard is deliberate ("a paused run's output is incomplete by definition")
# but the tradeoff is backwards: a partial answer that is SCORED AS PARTIAL beats
# an answer that is silently lost and scores as a missing cell. Observed live --
# research runs that paused on iteration budget had a complete final answer in
# their transcript and wrote no file.
# Fix: always capture; mark provenance instead of discarding.
# ---------------------------------------------------------------------------

def capture_decision(capture_target, file_exists, paused_reason, final_text):
    """Return (should_write, text_to_write, log_tag) -- or (False, None, reason).

    Replaces the `if capture_final_as and not paused_for_review:` condition.
    A paused run still captures, with a banner so the reader and any scorer can
    see the answer is partial rather than mistaking it for a clean finish.
    """
    if not capture_target:
        return (False, None, "no-target")
    if file_exists:
        return (False, None, "model-wrote-it")      # never clobber the model's file
    text = (final_text or "").strip()
    if not text:
        return (False, None, "no-final-text")
    if paused_reason:
        banner = (f"<!-- PARTIAL: run paused before completion ({paused_reason}). "
                  f"Captured from the final text answer. -->\n\n")
        return (True, banner + text, "capture=fallback-partial")
    return (True, text, "capture=fallback")


# ---------------------------------------------------------------------------
# ITEM 3c -- engine-health blindness on the SUCCESS path
# Integration: ollama-worker.py:1221-1225, the `if results:` branch.
# Bug: unresponsive_engines is only ever read on the EMPTY path. When 4 of 6
# engines are suspended but 2 still answer, the model gets a thin result set with
# no signal that coverage is degraded -- and treats "2 weak hits" as the whole
# web. This is the same failure class that burned an entire iteration budget,
# just with a non-empty result set hiding it.
# ---------------------------------------------------------------------------

def annotate_results(rendered, results, data, engines_queried):
    """Append a coverage warning to a SUCCESSFUL search when engines are down."""
    unresponsive = (data or {}).get("unresponsive_engines") or []
    if not unresponsive:
        return rendered
    names = sorted({(u[0] if isinstance(u, (list, tuple)) and u else str(u))
                    for u in unresponsive})
    live = max(0, engines_queried - len(names))
    return (rendered + f"\n\n[COVERAGE WARNING: {len(names)} of {engines_queried} search "
            f"engines are currently blocked or unresponsive ({', '.join(names)}). "
            f"These {len(results)} result(s) come from only {live} engine(s). "
            f"Absence of a result here is NOT evidence the thing does not exist -- "
            f"note the gap rather than concluding a negative.]")


# ---------------------------------------------------------------------------
# ITEM 2 -- resource sizing
# 2a: single source of truth for the iteration default.
# Bug: ollama-queue.py:1288 defaults --max-iters to 20 and ALWAYS passes it
# explicitly, so ollama-worker.py:72 DEFAULT_MAX_ITERS = 30 is dead code. Every
# queued job silently runs on 20 while the worker's own default says 30.
# Fix: queue default None -> omit the flag -> worker's constant actually governs.
# ---------------------------------------------------------------------------

WORKER_DEFAULT_MAX_ITERS = 30   # must mirror ollama-worker.py:72

def iters_flag(job_max_iters):
    """Argv fragment for --max-iters. Empty when unset, so the worker default wins."""
    if job_max_iters is None:
        return []
    return ["--max-iters", str(int(job_max_iters))]


def autocalc_iters(n_files=0, n_verify_checks=0, floor=None, cap=60):
    """Size the iteration budget to the shape of the task.

    floor + 5/file + 3/verify-check. Files dominate because each one costs at
    least a read and an edit; verify checks cost a run plus a fix round.
    """
    floor = WORKER_DEFAULT_MAX_ITERS if floor is None else floor
    return max(floor, min(cap, floor + 5 * max(0, n_files) + 3 * max(0, n_verify_checks)))


# 2b: context sizing from actual grounding bytes.
CTX_OVERHEAD_TOKENS = 4000       # system prompt, tool schemas, scratch
CTX_HEADROOM = 1.5               # room for the model's own output + tool results
_CTX_STEPS = [8192, 16384, 32768, 65536, 131072]

def autocalc_num_ctx(grounding_bytes, largest_file_bytes=0, ctx_cap=131072):
    """(num_ctx, refusal_or_None).

    Refuses when a SINGLE file cannot fit even alone -- that is a task-design
    error, and discovering it after a 40-minute run wastes the whole dispatch.
    Pre-flighting this is the standing instruction; this makes it mechanical.
    """
    need = int(grounding_bytes / 4 * CTX_HEADROOM) + CTX_OVERHEAD_TOKENS
    one_file = int(largest_file_bytes / 4 * CTX_HEADROOM) + CTX_OVERHEAD_TOKENS
    if one_file > ctx_cap:
        return (None, f"single file needs ~{one_file} tokens, over the {ctx_cap} cap -- "
                      f"split the task or narrow the read before dispatching")
    for step in _CTX_STEPS:
        if step >= need and step <= ctx_cap:
            return (step, None)
    if need > ctx_cap:
        return (None, f"grounding needs ~{need} tokens, over the {ctx_cap} cap -- "
                      f"split into {math.ceil(need / ctx_cap)} dispatches")
    return (ctx_cap, None)
