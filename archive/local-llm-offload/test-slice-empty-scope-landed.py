#!/usr/bin/env python3
"""Regression test for the 2026-09-25 "empty forward scope" integration-gate bug.

THE BUG. `chain_scope(cwt, dref)` diffs merge-base(dref, HEAD)..HEAD. That diff is
empty in two OPPOSITE situations:

  (a) the chain never changed anything outside the scaffold -- genuinely nothing to
      land, and failing the gate is right; and
  (b) the chain ALREADY LANDED -- its tip is an ancestor of dref, so the merge-base
      IS the tip and the forward diff is empty BECAUSE the work is on main.

`integrate_chain` failed both as (a), and it did so BEFORE ollama-dispatch-integrate
was ever invoked -- so `landed_on_default()` (558a17b), which exists precisely to
recognise (b), never ran. Live cost: the bg-brokers / bg-captcha / bg-formfill /
bg-interpret escalation wave -- four broker-guard chains whose tip 996627a is a
literal ancestor of main, each re-escalated as "the gate is broken, not the code".

THE FIX. `chain_already_landed()` requires BOTH that HEAD is reachable from dref
AND that each of the plan's targets exists on dref without the scaffold stub marker.
Ancestry alone would be a proxy, not the property: a chain that never committed
anything also has HEAD == its fork point, which is trivially an ancestor of dref --
so ancestry alone would pass the "did nothing" case too.

RED ON REVERT. Make `chain_already_landed` return (False, ...) unconditionally (or
delete the branch that calls it) -> the already-landed assertions fail. Return
(True, ...) unconditionally, or drop the stub/existence half and keep only the
ancestry check -> the "a chain that did nothing still FAILS" and "a stub on main is
not landed" assertions fail. Both directions are asserted, so a vacuously permissive
and a vacuously strict implementation are each caught.

Run: python3 test-slice-empty-scope-landed.py
"""
import importlib.util
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
    return m


def _git(cwd, *a, check=True):
    return subprocess.run(["git", "-C", str(cwd), *a], check=check,
                          capture_output=True, text=True)


STUB = "# Stub for app.py -- implement per TASK.md\n"
REAL = "def base():\n    return 0\n\ndef added():\n    return 1\n"


