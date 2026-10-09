#!/usr/bin/env python3
"""Regression test for the check_refimpl try/finally fix (2026-09-23).

Root cause (aw-sched-runner s19-run-schedule escalation): check_refimpl() called
revert_refimpl() on each exit path individually, not under try/finally. An
exception raised between a successful apply_refimpl() and the revert call (e.g.
inside check_verify_relevance's mutation testing) strands the reference impl in
the worktree. refimpl.py's own idempotency assert then makes every future
check_refimpl self-seal: NO-GO forever, the slice never enqueued.

This proves the bug on the pre-fix source and proves the fix on the post-fix
source, by loading BOTH versions of ollama-dispatch-preflight (via git show
HEAD vs the working tree) and running the same "something raises mid-check"
scenario against each: revert_refimpl must be called in both outcomes, but
only the fixed version guarantees it.
"""
import importlib.machinery
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

REPO = Path("/Users/user/Desktop/GitHub Projects/machine-config")
TARGET = "bin/ollama-dispatch-preflight"


def load_from_source(src: str, modname: str):
    mod = types.ModuleType(modname)
    mod.__file__ = f"<{modname}>"
    exec(compile(src, f"<{modname}>", "exec"), mod.__dict__)
    return mod


PRE_FIX = (Path(__file__).resolve().parent / "test-fixtures" / "refimpl-tryfinally"
           / "ollama-dispatch-preflight.pre-a80ced7")


def get_head_source() -> str:
    """The PRE-FIX source. SELF-CONTAINED (2026-10-05): this used to read
    machine-config HEAD, which stopped being pre-fix the moment a80ced7 (the fix)
    was committed -- the suite then reported INCONCLUSIVE (rc=1) forever. The
    pre-fix file is vendored verbatim (git show a80ced7^:bin/ollama-dispatch-preflight)."""
    return PRE_FIX.read_text()


def get_worktree_source() -> str:
    # the LIVE gate (what actually runs), not a checkout that may lag it
    return (Path(__file__).resolve().parent / "ollama-dispatch-preflight").read_text()


def run_scenario(mod, label):
    """Build a Preflight instance, force an exception mid check_refimpl, and
    report whether revert_refimpl still ran."""
    args = SimpleNamespace(worktree="/tmp/does-not-need-to-exist-for-this-test",
                            no_refimpl=False, verify="irrelevant")
    pf = mod.Preflight(args)

    calls = {"revert": False, "check_reverted": False}

    pf.git = lambda *a, **k: (0, "", "")
    pf.sh = lambda *a, **k: (0, "VERIFY_OK")
    pf.apply_refimpl = lambda: (True, "ok")
    pf.must_contain_pairs = lambda: []  # skip refimpl_scope_text (needs real git)

    def boom():
        raise RuntimeError("simulated crash mid relevance check")

    pf.check_verify_relevance = boom

    def revert():
        calls["revert"] = True
    pf.revert_refimpl = revert

    def checked():
        calls["check_reverted"] = True
    pf.check_reverted = checked

    raised = False
    try:
        pf.check_refimpl()
    except RuntimeError:
        raised = True

    ok = raised and calls["revert"] and calls["check_reverted"]
    print(f"  [{label}] exception propagated={raised} "
          f"revert_refimpl called={calls['revert']} "
          f"check_reverted called={calls['check_reverted']} "
          f"-> {'OK' if ok else 'BUG: refimpl left stranded on crash'}")
    return ok


print("Loading HEAD (pre-fix) and working-tree (post-fix) versions of "
      f"{TARGET}...")
pre = load_from_source(get_head_source(), "preflight_pre")
post = load_from_source(get_worktree_source(), "preflight_post")

print("\nScenario: exception raised inside check_refimpl after a successful "
      "apply_refimpl(), before the revert.")
pre_ok = run_scenario(pre, "HEAD (before fix)")
post_ok = run_scenario(post, "working tree (after fix)")

print()
if post_ok and not pre_ok:
    print("PASS: HEAD reproduces the strand-on-crash bug; the working-tree fix "
          "closes it (revert_refimpl/check_reverted now run under finally).")
    sys.exit(0)
elif post_ok and pre_ok:
    print("INCONCLUSIVE: HEAD already reverted on crash -- this scenario does "
          "not discriminate old vs new (nothing to prove the fix bites).")
    sys.exit(1)
else:
    print("FAIL: the working tree still strands the refimpl on a mid-check "
          "exception.")
    sys.exit(1)
