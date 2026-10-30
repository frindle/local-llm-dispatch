#!/usr/bin/env python3
"""Regression tests for the 2026-09-19 slicer/auto state-drift audit.

Bug #9 -- a chain commit that FAILS was recorded as a DONE slice.
    execute() ran `git commit` with check=False, read `rev-parse HEAD`, and stamped
    DONE unconditionally. A pre-commit hook that exits non-zero (or a stale
    index.lock, or a chain worktree with no user.email) leaves HEAD exactly where it
    was -- so the run-state says "landed on the chain" while the chain branch has
    nothing, the NEXT slice is cut from the unadvanced tip and never sees the
    predecessor's code, and the closing "converged slices are commits on
    slice/<label>" message is false. _commit_deliverable() now PROVES the tip moved
    and restores the chain tree on failure.
    Red-on-revert: make _commit_deliverable always return ('ok', <sha>) -> the
    "failed commit is reported as an error" / "HEAD did not move" assertions fail.

Bug #10 -- BLOCKED was a one-way door.
    A slice goes BLOCKED when a dependency is FAILED/BLOCKED/ESCALATED. Every
    documented recovery (--retry-slice, --regate) eventually drives that dependency
    to DONE, but execute() had no `status == BLOCKED` branch, so the dependent
    matched NO branch at all and was silently skipped on every pass forever -- the
    unattended auto-feed chain just dropped the rest of the plan.
    Red-on-revert: delete the "BLOCKED and not any_dep_failed -> PENDING" block ->
    the revive assertion fails.

Bug #11 -- the FAILED self-heal disagreed with reconcile() about the same job.
    execute() healed a stale FAILED marker to PENDING for ANY live job, while
    reconcile() maps a live CODING job to ENQUEUED via _QUEUE_TO_SLICE. With
    PENDING + a live coding job, the next --execute after that job finishes takes
    the PENDING branch, "resumes" the already-authored harness and enqueues a SECOND
    coding dispatch on the same worktree; the first job's converged deliverable is
    never staged onto the chain.
    Red-on-revert: make heal_status_for_live_job always return PENDING -> the
    coding-label assertions fail.

Bug #12 (ollama-dispatch-auto) -- the enqueue job id was thrown away on a non-zero rc.
    `enqueue` can print `enqueued <id>` and still exit non-zero. dispatch_model
    tested rc first and reported the dispatch failed while the job it had just
    created was ALIVE and editing the worktree; do_auto then auto-sliced the label
    and started authoring again, racing two dispatches on one tree. (The slicer
    already fixed this same enqueue contract -- its Bug #5.)
    Red-on-revert: make enqueued_job_id return None whenever rc != 0 -> the
    "id survives a non-zero rc" assertion fails.

Run: python3 test-slice-chain-state-bugs.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def _load(path, modname):
    loader = SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    # HERMETIC (2026-10-06): never let a fixture reach the REAL queue. QUEUE ran
    # `ollama-queue.py status` / `plan-clear --group slice-alpha` against the live
    # daemon state on fake plans. python3 <devnull> is an empty script: exit 0, no rows.
    m.QUEUE = os.devnull
    return m


def _git(cwd, *a, check=True):
    return subprocess.run(["git", "-C", str(cwd), *a], check=check,
                          capture_output=True, text=True)


def _init_repo(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")


# --------------------------------------------------------------------------
# Bug #9 -- the chain commit must be PROVEN to land
# --------------------------------------------------------------------------
def test_commit_deliverable(m):
    print("Bug #9: a chain commit that does not land is NOT a DONE slice")
    with tempfile.TemporaryDirectory() as td:
        cwt = Path(td) / "chain"
        cwt.mkdir()
        _init_repo(cwt)
        tgt = "server.py"
        (cwt / tgt).write_text("def base():\n    return 0\n")
        _git(cwt, "add", tgt)
        _git(cwt, "commit", "-qm", "chain baseline")
        tip0 = _git(cwt, "rev-parse", "HEAD").stdout.strip()

        # --- happy path: a staged real change commits and the tip advances ----
        (cwt / tgt).write_text("def base():\n    return 0\n\ndef added():\n    return 1\n")
        _git(cwt, "add", tgt)
        outcome, sha = m._commit_deliverable(str(cwt), "s1", "add a thing", tgt)
        tip1 = _git(cwt, "rev-parse", "HEAD").stdout.strip()
        ok("a real staged deliverable commits -> 'ok'", outcome == "ok")
        ok("the returned sha IS the new tip", sha == tip1)
        ok("the chain tip actually advanced", tip1 != tip0)

        # --- a commit that FAILS (chain worktree with no usable identity) -----
        # (2026-10-02: chain commits now run hooks OFF -- the machine's post-commit
        # hook auto-pushed dispatch/slice branches to public repos -- so a rejecting
        # pre-commit hook no longer models a failed commit; a missing user.email,
        # one of the other documented causes, does.)
        _git(cwt, "config", "--unset", "user.email")
        _git(cwt, "config", "user.useConfigOnly", "true")
        _saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull   # no ~/.gitconfig identity fallback
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        (cwt / tgt).write_text("def base():\n    return 0\n\ndef added():\n    return 1\n"
                               "\ndef second():\n    return 2\n")
        _git(cwt, "add", tgt)
        try:
            outcome, msg = m._commit_deliverable(str(cwt), "s2", "second thing", tgt)
        finally:
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        tip2 = _git(cwt, "rev-parse", "HEAD").stdout.strip()
        ok("a rejected commit is reported as an error (NOT ok)", outcome == "error")
        ok("the chain tip did NOT move on a rejected commit", tip2 == tip1)
        ok("the error message says the commit did not land",
           "did NOT land" in msg)
        staged = _git(cwt, "diff", "--cached", "--name-only").stdout.strip()
        ok("nothing is left staged after a rejected commit (no leak into the "
           "NEXT slice's commit)", staged == "")
        ok("the chain worktree is restored to its tip",
           (cwt / tgt).read_text() == "def base():\n    return 0\n\ndef added():\n    return 1\n")


# --------------------------------------------------------------------------
# Bug #10 / #11 -- execute()'s state transitions
# --------------------------------------------------------------------------
def _mk_state(m, slices):
    return {"label": "alpha", "repo": "/nope", "target": "server.py", "lang": "python",
            "chain_branch": "slice/alpha", "chain_worktree": "/nope/chain",
            "dag_published": True, "order": [s for s in slices],
            "slices": slices}


def _patch_execute_env(m, saved, inflight=None):
    """Neutralise every side effect execute() has except the state transitions."""
    m.ensure_chain_worktree = lambda st: "/nope/chain"
    m.save_state = lambda st: None
    m.publish_dag = lambda st: []
    # BIT-ROT REPAIR (2026-09-20): the vacuous-must_contain gate landed AFTER these
    # fixtures were written and runs before every other branch, so it ESCALATED each
    # of them ("declares NO must_contain literals") and six assertions about the
    # BLOCKED / stale-FAILED transitions had been failing ever since -- silently, in
    # a test file nobody was running to green. These tests are about the transitions,
    # not the gate (the gate has its own coverage in --self-test), so neutralise it.
    m.vacuous_gate_reason = lambda *a, **k: None
    m.slice_job_inflight = lambda st, sid: inflight
    m.harness_authored = lambda wt: True
    # BIT-ROT REPAIR (2026-10-06): the resume-author path (harness_self_check_fail) landed after
    # these fixtures; on a fake worktree it reported 'not converged' and execute() went on to
    # authoring_intent() on an intent-less fixture slice (KeyError). These tests are about status
    # transitions, so the harness is declared self-checking.
    m.harness_self_check_fail = lambda wt: None
    m.clean_and_seal = lambda wt: None
    m.remove_worktree = lambda cwt, wt: None
    m._park_or_advance = lambda *a, **k: saved.setdefault("parked", a[1])


def test_blocked_revives(m):
    print("Bug #10: a BLOCKED slice revives once its dependency recovers")
    saved_fns = {k: getattr(m, k) for k in
                 ("ensure_chain_worktree", "save_state", "publish_dag", "vacuous_gate_reason",
                  "slice_job_inflight", "harness_authored", "clean_and_seal",
                  "remove_worktree", "_park_or_advance")}
    try:
        seen = {}
        _patch_execute_env(m, seen)
        slices = {
            "s1": {"status": m.DONE, "title": "one", "depends_on": [],
                   "worktree": "/wt/s1", "job_id": None},
            "s2": {"status": m.BLOCKED, "title": "two", "depends_on": ["s1"],
                   "worktree": "/wt/s2", "job_id": None},
        }
        st = _mk_state(m, slices)
        m.execute(st, "mdl", "studio", 20, 1200, "auto", 32768)
        ok("a BLOCKED slice whose dep is now DONE is revived, not skipped forever",
           slices["s2"]["status"] != m.BLOCKED)
        ok("...and execute() actually went on to work it (reached the author/gate step)",
           seen.get("parked") == "s2")

        # A slice whose dep is STILL failed must stay BLOCKED.
        slices2 = {
            "s1": {"status": m.FAILED, "title": "one", "depends_on": [],
                   "worktree": "/wt/s1", "job_id": None},
            "s2": {"status": m.BLOCKED, "title": "two", "depends_on": ["s1"],
                   "worktree": "/wt/s2", "job_id": None},
        }
        st2 = _mk_state(m, slices2)
        seen.clear()
        m.execute(st2, "mdl", "studio", 20, 1200, "auto", 32768)
        ok("a BLOCKED slice whose dep is still FAILED stays BLOCKED",
           slices2["s2"]["status"] == m.BLOCKED)
        # NARROWED (2026-09-20, bare-FAILED auto-heal): "nothing was authored at all"
        # is no longer the right assertion here, and it never was the CLAIM. s1 is
        # FAILED with no live job, which execute() now auto-retries -- so the pass
        # legitimately reaches the author step FOR s1. What must still hold is that
        # the BLOCKED dependent is not authored off a tip its dependency is missing.
        ok("...and the BLOCKED dependent is NOT handed to the author/gate step",
           seen.get("parked") != "s2")
        ok("(it is the FAILED dependency that gets the retry, not the dependent)",
           seen.get("parked") == "s1")
    finally:
        for k, v in saved_fns.items():
            setattr(m, k, v)


def test_failed_self_heal(m):
    print("Bug #11: the stale-FAILED self-heal agrees with reconcile()")
    ok("a live AUTHORING job means the slice is PENDING (no coding job yet)",
       m.heal_status_for_live_job("auto-author-alpha-s1") == m.PENDING)
    ok("a live CODING job means the slice is ENQUEUED (a deliverable is coming)",
       m.heal_status_for_live_job("alpha-s1") == m.ENQUEUED)
    ok("the heal agrees with reconcile()'s own queue->slice mapping for a live "
       "coding job",
       m.heal_status_for_live_job("alpha-s1") == m._QUEUE_TO_SLICE["running"])

    saved_fns = {k: getattr(m, k) for k in
                 ("ensure_chain_worktree", "save_state", "publish_dag", "vacuous_gate_reason",
                  "slice_job_inflight", "harness_authored", "clean_and_seal",
                  "remove_worktree", "_park_or_advance")}
    try:
        seen = {}
        _patch_execute_env(m, seen, inflight=("job77", "running", "alpha-s1"))
        slices = {"s1": {"status": m.FAILED, "title": "one", "depends_on": [],
                         "worktree": "/wt/s1", "job_id": None}}
        st = _mk_state(m, slices)
        m.execute(st, "mdl", "studio", 20, 1200, "auto", 32768)
        ok("a stale FAILED marker over a live CODING job heals to ENQUEUED, "
           "not PENDING (PENDING would re-author + double-enqueue)",
           slices["s1"]["status"] == m.ENQUEUED)
        ok("the live coding job id is recorded as the slice's job_id",
           slices["s1"].get("job_id") == "job77")
        ok("execute() does NOT launch a rival dispatch while it is live",
           "parked" not in seen)

        seen.clear()
        _patch_execute_env(m, seen, inflight=("job88", "running", "auto-author-alpha-s1"))
        slices2 = {"s1": {"status": m.FAILED, "title": "one", "depends_on": [],
                          "worktree": "/wt/s1", "job_id": None}}
        m.execute(_mk_state(m, slices2), "mdl", "studio", 20, 1200, "auto", 32768)
        ok("a stale FAILED marker over a live AUTHORING job still heals to PENDING",
           slices2["s1"]["status"] == m.PENDING)
        ok("...and the authoring id lands in author_job_id, never job_id "
           "(job_id is the coding deliverable's id)",
           slices2["s1"].get("author_job_id") == "job88"
           and slices2["s1"].get("job_id") is None)
    finally:
        for k, v in saved_fns.items():
            setattr(m, k, v)


# --------------------------------------------------------------------------
# Bug #12 -- ollama-dispatch-auto must not discard a created job id
# --------------------------------------------------------------------------
def test_enqueue_id(auto):
    print("Bug #12: an enqueue that prints an id but exits non-zero is NOT a failure")
    good = "enqueued 4e146d20f607  alpha-s1  model=qwen3.8:27b-q4_K_M host=studio\n"
    ok("the id is read on a clean enqueue",
       auto.enqueued_job_id(good, 0) == "4e146d20f607")
    ok("the id SURVIVES a non-zero rc (the job exists -- poll it, do not auto-slice)",
       auto.enqueued_job_id(good, 3) == "4e146d20f607")
    ok("a noisy clean enqueue still falls back to the loose hex scan",
       auto.enqueued_job_id("ok\njob 4e146d20f607 created\n", 0) == "4e146d20f607")
    refusal = ("[queue] ctx-gate REFUSED: estimated 71000 tok > ceiling; "
               "nothing enqueued (digest deadbeefcafe)\n")
    ok("a ctx-gate REFUSAL is NOT mistaken for a job id just because it "
       "contains hex", auto.enqueued_job_id(refusal, 2) is None)
    ok("`enqueued SPLIT` is never taken as a job id",
       auto.enqueued_job_id("enqueued SPLIT into 3 parts\n", 2) is None)
    ok("an empty failed enqueue is still a failure",
       auto.enqueued_job_id("", 1) is None)


def main():
    m = _load(BIN / "ollama-dispatch-slice", "oslice")
    auto = _load(BIN / "ollama-dispatch-auto", "oauto")
    test_commit_deliverable(m)
    test_blocked_revives(m)
    test_failed_self_heal(m)
    test_enqueue_id(auto)
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall slice/auto chain-state tests passed")


if __name__ == "__main__":
    main()
