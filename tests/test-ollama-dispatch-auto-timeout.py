#!/usr/bin/env python3
"""Guard test for the ollama-dispatch-auto authoring-poll TIMEOUT RACE fix.

THE BUG: dispatch_model() polled with a wall-clock `deadline = now + a.timeout`
and returned "job ... did not finish within {timeout}s" once the clock passed,
EVEN THOUGH the queue job was still running and about to succeed. That hard-
failed voicemail-ui s4-apilist (the job finished + gated PASS-with-concerns, but
the orchestrator had already given up at 2400s).

THE FIX: poll to a TERMINAL job state -- the queue decides done/failed, not a
stopwatch. --timeout is now a SOFT warn threshold; a still-running job is never
failed on the clock. Only a terminal-bad status, or the row vanishing for >300s,
is a failure.

This test drives the REAL dispatch_model() with a scripted job_row + a fake
clock that jumps 1000s per poll, so the soft threshold is blown past on the very
first poll. Cases:
  A. job completes AFTER the soft cap is exceeded  -> NEW code: success.
     Revert-test: the OLD deadline loop (reimplemented here) on the SAME inputs
     returns "did not finish" -> proves the guard bites.
  B. job reaches a terminal-BAD status             -> failure (preserved).
  C. row vanishes and stays gone past the grace    -> failure (stuck worker).
"""
import importlib.util
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

AUTO = Path.home() / "bin" / "ollama-dispatch-auto"

# ollama-dispatch-auto has no .py extension, so give importlib an explicit
# source loader instead of relying on suffix-based detection.
loader = SourceFileLoader("auto_mod", str(AUTO))
spec = importlib.util.spec_from_loader("auto_mod", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class FakeTime:
    """Monotonic fake clock; each sleep() jumps the clock far forward so any
    wall-clock threshold is crossed almost immediately."""
    def __init__(self, start=1000.0, step=1000.0):
        self.t = start
        self.step = step

    def time(self):
        return self.t

    def sleep(self, _s):
        self.t += self.step


def _args(timeout):
    return types.SimpleNamespace(
        drafter_cmd=None, model="m", host="h", num_ctx=8192,
        author_max_iters=30, timeout=timeout,
        # Added 2026-09-19: dispatch_model grew --max-tokens and --lang after this
        # fixture was written, so the SimpleNamespace stopped standing in for the
        # real argparse namespace and every case in this file died on AttributeError
        # instead of testing the poll loop. (Bit-rotted stub, not a source bug --
        # the real caller always comes from argparse.)
        max_tokens=None, lang="python",
    )


def _install(status_sequence):
    """Patch the module's time, capture (enqueue), and job_row. job_row yields
    the next status in status_sequence on each call; a None entry simulates the
    row being absent. The last entry repeats once exhausted."""
    fake = FakeTime()
    mod.time = fake

    def fake_capture(cmd, cwd=None, timeout=None):
        return (0, "queued 0123456789ab\n", "")
    mod.capture = fake_capture

    seq = list(status_sequence)
    calls = {"i": 0}

    def fake_job_row(job):
        i = calls["i"]
        calls["i"] += 1
        st = seq[i] if i < len(seq) else seq[-1]
        if st is None:
            return None
        return {"status": st, "log_path": "", "exit_code": 0}
    mod.job_row = fake_job_row
    return fake


# ---- The OLD (buggy) loop, reimplemented verbatim for the revert-test -------
def old_deadline_loop(a, job="0123456789ab"):
    deadline = mod.time.time() + a.timeout
    last = None
    while mod.time.time() < deadline:
        mod.time.sleep(15)
        row = mod.job_row(job)
        if not row:
            continue
        st = row.get("status")
        if st in ("done", "failed", "cancelled"):
            if st != "done":
                return False, f"job {job} {st}"
            return True, f"job {job} done"
    return False, f"job {job} did not finish within {a.timeout}s"


def run():
    fails = 0

    # ---- Case A: completes long after the soft cap -------------------------
    _install(["running", "running", "running", "done"])
    ok, why = mod.dispatch_model(Path(tempfile.mkdtemp()), "prompt", "lbl",
                                 "bash verify.sh", _args(timeout=5))
    if not (ok and "done" in why):
        print(f"  FAIL A(new): expected success past soft cap, got {ok!r} {why!r}")
        fails += 1
    else:
        print("  PASS A(new): still-running job past soft cap -> polled to done")

    # Revert-test: the OLD loop on the SAME scripted inputs must bail.
    _install(["running", "running", "running", "done"])
    ok_old, why_old = old_deadline_loop(_args(timeout=5))
    if ok_old or "did not finish" not in why_old:
        print(f"  FAIL A(old): revert-test did not reproduce the bug -> "
              f"{ok_old!r} {why_old!r} (guard proves nothing)")
        fails += 1
    else:
        print("  PASS A(old): revert-test reproduces the bug (did-not-finish) "
              "-> guard bites")

    # ---- Case B: terminal-bad status is still a failure --------------------
    _install(["running", "failed"])
    ok, why = mod.dispatch_model(Path(tempfile.mkdtemp()), "prompt", "lbl",
                                 "bash verify.sh", _args(timeout=5))
    if ok or "failed" not in why:
        print(f"  FAIL B: expected failure on terminal 'failed', got {ok!r} {why!r}")
        fails += 1
    else:
        print("  PASS B: terminal-bad status -> failure (preserved)")

    # ---- Case C: row vanishes and stays gone past the grace ----------------
    _install([None])  # always absent; clock jumps 1000s/poll -> >300s fast
    ok, why = mod.dispatch_model(Path(tempfile.mkdtemp()), "prompt", "lbl",
                                 "bash verify.sh", _args(timeout=5))
    if ok or "vanished" not in why:
        print(f"  FAIL C: expected 'vanished' failure, got {ok!r} {why!r}")
        fails += 1
    else:
        print("  PASS C: vanished row past grace -> failure")

    print()
    if fails:
        print(f"AUTO_TIMEOUT_GUARD_FAIL: {fails} case(s) failed")
        return 1
    print("AUTO_TIMEOUT_GUARD_OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
