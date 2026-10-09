#!/usr/bin/env python3
"""Regression tests for slicer Bug #2 and Bug #5 (2026-09-18).

Bug #2 (creation-task stub revert -> unwinnable refine loop): a creation-task
target must be git-TRACKED in the sealed baseline. If it is left untracked, the
gate's baseline-revert DELETES the model's authored file (rather than restoring a
stub), the gate reports "EDIT DID NOT LAND", and the slice spins forever.
_seal_creation_target(wt) commits the stub into the baseline when the seal left it
untracked. Red-on-revert: make _seal_creation_target a no-op -> the "target is
tracked after seal" assertion fails.

Bug #5 (empty-diff pass laundered onto the chain): a "done" verdict is NOT proof a
deliverable landed. A job can report done+PASS over an EMPTY diff (the gate
rubber-stamps zero changed files -- arr-codec-floor s1-codec-rank: verdict=pass,
counts.total=0, target unchanged). _stage_deliverable(cwt, wt, target) refuses to
stage an absent or byte-identical target so execute() FAILS the slice instead of
committing an untouched baseline as DONE. Red-on-revert: make _stage_deliverable
always return ('ok','') -> the empty/missing assertions fail.
Also tests _sidecar_status: a completed job's terminal status is recovered from its
never-pruned done.json sidecar when the live queue row is gone.

Run: python3 test-slice-bug2-bug5.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SLICE = Path(__file__).resolve().parent / "ollama-dispatch-slice"


def _load():
    loader = SourceFileLoader("oslice", str(SLICE))
    spec = importlib.util.spec_from_loader("oslice", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True,
                   capture_output=True, text=True)


def _init_repo(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def test_bug2(m):
    print("Bug #2: creation-task target tracked in the sealed baseline")
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        _init_repo(wt)
        (wt / "README.md").write_text("base\n")
        _git(wt, "add", "README.md")
        _git(wt, "commit", "-qm", "base")
        # Creation-task harness: target present but UNTRACKED (the seal left it out).
        tgt = "pkg/widget.py"
        (wt / "pkg").mkdir()
        (wt / tgt).write_text('"""Stub for pkg/widget.py -- implement per TASK.md."""\n')
        (wt / ".dispatch-harness.json").write_text(
            json.dumps({"target": tgt, "creation_task": True}))

        ok("precondition: target is untracked before the seal fix",
           not m._git_tracked(str(wt), tgt))
        res = m._seal_creation_target(str(wt))
        ok("seal-creation-target reports it sealed the stub", res == "sealed")
        ok("target is TRACKED after the fix (revert-test bites here)",
           m._git_tracked(str(wt), tgt))

        # Idempotent: a second call is a no-op (already tracked).
        ok("second call is a no-op (already tracked)",
           m._seal_creation_target(str(wt)) == "noop")

        # Non-creation task -> never touched.
        (wt / ".dispatch-harness.json").write_text(json.dumps({"target": tgt}))
        ok("non-creation-task harness is a no-op",
           m._seal_creation_target(str(wt)) == "noop")

        # Missing target -> reported, not crashed.
        (wt / ".dispatch-harness.json").write_text(
            json.dumps({"target": "gone/none.py", "creation_task": True}))
        ok("absent creation target is reported missing (no crash)",
           m._seal_creation_target(str(wt)) == "missing")


def test_bug5_stage(m):
    print("Bug #5: empty/nonexistent deliverable is NOT committed onto the chain")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwt = root / "chain"
        wt = root / "slicewt"
        cwt.mkdir()
        wt.mkdir()
        _init_repo(cwt)
        tgt = "arr-webhook.py"
        (cwt / tgt).write_text("def base():\n    return 0\n")
        _git(cwt, "add", tgt)
        _git(cwt, "commit", "-qm", "chain baseline")

        # 1) target MISSING in the worktree -> 'missing', nothing staged.
        outcome, _ = m._stage_deliverable(str(cwt), str(wt), tgt)
        ok("missing target -> 'missing'", outcome == "missing")
        staged = subprocess.run(["git", "-C", str(cwt), "diff", "--cached",
                                 "--name-only"], capture_output=True, text=True).stdout
        ok("missing target leaves nothing staged", not staged.strip())

        # 2) target IDENTICAL to the chain baseline (empty-diff pass) -> 'empty'.
        (wt / tgt).write_text("def base():\n    return 0\n")
        outcome, _ = m._stage_deliverable(str(cwt), str(wt), tgt)
        ok("byte-identical target -> 'empty' (empty-diff pass rejected)",
           outcome == "empty")
        staged = subprocess.run(["git", "-C", str(cwt), "diff", "--cached",
                                 "--name-only"], capture_output=True, text=True).stdout
        ok("empty deliverable leaves nothing staged (index reset)",
           not staged.strip())

        # 3) a REAL change -> 'ok', staged.
        (wt / tgt).write_text("def base():\n    return 0\n\ndef codec_rank(x):\n    return x\n")
        outcome, _ = m._stage_deliverable(str(cwt), str(wt), tgt)
        ok("a real change -> 'ok'", outcome == "ok")
        staged = subprocess.run(["git", "-C", str(cwt), "diff", "--cached",
                                 "--name-only"], capture_output=True, text=True).stdout
        ok("a real deliverable IS staged", tgt in staged)


def test_bug5_sidecar(m):
    print("Bug #5: terminal status recovered from the never-pruned sidecar")
    with tempfile.TemporaryDirectory() as td:
        logdir = Path(td)
        (logdir / "archive").mkdir()
        orig = m.LOG_DIR
        m.LOG_DIR = str(logdir)
        try:
            # live done.json
            (logdir / "abc123.done.json").write_text(json.dumps({"status": "done"}))
            ok("live done.json -> 'done'", m._sidecar_status("abc123") == "done")
            # archive done.json
            (logdir / "archive" / "def456.done.json").write_text(
                json.dumps({"status": "failed"}))
            ok("archive done.json -> 'failed'", m._sidecar_status("def456") == "failed")
            # no sidecar -> None
            ok("no sidecar -> None", m._sidecar_status("nope999") is None)
        finally:
            m.LOG_DIR = orig


def main():
    m = _load()
    test_bug2(m)
    test_bug5_stage(m)
    test_bug5_sidecar(m)
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall slice bug2/bug5 tests passed")


if __name__ == "__main__":
    main()
