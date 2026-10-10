#!/usr/bin/env python3
"""Regression tests for the 2026-09-20 bare-FAILED gap in execute().

THE BUG. execute()'s slice loop had a self-heal for a STALE FAILED marker -- a
slice recorded FAILED while a fresh dispatch is actually live in the queue under
its label -- and nothing at all for the ordinary case: FAILED with NO live job.
Walk the branches: `status == PENDING`, `status == BLOCKED and not
any_dep_failed`, AWAITING_REVIEW, CONFIRMED, ESCALATED, ENQUEUED. A bare FAILED
matches none of them, and chain_may_pass(FAILED) is True, so the loop fell out of
the bottom and stepped over it -- on that pass and on every pass after it,
forever. The slice sat FAILED with a dirty worktree (the target half-edited by
the losing run, plus the scaffold files) and the ONLY recovery was a human typing
`--retry-slice <sid>` by hand.

Seen live (2026-09-20): the broker_guard/playwright_checks.py chain, slices
s1-build-site-checks and s3-run-playwright-checks, both FAILED with no live job
and an untouched dirty worktree across many --execute passes.

THE FIX. execute() now does automatically what the human does: clear the losing
attempt's worktree and reset to PENDING, so the SAME pass re-authors from a clean
baseline. It is bounded by the EXISTING retry-storm budget rather than a second,
parallel one: the identical-signature streak (MAX_IDENTICAL_AUTHOR_FAILURES) and
the total-attempts cap (MAX_AUTHOR_ATTEMPTS), extracted out of the PENDING branch
into bound_stale_worktree_retry() so both callers share it. A slice that keeps
failing the same way ESCALATES to the Claude gate instead of looping.

REVERT-TEST. test_revert_proof() physically excises the `if status == FAILED:`
auto-heal block from a COPY of the script, loads that copy, and re-runs the exact
same scenario: the slice must stay FAILED, its worktree must never be cleared,
and execute() must never reach the authoring step. That is the bug reproducing on
demand, which is also what proves the assertions above are not tautologies.

Run: python3 test-slice-failed-autoheal.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SCRIPT = BIN / "ollama-dispatch-slice"
failures = []

STUB_SIG = "FAIL: TASK.md still has TODO placeholders -- fill them in"


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


def _mk_state(m, slices):
    return {"label": "alpha", "repo": "/nope", "target": "server.py", "lang": "python",
            "chain_branch": "slice/alpha", "chain_worktree": "/nope/chain",
            "dag_published": True, "order": list(slices), "slices": slices}


def _slice(m, status, sid="s1", depends_on=(), **kw):
    s = {"status": status, "title": f"the {sid} slice", "intent": "add a thing",
         "depends_on": list(depends_on), "worktree": f"/wt/{sid}", "job_id": None,
         "must_contain": ["def added("],
         # Phase 2: the FIRST cap re-specs (tested in test-slice-spec-satisfiability.py);
         # these scenarios exercise the park, i.e. the re-spec budget already spent.
         "respec_count": getattr(m, "RESPEC_CAP", 1)}
    s.update(kw)
    return s


def _patch(m, seen, inflight=None, sig=STUB_SIG, authored=False):
    """Neutralise every side effect of execute() except the state transitions, and
    RECORD the two that matter: the worktree clear, and reaching the author step."""
    m.ensure_chain_worktree = lambda st: "/nope/chain"
    m.save_state = lambda st: None
    m.publish_dag = lambda st: []
    m.vacuous_gate_reason = lambda *a, **k: None
    m.slice_job_inflight = lambda st, sid: inflight
    m.harness_authored = lambda wt: authored
    m.author_failure_signature = lambda wt: sig
    m.clean_and_seal = lambda wt: None
    m.remove_worktree = lambda cwt, wt: seen.setdefault("removed", []).append(wt)
    # stdout/stderr too: audit_baseline_green_claim() reads cp.stdout, so a stub
    # carrying only returncode made this whole guard test die on an AttributeError
    # instead of asserting anything (silently red since the audit was added).
    m.run = lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stdout="VERIFY_OK\n", stderr="")
    m.run_tee = m.run          # execute() launches AUTO via run_tee (keeps its output)
    m._park_or_advance = lambda *a, **k: seen.setdefault("parked", a[1])
    m.Path = _FakePath(m)
    # the green-at-baseline AUDIT re-runs verify.sh for real (pgrun, not m.run) in
    # the worktree; these scratch /wt/ paths are not on disk, and this suite is about
    # ROUTING, so the audit confirms the claim (it has its own tests).
    m.audit_baseline_green_claim = lambda wt: True


class _FakePath:
    """Path(wt).exists() must be True for a scratch worktree path that is not on
    disk; everything else in execute() defers to the real Path."""
    def __init__(self, m):
        self._real = Path

    def __call__(self, p):
        if str(p).startswith("/wt/"):
            return _FakeWtPath(str(p))
        return self._real(p)


class _FakeWtPath:
    """A scratch worktree path: exists, but its files do not (reading one raises
    FileNotFoundError, like a real missing file) -- execute()'s audit helpers do
    `Path(wt) / ".dispatch-harness.json"` (the old bare SimpleNamespace made the
    whole suite die on a TypeError instead of asserting anything)."""
    def __init__(self, p):
        self._p = p

    def exists(self):
        return True

    def is_dir(self):
        return "/." not in self._p and not self._p.endswith((".json", ".md", ".sh", ".py"))

    def __truediv__(self, name):
        return _FakeWtPath(f"{self._p}/{name}")

    def is_file(self):
        return False

    def read_text(self, *a, **k):
        raise FileNotFoundError(self._p)

    def __str__(self):
        return self._p

    __fspath__ = __str__


_PATCHED = ("ensure_chain_worktree", "save_state", "publish_dag", "vacuous_gate_reason",
            "slice_job_inflight", "harness_authored", "author_failure_signature",
            "clean_and_seal", "remove_worktree", "run", "run_tee", "_park_or_advance",
            "Path", "audit_baseline_green_claim")


def run_scenario(m, slices, inflight=None, sig=STUB_SIG, authored=False, seen=None):
    saved = {k: getattr(m, k) for k in _PATCHED}
    seen = {} if seen is None else seen
    try:
        _patch(m, seen, inflight=inflight, sig=sig, authored=authored)
        m.execute(_mk_state(m, slices), "mdl", "studio", 20, 1200, "auto", 32768)
    finally:
        for k, v in saved.items():
            setattr(m, k, v)
    return seen


# --------------------------------------------------------------------------
# THE BUG: a bare FAILED slice must be auto-retried, not stepped over forever
# --------------------------------------------------------------------------
def test_bare_failed_auto_heals(m):
    print("a FAILED slice with NO live job is auto-retried (was: skipped forever)")
    slices = {"s1": _slice(m, m.FAILED)}
    seen = run_scenario(m, slices)
    ok("it does not stay FAILED", slices["s1"]["status"] != m.FAILED)
    ok("...it is reset to PENDING (same transition --retry-slice makes)",
       slices["s1"]["status"] == m.PENDING)
    ok("the losing attempt's dirty worktree is cleared (retry_slice's own plumbing)",
       seen.get("removed") == ["/wt/s1"])
    ok("...and the SAME --execute pass goes on to re-author it",
       seen.get("parked") == "s1")
    ok("the attempt is COUNTED, so the retry cannot be unbounded",
       slices["s1"].get("author_attempts") == 1)


def test_bound_escalates(m):
    print("the auto-retry is bounded by the EXISTING retry-storm budget")
    s = _slice(m, m.FAILED)
    slices = {"s1": s}
    run_scenario(m, slices)
    ok("first bare-FAILED pass retries (streak 1 of "
       f"{m.MAX_IDENTICAL_AUTHOR_FAILURES})", s["status"] == m.PENDING)
    # The re-authored dispatch fails the same way again -> FAILED once more.
    s["status"] = m.FAILED
    seen2 = run_scenario(m, slices)
    ok("a SECOND identical failure ESCALATES to the Claude gate, it does not retry",
       s["status"] == m.ESCALATED)
    ok("...and does NOT re-author (no dispatch is burned)", "parked" not in seen2)
    ok("...and leaves the worktree INTACT as evidence (nothing cleared)",
       "removed" not in seen2)
    ok("the escalation records the identical failure it escalated on",
       STUB_SIG in s.get("escalation_reason", ""))

    # A DIFFERENT failure reason means the loop is still moving: it must NOT
    # escalate on the streak -- only the total-attempts cap may stop it.
    s2 = _slice(m, m.FAILED, sid="s2")
    for i in range(2):
        s2["status"] = m.FAILED
        run_scenario(m, {"s2": s2}, sig=f"FAIL: distinct reason {i}")
    ok("a slice failing for a DIFFERENT reason each time is still retried",
       s2["status"] == m.PENDING and s2["author_attempts"] == 2)
    for i in range(2, 16):
        if s2["status"] == m.ESCALATED:
            break
        s2["status"] = m.FAILED
        run_scenario(m, {"s2": s2}, sig=f"FAIL: distinct reason {i}")
    # 2026-10-09: a changing-reason stall earns ONE fixture-class re-spec at the attempt cap
    # (author_attempts is zeroed by it); the signature-blind LIFETIME launch cap is what stops it.
    ok("...but the TOTAL-attempts cap still stops it "
       f"(MAX_AUTHOR_ATTEMPTS={m.MAX_AUTHOR_ATTEMPTS}, lifetime {m.MAX_AUTHOR_LAUNCHES_TOTAL})",
       s2["status"] == m.ESCALATED
       and s2["author_attempts"] <= m.MAX_AUTHOR_ATTEMPTS + 1
       and s2["author_launches_total"] <= m.MAX_AUTHOR_LAUNCHES_TOTAL)


def test_green_at_baseline_is_not_retried(m):
    print("a green-at-baseline FAILED slice is routed, never retried")
    s = _slice(m, m.FAILED)
    seen = run_scenario(m, {"s1": s},
                        sig="FAIL: verify.sh PASSES at baseline -- the fixture "
                            "cannot fail, so it certifies nothing")
    ok("it goes to the already-satisfied decision, not back to PENDING",
       s["status"] in (m.ESCALATED, m.SKIPPED))
    ok("...and no dispatch is launched for a property that already holds",
       "parked" not in seen)


# --------------------------------------------------------------------------
# What the auto-heal must NOT touch
# --------------------------------------------------------------------------
def test_refusals(m):
    print("the auto-heal keeps retry_slice's refusal semantics")
    for st_name in (m.DONE, m.SKIPPED):
        s = _slice(m, st_name)
        seen = run_scenario(m, {"s1": s})
        ok(f"a {st_name} slice is untouched (retry_slice refuses these too)",
           s["status"] == st_name and "removed" not in seen)

    s = _slice(m, m.ESCALATED)
    seen = run_scenario(m, {"s1": s})
    ok("an ESCALATED slice is NOT auto-healed -- it exists to force a human/Claude "
       "judgement, and auto-retrying it would paper over the very thing it parks for",
       s["status"] == m.ESCALATED and "removed" not in seen)

    # The pre-existing stale-marker heal must keep its case.
    s = _slice(m, m.FAILED)
    seen = run_scenario(m, {"s1": s}, inflight=("job77", "running", "alpha-s1"))
    ok("a FAILED slice WITH a live coding job still heals to ENQUEUED "
       "(the new branch does not steal that case)", s["status"] == m.ENQUEUED)
    ok("...and its worktree is NOT cleared while a job is editing it",
       "removed" not in seen)

    # A dep that has not landed: re-authoring would be cut from the wrong tip.
    dep = _slice(m, m.FAILED, sid="s0")
    dependent = _slice(m, m.FAILED, sid="s2", depends_on=["s0"])
    seen = run_scenario(m, {"s0": dep, "s2": dependent})
    ok("a FAILED slice whose dependency is NOT satisfied is not auto-retried "
       "(its re-author would be cut from a tip missing the dependency)",
       dependent["status"] == m.FAILED and "/wt/s2" not in seen.get("removed", []))


# --------------------------------------------------------------------------
# The clear is real, not a recorded no-op
# --------------------------------------------------------------------------
def test_remove_worktree_really_clears(m):
    print("the cleared worktree really is gone (real git, real files)")
    with tempfile.TemporaryDirectory() as td:
        cwt = Path(td) / "chain"
        cwt.mkdir()
        _git(cwt, "init", "-q")
        _git(cwt, "config", "user.email", "t@t")
        _git(cwt, "config", "user.name", "t")
        (cwt / "server.py").write_text("def base():\n    return 0\n")
        _git(cwt, "add", "server.py")
        _git(cwt, "commit", "-qm", "baseline")
        wt = Path(td) / "wt-slice-alpha-s1"
        _git(cwt, "worktree", "add", "-q", "-b", "slice/alpha-s1", str(wt))
        # exactly the mess a nonconvergent author attempt leaves behind
        (wt / "server.py").write_text("def base():\n    return 0\n# half-edited\n")
        for f in ("TASK.md", "refimpl.py", "verify.sh", "check_literals.py",
                  "test_fixture.py"):
            (wt / f).write_text("TODO\n")
        ok("(setup) the dirty worktree exists", wt.exists())
        m.remove_worktree(str(cwt), str(wt))
        ok("remove_worktree wipes the dirty tree AND its scaffold files",
           not wt.exists())
        ok("...and the chain worktree itself is untouched",
           (cwt / "server.py").read_text() == "def base():\n    return 0\n")


# --------------------------------------------------------------------------
# REVERT-TEST: excise the fix, and the bug must come back
# --------------------------------------------------------------------------
def test_revert_proof():
    print("REVERT-TEST: with the auto-heal block removed, the bug reproduces")
    src = SCRIPT.read_text()
    start = src.index("        if status == FAILED:\n            if not deps_done(")
    end = src.index("        if status == PENDING:", start)
    reverted = src[:start] + src[end:]
    ok("(setup) the auto-heal block was found and excised",
       len(reverted) < len(src) and "if status == FAILED:\n            if not deps_done(" not in reverted)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ollama-dispatch-slice-reverted.py"
        p.write_text(reverted)
        old = _load(p, "oslice_reverted")
        s = _slice(old, old.FAILED)
        seen = run_scenario(old, {"s1": s})
        ok("WITHOUT the fix a bare FAILED slice stays FAILED forever",
           s["status"] == old.FAILED)
        ok("WITHOUT the fix its dirty worktree is never cleared",
           "removed" not in seen)
        ok("WITHOUT the fix execute() never re-authors it (silently stepped over)",
           "parked" not in seen)
        ok("WITHOUT the fix nothing even counts the failure",
           s.get("author_attempts") is None)


def main():
    m = _load(SCRIPT, "oslice")
    test_bare_failed_auto_heals(m)
    test_bound_escalates(m)
    test_green_at_baseline_is_not_retried(m)
    test_refusals(m)
    test_remove_worktree_really_clears(m)
    test_revert_proof()
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nSLICE_FAILED_AUTOHEAL_OK: a bare FAILED slice self-heals, bounded by "
          "the existing retry-storm budget, and nothing else is touched")


if __name__ == "__main__":
    main()
