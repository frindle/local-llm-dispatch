#!/usr/bin/env python3
"""Harness-authoring tree dirt -> UNTRUSTED gate -> regate that froze other bundles
(rt-costco-receipt-attach-s2-pick-receipt-order, stage-3 job b90e77ace2b2, 2026-10-09).

ROOT CAUSE: the stage-2 (fixture) authoring model ran `python3 refimpl.py` by hand, which
applies the reference solution to the TRACKED target. The self-check preserves an already
dirty tracked file (as "the model's own work") and _harness_check_output restores the
pre-check bytes (the same dirt), so nothing ever put the target back; stage 3 launched with
launch_baseline.dirty=1 -> gate "launch baseline was dirty" (HIGH input, UNTRUSTED), the
diff carried the target (+15 lines) and the untracked 963-line dispatch-env.ts (not in the
gate's scaffold set), a 14B reviewer cited "lib/costcoReceiptMatch.ts:976" (a 21-line file;
the text was dispatch-env.ts:949) -> pre-gate FAIL -> authoritative regate enqueued.

Asserted (driver = ollama-dispatch-auto, gate = gate-on-complete.py):
  D1 restore_authoring_tree_to_base puts dirty TRACKED product files back to HEAD, never
     touches harness files / the tracked fixture; creation stub target is rewritten to the stub
  D2 dispatch_model restores the target before AND after an `auto-author-*` job, and leaves an
     `auto-refine-*` job's tree alone
  G1 dispatch-env.ts / dispatch_env.py / manifest-`authored` untracked files are scaffold in
     get_diff (excluded), real untracked product files are not
  G2 get_diff_locked reads the diff while holding the worktree tree lock
  G3 effective_launch_baseline: a dirty launch fully explained by refimpl's own output on an
     auto-author job -> dirty 0 (not UNTRUSTED); real extra dirt / coding job / override stay
  G4 finding_line_out_of_range
  G5 merge_review(pre-gate) drops a review code finding whose line is past the end of the
     cited file; the authoritative regate leaves it

AUTO_SRC / GATE_SRC override the files under test (revert checks).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = os.environ.get("AUTO_SRC") or str(HERE / "ollama-dispatch-auto")
GATE = os.environ.get("GATE_SRC") or str(HERE / "gate-on-complete.py")
sys.path.insert(0, str(HERE))
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load_auto():
    ld = SourceFileLoader("oda_treedirt", AUTO)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_treedirt", ld))
    ld.exec_module(m)
    return m


def load_gate():
    sp = importlib.util.spec_from_file_location("goc_treedirt", GATE)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True).stdout


TARGET_BASE = "".join(f"export const line{i} = {i};\n" for i in range(1, 22))   # 21 lines
REFIMPL = '''import pathlib, sys
wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / "lib/t.ts"
t = p.read_text()
assert "export const line21 = 21;\\n" in t
p.write_text(t + "\\nexport function pick() { return 1; }\\n")
print("refimpl applied")
'''


def make_repo(tmp, name="wt"):
    wt = Path(tmp) / name
    (wt / "lib").mkdir(parents=True)
    git(wt, "init", "-q")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    (wt / "lib/t.ts").write_text(TARGET_BASE)
    (wt / "verify.test.ts").write_text("// fixture v0\n")
    (wt / "README.md").write_text("x\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "base")
    (wt / "refimpl.py").write_text(REFIMPL)
    (wt / "TASK.md").write_text("# T\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": ["TASK.md", "refimpl.py", "dispatch-env.ts", "verify.test.ts"],
         "target": "lib/t.ts", "fixture": "verify.test.ts", "creation_task": False}))
    return wt


def apply_refimpl(wt):
    subprocess.run([sys.executable, "refimpl.py"], cwd=str(wt), check=True, capture_output=True)


tmp = tempfile.mkdtemp(prefix="treedirt-")
try:
    # ------------------------------------------------------------------ driver
    oda = load_auto()
    A = types.SimpleNamespace(target="lib/t.ts", lang="ts", drafter_cmd=None)

    wt = make_repo(tmp, "d1")
    apply_refimpl(wt)
    (wt / "verify.test.ts").write_text("// fixture v1 (the model's own work)\n")
    (wt / "TASK.md").write_text("# T edited\n")
    done = oda.restore_authoring_tree_to_base(wt, "lib/t.ts", "ts", quiet=True)
    check("D1 dirty tracked target is restored to HEAD", (wt / "lib/t.ts").read_text(), TARGET_BASE)
    check("D1 ...and reported", done, ["lib/t.ts"])
    check("D1 tracked fixture edit is NOT touched",
          (wt / "verify.test.ts").read_text(), "// fixture v1 (the model's own work)\n")
    check("D1 untracked harness files are NOT touched",
          ((wt / "TASK.md").read_text(), (wt / "refimpl.py").read_text()), ("# T edited\n", REFIMPL))
    check("D1 a clean tree restores nothing",
          oda.restore_authoring_tree_to_base(wt, "lib/t.ts", "ts", quiet=True), [])

    # creation task: untracked stub target overwritten with a solution -> stub again
    wtc = make_repo(tmp, "d1c")
    (wtc / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": ["TASK.md"], "target": "newmod/x.py", "creation_task": True}))
    (wtc / "newmod").mkdir()
    (wtc / "newmod/x.py").write_text("def real_solution():\n    return 42  # SOLUTION\n")
    oda.restore_authoring_tree_to_base(wtc, "newmod/x.py", "python", quiet=True)
    try:
        ld = SourceFileLoader("scaf_treedirt", str(HERE / "ollama-dispatch-scaffold"))
        sm = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_treedirt", ld))
        ld.exec_module(sm)
        want_stub = sm.creation_stub("newmod/x.py", ".py")
    except Exception as e:                                   # pragma: no cover
        want_stub = f"<no scaffold: {e}>"
    check("D1 creation-task stub target rewritten to the scaffold stub (solution gone)",
          (wtc / "newmod/x.py").read_text(), want_stub)

    # D2: dispatch_model choke point
    seen = {}

    def fake_inner(wt_, prompt, label, verify_cmd, a, max_iters=None):
        seen[label] = (wt_ / "lib/t.ts").read_text() == TARGET_BASE
        (wt_ / "lib/t.ts").write_text(TARGET_BASE + "// model hand-ran refimpl\n")
        return True, "ok"

    oda._dispatch_model_inner = fake_inner
    wt2 = make_repo(tmp, "d2")
    apply_refimpl(wt2)                                       # previous stage's leftover
    ok, _ = oda.dispatch_model(wt2, "p", "auto-author-x-s3", "python3 auto-harness-check.py", A)
    check("D2 auto-author job launches from a clean target", seen.get("auto-author-x-s3"), True)
    check("D2 auto-author job's own hand-applied refimpl is cleaned afterwards",
          (wt2 / "lib/t.ts").read_text(), TARGET_BASE)
    check("D2 dispatch_model return value passes through", ok, True)
    wt3 = make_repo(tmp, "d2r")
    (wt3 / "lib/t.ts").write_text(TARGET_BASE + "// refine-round target edit\n")
    oda.dispatch_model(wt3, "p", "auto-refine-x-r1", "python3 auto-harness-check.py", A)
    check("D2 auto-refine job's tree is left alone (its target edit is legitimate)",
          (wt3 / "lib/t.ts").read_text(), TARGET_BASE + "// model hand-ran refimpl\n")

    # -------------------------------------------------------------------- gate
    gate = load_gate()

    # G1 diff scaffold exclusion
    wt4 = make_repo(tmp, "g1")
    (wt4 / "dispatch-env.ts").write_text("export const x = 1;\n" * 50)
    (wt4 / "custom-harness.txt").write_text("declared in the manifest\n")
    (wt4 / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": ["TASK.md", "refimpl.py", "custom-harness.txt"], "target": "lib/t.ts"}))
    (wt4 / "stray_product.ts").write_text("export const stray = 1;\n")
    excl = []
    dp, how = gate.get_diff(wt4, Path(tmp) / "g1.diff", {}, excl)
    dtxt = dp.read_text() if dp else ""
    check("G1 PIPELINE_HARNESS_BASENAMES are in the gate's scaffold set",
          gate.PIPELINE_HARNESS_BASENAMES <= set(gate._SCAFFOLD_BASENAMES), True)
    # G1b (2026-10-13 review): ollama-queue.py::_SCAFFOLD_BASENAMES now carries dispatch-env.ts
    # itself, so with a loadable queue module the gate's own union is redundant and a revert of
    # PIPELINE_HARNESS_BASENAMES stayed green. The union is the gate's FAIL-OPEN fallback: when the
    # queue module cannot load (GATE_BIN with no ollama-queue.py) the built-in set must still hide
    # the sealed fixture helpers, or dispatch-env.ts rides into the diff again.
    _old_gb = os.environ.get("GATE_BIN")
    os.environ["GATE_BIN"] = str(Path(tmp) / "no-queue-bin")
    try:
        gate_nq = load_gate()
    finally:
        if _old_gb is None:
            os.environ.pop("GATE_BIN", None)
        else:
            os.environ["GATE_BIN"] = _old_gb
    check("G1b queue module unloadable: the fallback scaffold set still hides dispatch-env.ts/.py",
          {"dispatch-env.ts", "dispatch_env.py"} <= set(gate_nq._SCAFFOLD_BASENAMES), True)
    check("G1 dispatch-env.ts is excluded from the diff (reviewer must not be shown it)",
          ("dispatch-env.ts" in dtxt, "dispatch-env.ts" in excl), (False, True))
    check("G1 a manifest-`authored` untracked file is excluded",
          ("custom-harness.txt" in dtxt, "custom-harness.txt" in excl), (False, True))
    check("G1 a real untracked product file stays in the diff", "stray_product.ts" in dtxt, True)

    # G2 the diff is read under the tree lock
    import fcntl
    wt5 = make_repo(tmp, "g2")
    held = {}
    real_get_diff = gate.get_diff

    def spy_get_diff(cwd, out, scaffold=None, excluded=None, base=""):
        gd = git(cwd, "rev-parse", "--absolute-git-dir").strip()
        fh = open(Path(gd) / "dispatch-tree.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held["during"] = False
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            held["during"] = True
        finally:
            fh.close()
        return None, "spy"

    gate.get_diff = spy_get_diff
    gate.get_diff_locked(wt5, Path(tmp) / "g2.diff", {}, [], base="")
    gate.get_diff = real_get_diff
    gd = git(wt5, "rev-parse", "--absolute-git-dir").strip()
    fh = open(Path(gd) / "dispatch-tree.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        free_after = True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        free_after = False
    fh.close()
    check("G2 get_diff runs while the tree lock is HELD", held.get("during"), True)
    check("G2 ...and the lock is released afterwards", free_after, True)

    # G3 effective_launch_baseline
    wt6 = make_repo(tmp, "g3")
    apply_refimpl(wt6)                                       # exactly refimpl's output
    lb = {"head": "abc", "dirty": 1}
    eff = gate.effective_launch_baseline(dict(lb), wt6, "auto-author-x-s3")
    check("G3 dirt == refimpl's own output on an auto-author job -> dirty 0", eff.get("dirty"), 0)
    check("G3 ...original kept as dirty_raw", eff.get("dirty_raw"), 1)
    pl = {"verdict": "pass", "issues": []}
    gate.apply_baseline(pl, eff, -1, "launch")
    check("G3 apply_baseline on the effective baseline: not UNTRUSTED, stays pass",
          (pl.get("untrusted"), pl.get("verdict")), (None, "pass"))
    pl2 = {"verdict": "pass", "issues": []}
    gate.apply_baseline(pl2, dict(lb), -1, "launch")
    check("G3 control: the raw dirty=1 baseline IS untrusted", bool(pl2.get("untrusted")), True)
    check("G3 a CODING job's dirty launch is never explained away",
          gate.effective_launch_baseline(dict(lb), wt6, "rt-costco-receipt-attach-s2").get("dirty"), 1)
    check("G3 an explicit --launch-dirty override is respected",
          gate.effective_launch_baseline(dict(lb), wt6, "auto-author-x-s3", 3).get("dirty"), 1)
    check("G3 dirt beyond what refimpl explains (2 > 1) is NOT explained",
          gate.effective_launch_baseline({"head": "abc", "dirty": 2}, wt6, "auto-author-x-s3").get("dirty"), 2)
    (wt6 / "lib/t.ts").write_text((wt6 / "lib/t.ts").read_text() + "// someone's real edit\n")
    check("G3 a target that differs from refimpl's output is real work: not explained",
          gate.effective_launch_baseline(dict(lb), wt6, "auto-author-x-s3").get("dirty"), 1)
    wt7 = make_repo(tmp, "g3b")
    apply_refimpl(wt7)
    (wt7 / "README.md").write_text("changed too\n")
    check("G3 refimpl residue MIXED with an unexplained tracked edit: explain nothing",
          gate.effective_launch_baseline({"head": "abc", "dirty": 2}, wt7, "auto-author-x-s3").get("dirty"), 2)

    # G4 finding_line_out_of_range
    wt8 = make_repo(tmp, "g4")
    chk = gate.finding_line_out_of_range
    check("G4 line 976 in a 21-line file -> out of range",
          chk(wt8, {"file": "lib/t.ts", "line": 976}), True)
    check("G4 line 21 (last line) is in range", chk(wt8, {"file": "lib/t.ts", "line": 21}), False)
    check("G4 line 22 is out of range", chk(wt8, {"file": "lib/t.ts", "line": 22}), True)
    check("G4 no line cited -> abstain", chk(wt8, {"file": "lib/t.ts", "line": 0}), False)
    check("G4 file missing -> abstain", chk(wt8, {"file": "nope.ts", "line": 9999}), False)
    check("G4 bare basename resolves through the diff files",
          chk(wt8, {"file": "t.ts", "line": 976}, ["lib/t.ts"]), True)
    check("G4 no worktree -> abstain", chk("", {"file": "lib/t.ts", "line": 976}), False)

    # G5 merge_review pre-gate vs regate
    def _boom(*a, **k):
        raise RuntimeError("test must not reach a live side effect")

    escalated = []
    gate._escalate_regate = lambda *a, **k: escalated.append(1)
    sandbox = Path(tempfile.mkdtemp(prefix="treedirt-sbx-", dir=tmp))
    (sandbox / "ollama-queue-logs" / "auto-fix").mkdir(parents=True)
    gate.BIN = sandbox
    gate.COMPLETED_ROOT = sandbox / "Completed"
    gate.TEST_MODE = True
    gate.subprocess = types.SimpleNamespace(run=_boom, Popen=_boom, PIPE=-1, DEVNULL=-3,
                                            TimeoutExpired=Exception, CalledProcessError=Exception)
    REPORT = ("# Review\n\n## VERDICT: FAIL\n\n| # | Severity | Location | Finding |\n|---|---|---|---|\n"
              "| 1 | high | `lib/t.ts:976` | dispatch-env.ts:949 uses a non-standard builtin |\n"
              "| 2 | high | `lib/t.ts:10` | line10 is wrong |\n")

    def merge(prefix, authoritative, parent):
        td = Path(tempfile.mkdtemp(dir=tmp))
        wtm = make_repo(td, "wtm")
        pl = {"verdict": "pass-pending-review", "issues": [], "counts": {}, "job_cwd": str(wtm),
              "job_label": f"auto-author-{parent}", "job_id": parent, "not_checked": []}
        (td / f"{parent}.gate.json").write_text(json.dumps(pl))
        (td / f"{parent}.diff").write_text(
            "diff --git a/lib/t.ts b/lib/t.ts\n--- a/lib/t.ts\n+++ b/lib/t.ts\n@@ -1 +1 @@\n-a\n+b\n")
        rd = td / f"{prefix}{parent}"
        rd.mkdir()
        (rd / "report.md").write_text(REPORT)
        a = types.SimpleNamespace(job_label=f"{prefix}{parent}", cwd=str(rd), job_id="t0", out_dir=str(td))
        try:
            gate.merge_review(a, td, prefix=prefix, authoritative=authoritative)
        except RuntimeError as e:
            print("   (merge stopped at a guarded side effect:", e, ")")
        return json.loads((td / f"{parent}.gate.json").read_text())

    g_pre = merge("gate-", False, "aaaa11112222")
    lines_pre = sorted(i["line"] for i in g_pre["issues"] if i.get("source") == "review")
    check("G5 pre-gate: the line-976 finding on a 21-line file is DROPPED, line 10 kept", lines_pre, [10])
    check("G5 ...and the drop is recorded",
          [d.get("line") for d in g_pre.get("pregate_dropped_ungrounded_line", [])], [976])
    g_re = merge("regate-", True, "bbbb33334444")
    lines_re = sorted(i["line"] for i in g_re["issues"] if i.get("source") == "review")
    check("G5 authoritative regate is left intact (both rows kept)", lines_re, [10, 976])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print(f"\nHARNESS_TREE_DIRT_FAIL ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("\nHARNESS_TREE_DIRT_OK")