def _repo_with_chain(root, land_it, baseline=STUB):
    """A repo on `main` with a `slice/alpha` branch. When `land_it`, the chain makes
    a real change and main fast-forwards onto it (case b: landed). Otherwise the
    chain never commits, so its forward diff is empty because it did NOTHING (case
    a) -- and, crucially, its tip is STILL an ancestor of main."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text(baseline)
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "baseline")

    _git(repo, "checkout", "-q", "-b", "slice/alpha")
    if land_it:
        (repo / "app.py").write_text(REAL)
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-qm", "slice s1: add a thing")
    _git(repo, "checkout", "-q", "main")
    if land_it:
        _git(repo, "merge", "-q", "--ff-only", "slice/alpha")

    cwt = root / "chain"
    _git(repo, "worktree", "add", "-q", str(cwt), "slice/alpha")
    return repo, cwt


def _state(m, repo, cwt):
    return {"label": "alpha", "repo": str(repo), "target": "app.py", "lang": "python",
            "chain_branch": "slice/alpha", "chain_worktree": str(cwt), "slices": {}}


def _no_integrate(args):
    raise AssertionError(
        "ollama-dispatch-integrate must NOT be invoked on the empty-scope path: "
        f"called with {args}")


def test_already_landed_passes(m):
    print("an already-landed chain (tip IS an ancestor of main) is PASSED, not failed")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, cwt = _repo_with_chain(root, land_it=True)
        m.STATE_ROOT = str(root / "state")

        tip = _git(cwt, "rev-parse", "HEAD").stdout.strip()
        anc = _git(repo, "merge-base", "--is-ancestor", tip, "main", check=False).returncode
        ok("fixture really is the landed case (tip is an ancestor of main)", anc == 0)
        ok("...and the forward scope really is empty", m.chain_scope(str(cwt), "main") == [])

        st = _state(m, repo, cwt)
        status = m.integrate_chain(st, cwt=str(cwt), run_integrate=_no_integrate)
        rec = st.get("integration") or {}
        ok("status is passed", status == "passed")
        ok("the record says it landed", rec.get("landed_ancestor") is True)
        ok("the summary names the real reason (LANDED, not empty)",
           "LANDED" in (rec.get("summary") or ""))
        ok("no stage was recorded (nothing left to stage)", not rec.get("staged"))
        ok("main was not touched",
           _git(repo, "rev-parse", "main").stdout.strip() == tip)


def test_did_nothing_still_fails(m):
    print("a chain that did NOTHING still FAILS -- even though its tip is an ancestor")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, cwt = _repo_with_chain(root, land_it=False)
        m.STATE_ROOT = str(root / "state")

        ok("the forward scope really is empty", m.chain_scope(str(cwt), "main") == [])
        ok("THE TRAP: the tip of a chain that did nothing IS an ancestor of main",
           _git(repo, "merge-base", "--is-ancestor", "slice/alpha", "main",
                check=False).returncode == 0)

        st = _state(m, repo, cwt)
        status = m.integrate_chain(st, cwt=str(cwt), run_integrate=_no_integrate)
        rec = st.get("integration") or {}
        ok("status is failed", status == "failed")
        ok("the failed stage is scope", "scope" in (rec.get("failed_stages") or []))
        ok("it is NOT mislabelled as landed", not rec.get("landed_ancestor"))
        ok("the record says WHY (the target on main is still the stub)",
           "stub" in (rec.get("empty_scope_why") or ""))


def test_missing_target_is_not_landed(m):
    print("an ancestor chain whose target does not exist on main is NOT landed")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, cwt = _repo_with_chain(root, land_it=True)
        m.STATE_ROOT = str(root / "state")
        st = _state(m, repo, cwt)
        st["target"] = "never_written.py"
        status = m.integrate_chain(st, cwt=str(cwt), run_integrate=_no_integrate)
        rec = st.get("integration") or {}
        ok("status is failed", status == "failed")
        ok("the record says the target is absent on main",
           "does not exist" in (rec.get("empty_scope_why") or ""))


def test_helper_is_not_vacuous(m):
    print("chain_already_landed discriminates (not a constant)")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, cwt = _repo_with_chain(root, land_it=True)
        ok("True when HEAD is reachable from main and the target is implemented",
           m.chain_already_landed(str(cwt), "main", ["app.py"])[0])
        # Advance the chain past main -> no longer an ancestor.
        (cwt / "app.py").write_text("def base():\n    return 0\n\ndef later():\n    return 2\n")
        _git(cwt, "add", "app.py")
        _git(cwt, "commit", "-qm", "chain moves ahead")
        ok("False once the chain moves ahead of main",
           not m.chain_already_landed(str(cwt), "main", ["app.py"])[0])
        ok("False for a ref that does not exist",
           not m.chain_already_landed(str(cwt), "no-such-branch", ["app.py"])[0])
        ok("False when the plan names no target",
           not m.chain_already_landed(str(cwt), "main", [])[0])


def test_stale_seed_does_not_clobber(m):
    """The bg-health corruption, reproduced in miniature.

    Live sequence (broker-guard, 2026-09-20): slice s3-heartbeat's worktree was cut
    from chain tip 72dac47; s1-status then advanced the chain to 633ecfa (adding
    update_run_status and removing the scaffold docstring); 16 seconds later
    s3-heartbeat landed as c18c831 by COPYING its whole stale target over the chain's
    -- reverting s1-status and restoring the stub marker. The chain read "4 done"
    while its health.py had lost a done slice's work, which is what the integration
    gate later reported as `unconverged`.

    _stage_deliverable's STALE-SEED REBASE (2026-09-23) measures the seed against the
    live tip and 3-way merges instead of copying. This asserts the predecessor's work
    SURVIVES -- the property, not the proxy of "a rebase happened".

    Red on revert: drop the `base != tip` branch (copy unconditionally) -> the
    "predecessor's function survives" assertion fails."""
    print("a stale-seeded deliverable does NOT revert the predecessor slice")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        tgt = "pkg/health.py"
        (repo / "pkg").mkdir()
        base_txt = ('"""Stub for pkg/health.py -- implement per TASK.md."""\n\n\n'
                    "def classify_failure(e):\n    return 'tool_side'\n")
        (repo / tgt).write_text(base_txt)
        _git(repo, "add", tgt)
        _git(repo, "commit", "-qm", "slice s2-classify")
        seed = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # The slice worktree for s3 is cut HERE, from the seed.
        wt = root / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "dispatch/s3", str(wt), seed)

        # Meanwhile s1-status lands on the chain: adds update_run_status, drops the stub.
        cwt = root / "chain"
        _git(repo, "worktree", "add", "-q", "-b", "slice/alpha", str(cwt), seed)
        (cwt / tgt).write_text("def update_run_status(prev, ok):\n    return {}\n\n\n"
                               "def classify_failure(e):\n    return 'tool_side'\n")
        _git(cwt, "add", tgt)
        _git(cwt, "commit", "-qm", "slice s1-status")
        tip = _git(cwt, "rev-parse", "HEAD").stdout.strip()
        ok("the chain really moved past the seed", tip != seed)

        # s3's deliverable, authored against the STALE seed (keeps the stub docstring,
        # knows nothing of update_run_status) -- byte-for-byte the shape of c18c831.
        (wt / tgt).write_text(base_txt.replace(
            "def classify_failure(e):",
            "def heartbeat_stale(a, b, c):\n    return a > c\n\n\ndef classify_failure(e):"))
        # verify.sh is re-run by the rebase path; make it unconditionally green so the
        # assertion below is about the MERGE, not about a fixture.
        (wt / "verify.sh").write_text("#!/bin/sh\necho VERIFY_OK\n")

        outcome, msg = m._stage_deliverable(str(cwt), str(wt), tgt)
        merged = (cwt / tgt).read_text()
        ok(f"staging a stale-seeded deliverable is not a silent copy (got {outcome!r})",
           outcome in ("ok", "conflict"))
        if outcome == "ok":
            ok("THE PROPERTY: the predecessor slice's update_run_status SURVIVES",
               "def update_run_status" in merged)
            ok("...and the new slice's own work is present too",
               "def heartbeat_stale" in merged)
            ok("...and the scaffold stub marker is NOT resurrected",
               "Stub for pkg/health.py" not in merged)
        else:
            ok("a conflict is REPORTED rather than clobbering (chain tree untouched)",
               "def update_run_status" in merged)


def main():
    m = _load(BIN / "ollama-dispatch-slice", "slicer_empty_scope")
    test_already_landed_passes(m)
    test_did_nothing_still_fails(m)
    test_missing_target_is_not_landed(m)
    test_helper_is_not_vacuous(m)
    test_stale_seed_does_not_clobber(m)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
