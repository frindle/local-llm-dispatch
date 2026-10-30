#!/usr/bin/env python3
"""test-canary-depmap.py -- the canary testing ITSELF (hardening design 4: seam `depmap`).

A 4-seam fixture bin proves, against the real pipeline-canary.py machinery:
  * the run-time dependency map (sitecustomize audit hook) records imports, opened data files and
    the files a CHILD process touches, and nothing the seam did not touch;
  * proof staleness is scoped to the code around the anchor: an unrelated function edit stales NO
    proof (only re-runs the seams that depend on the file), an edit to the anchor's function or to a
    module-level constant stales it, a non-Python file uses a +-40 line window;
  * a vanished anchor is ANCHOR-LOST (never silently stale, never re-proved);
  * a dependency edit selects a green re-run only (`--affected` plan);
  * parallel prove (private bin copy per worker) gives results identical to serial;
  * merge keeps the newer per-seam entry; derived file list; CANARY_DEPMAP=0 kill switch;
  * the recorder never raises into the program under test and chains to the interpreter's own
    sitecustomize.

  python3 test-canary-depmap.py [--bin DIR]        -> prints CANARY_DEPMAP_OK
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

BIN = Path(__file__).resolve().parent
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
spec = importlib.util.spec_from_file_location("pc_under_test", str(BIN / "pipeline-canary.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["pc_under_test"] = m
spec.loader.exec_module(m)

FAILS: list = []


def check(name, ok, note=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -- {note}"))
    if not ok:
        FAILS.append(name)


TARGET = '''LIMIT = 10


def f1(x):
    return x + 1  # ANCHOR1


def f2(x):
    return x * 2  # ANCHOR2


def f3(x):
    return x  # unrelated


class K:
    def m1(self):
        return 1  # ANCHOR3

    def m2(self):
        return 2
'''
NOTES = "\n".join(f"line {i}" for i in range(1, 100)).replace("line 50", "KEEP=1") + "\n"
FILES = {
    "helper.py": "def h():\n    return 1\n",
    "target.py": TARGET,
    "notes.txt": NOTES,
    "data.txt": "payload\n",
    "child.py": ("import os\nd = os.path.dirname(os.path.abspath(__file__))\n"
                 "print(open(os.path.join(d, 'data.txt')).read().strip())\n"),
    "test-a.py": ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                  "import helper, target\nassert target.f1(1) == 2 and helper.h() == 1\nprint('A_OK')\n"),
    "test-b.py": ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                  "import target\nassert target.f2(3) == 6\nprint('B_OK')\n"),
    "test-c.py": ("import os, subprocess, sys\nd = os.path.dirname(os.path.abspath(__file__))\n"
                  "r = subprocess.run([sys.executable, os.path.join(d, 'child.py')], capture_output=True, text=True)\n"
                  "assert r.stdout.strip() == 'payload', r\n"
                  "subprocess.run(['/bin/sh', os.path.join(d, 'child.sh')], check=True)\nprint('C_OK')\n"),
    "child.sh": "cat \"$(dirname \"$0\")/data.txt\" > /dev/null\n",
    "test-e.py": ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                  "import target\nassert target.K().m1() == 1\nprint('E_OK')\n"),
    "test-d.py": ("import os\nd = os.path.dirname(os.path.abspath(__file__))\n"
                  "assert 'KEEP=1' in open(os.path.join(d, 'notes.txt')).read()\nprint('D_OK')\n"),
}


def fx(sid, tool, marker, proofs):
    return dict(id=sid, name="fixture " + sid, tool=tool, marker=marker, timeout=60, proofs=proofs)


FX_SEAMS = (
    fx("A", "test-a.py", "A_OK", [[{"mut": "target.py", "old": "return x + 1  # ANCHOR1", "new": "return x - 1  # ANCHOR1", "why": "f1"}]]),
    fx("B", "test-b.py", "B_OK", [[{"mut": "target.py", "old": "return x * 2  # ANCHOR2", "new": "return x * 3  # ANCHOR2", "why": "f2"}]]),
    fx("C", "test-c.py", "C_OK", [[{"mut": "data.txt", "old": "payload", "new": "other", "why": "data"}]]),
    fx("E", "test-e.py", "E_OK", [[{"mut": "target.py", "old": "return 1  # ANCHOR3", "new": "return 0  # ANCHOR3", "why": "m1"}]]),
    fx("D", "test-d.py", "D_OK", [[{"mut": "notes.txt", "old": "KEEP=1", "new": "KEEP=0", "why": "notes"}]]),
)
m.SEAMS = FX_SEAMS
TMP = Path(tempfile.mkdtemp(prefix="canary-depmap-test-")).resolve()
m.COVERAGE_PATH = TMP / "coverage.json"
m.AFFECTED_PATH = TMP / "affected.json"
m.AFFECTED_LOCK = TMP / "affected.lock"
m.AFFECTED_DIRTY = TMP / "affected.dirty"


def make_bin(name):
    d = TMP / name
    d.mkdir()
    for f, t in FILES.items():
        (d / f).write_text(t)
    return d


LEAKS: list = []


def prove_all(src, jobs):
    root = Path(tempfile.mkdtemp(prefix="w", dir=str(TMP))).resolve()
    pre = m._SrcCache(src)
    ctx = {s["id"]: m.seam_ctxs(s, pre) for s in FX_SEAMS}
    res, _ = m.run_pool(list(FX_SEAMS), src, root, jobs,
                        lambda s, w, pr: m.prove_seam(s, w, src, ctx[s["id"]], pr))
    for wb in root.glob("w*/home/bin"):
        for f, txt in FILES.items():
            if (wb / f).read_text() != txt:
                LEAKS.append(f"{wb.parent.parent.name}/{f}")
    shutil.rmtree(root, ignore_errors=True)
    return {s["id"]: res[s["id"]] for s in FX_SEAMS}


def compact(res):
    return {k: (v["green_now"], v["proven"], [(p["red"], p["partial"]) for p in v["proofs"]]) for k, v in res.items()}


def scenario(tag, fname, old, new):
    d = TMP / f"s_{tag}"
    shutil.copytree(BASE, d)
    t = (d / fname).read_text()
    assert old in t, (tag, old)
    (d / fname).write_text(t.replace(old, new, 1))
    return d


# --- baseline: serial and parallel prove agree --------------------------------------------------
BASE = make_bin("base")
serial = prove_all(BASE, 1)
par = prove_all(BASE, 3)
check("prove.fixture_all_proven_serial", all(v["proven"] for v in serial.values()), json.dumps(compact(serial)))
check("prove.parallel_equals_serial", compact(serial) == compact(par), f"{compact(serial)} != {compact(par)}")
check("prove.worker_bins_restored_after_every_proof", not LEAKS, str(LEAKS))
check("prove.base_bin_untouched_by_workers", all((BASE / f).read_text() == t for f, t in FILES.items()))
check("prove.results_in_seams_order", list(par) == [s["id"] for s in FX_SEAMS])
check("prove.parallel_pool_returns_all_seams", len(par) == len(FX_SEAMS) and all("proofs" in v for v in par.values()))
COV = {"ts": 1.0, "seams": serial}
m.COVERAGE_PATH.write_text(json.dumps(COV))

# --- the dependency map --------------------------------------------------------------------------
dA, dB, dC, dD = (set(serial[k]["deps"]) for k in "ABCD")
check("deps.import_chain_recorded", {"helper.py", "target.py", "test-a.py"} <= dA, str(dA))
check("deps.no_untouched_file", "helper.py" not in dB and "child.py" not in dB and "notes.txt" not in dB, str(dB))
check("deps.child_process_recorded", {"child.py", "data.txt", "test-c.py"} <= dC, str(dC))
check("deps.non_python_exec_target_recorded", "child.sh" in dC, str(dC))
check("deps.data_file_recorded", "notes.txt" in dD, str(dD))
check("deps.hashes_are_pristine", serial["A"]["deps"]["target.py"] == m._h(BASE / "target.py"))

# --- staleness scoped to the anchor's function --------------------------------------------------
an = m.analyze_coverage(BASE, COV)
check("stale.clean_tree_all_ok", sorted(an["ok"]) == ["A", "B", "C", "D", "E"] and not an["reprove"] and not an["recheck"], str(an))

d = scenario("helper", "helper.py", "return 1", "return 2")
an = m.analyze_coverage(d, COV)
check("stale.dep_edit_is_green_rerun_only", list(an["recheck"]) == ["A"] and not an["reprove"], str(an))

d = scenario("f3", "target.py", "return x  # unrelated", "return x  # unrelated, edited")
an = m.analyze_coverage(d, COV)
check("stale.unrelated_function_edit_stales_no_proof", not an["reprove"], str(an["reprove"]))
check("stale.unrelated_edit_reruns_dependents", sorted(an["recheck"]) == ["A", "B", "E"], str(an["recheck"]))

d = scenario("f1", "target.py", "return x + 1  # ANCHOR1", "return x + 1  # ANCHOR1 (tweaked)")
an = m.analyze_coverage(d, COV)
check("stale.anchor_function_edit_stales_that_proof_only", list(an["reprove"]) == ["A"], str(an["reprove"]))

d = scenario("const", "target.py", "LIMIT = 10", "LIMIT = 11")
an = m.analyze_coverage(d, COV)
check("stale.module_constant_edit_stales_every_proof_in_file", sorted(an["reprove"]) == ["A", "B", "E"], str(an["reprove"]))
d = scenario("m2", "target.py", "        return 2\n", "        return 3\n")
an = m.analyze_coverage(d, COV)
check("stale.sibling_method_edit_stales_no_proof", not an["reprove"], str(an["reprove"]))
d = scenario("m1", "target.py", "return 1  # ANCHOR3", "return 1  # ANCHOR3 edited")
an = m.analyze_coverage(d, COV)
check("stale.anchor_method_edit_stales_that_proof_only", list(an["reprove"]) == ["E"], str(an["reprove"]))

d = scenario("testb", "test-b.py", "print('B_OK')", "print('B_OK')  # touched")
an = m.analyze_coverage(d, COV)
check("stale.seam_test_edit_stales_its_seam", list(an["reprove"]) == ["B"], str(an["reprove"]))

d = scenario("notes_far", "notes.txt", "line 5\n", "line 5 edited\n")
an = m.analyze_coverage(d, COV)
check("stale.nonpython_far_edit_outside_window", "D" not in an["reprove"], str(an["reprove"]))
d = scenario("notes_near", "notes.txt", "line 49\n", "line 49 edited\n")
an = m.analyze_coverage(d, COV)
check("stale.nonpython_near_edit_inside_window", "D" in an["reprove"], str(an["reprove"]))

# --- ANCHOR-LOST --------------------------------------------------------------------------------
d = scenario("lost", "target.py", "return x + 1  # ANCHOR1", "return x + 2")
an = m.analyze_coverage(d, COV)
check("anchor.lost_is_reported", list(an["anchor_lost"]) == ["A"], str(an["anchor_lost"]))
pl = m.affected_plan(d, COV)
check("anchor.lost_seam_is_not_reproved", "A" not in pl["reprove"], str(pl["reprove"]))
COV_UNPROVEN = {"ts": 1.0, "seams": {**serial, "A": {**serial["A"], "proven": False}}}
pl = m.affected_plan(TMP / "s_lost", COV_UNPROVEN)
check("anchor.unproven_and_lost_seam_is_not_reproved", "A" in pl["analysis"]["unproven"] and "A" not in pl["reprove"], str(pl["reprove"]))
d = scenario("dup", "target.py", "def f3(x):", "def f3(x):\n    return x + 1  # ANCHOR1\n\n\ndef f4(x):")
an = m.analyze_coverage(d, COV)
check("anchor.duplicated_anchor_is_lost_too", "A" in an["anchor_lost"], str(an["anchor_lost"]))

# --- CANARY_DEPMAP=0 -----------------------------------------------------------------------------
os.environ["CANARY_DEPMAP"] = "0"
try:
    d = TMP / "s_f3"
    an = m.analyze_coverage(d, COV)
    check("killswitch.depmap_off_is_file_level", sorted(an["reprove"]) == ["A", "B", "E"], str(an["reprove"]))
    check("killswitch.depmap_off_static_file_list", "helper.py" not in m.list_files())
finally:
    del os.environ["CANARY_DEPMAP"]
check("files.derived_list_includes_seam_deps", {"helper.py", "child.py", "data.txt"} <= set(m.list_files()), str(m.list_files()[-5:]))
check("files.static_names_kept", set(m.PIPELINE_FILES) <= set(m.list_files()))

# --- --affected plan + run ---------------------------------------------------------------------
d = TMP / "s_aff"
shutil.copytree(BASE, d)
(d / "helper.py").write_text("def h():\n    return 1  # dep edit\n")
t = (d / "target.py").read_text()
(d / "target.py").write_text(t.replace("return x + 1  # ANCHOR1", "return x + 1  # ANCHOR1 v2"))
pl = m.affected_plan(d, COV)
check("affected.plan_reprove_only_touched_proof", pl["reprove"] == ["A"], str(pl["reprove"]))
check("affected.plan_green_only_dependents", pl["green"] == ["B", "E"], str(pl["green"]))
ns = types.SimpleNamespace(bin=str(d), dry_run=False, jobs=2, max_reprove=0)
rc = m.affected(ns)
doc = json.loads(m.AFFECTED_PATH.read_text())
p0 = doc["passes"][0]
check("affected.run_executes_plan_and_passes", rc == 0 and p0["reprove"] == ["A"] and p0["green"] == ["B", "E"] and not p0["failed"],
      json.dumps(doc)[:400])

# --- merge: the newer per-seam entry wins ------------------------------------------------------
m.COVERAGE_PATH.write_text(json.dumps({"ts": 1.0, "seams": {"X": {"ts": 100.0, "v": "new"}}}))
m.merge_coverage({"X": {"ts": 50.0, "v": "older"}, "Y": {"ts": 10.0, "v": "y"}})
cv = json.loads(m.COVERAGE_PATH.read_text())
check("merge.older_result_does_not_clobber_newer", cv["seams"]["X"]["v"] == "new" and cv["seams"]["Y"]["v"] == "y", str(cv))
m.merge_coverage({"X": {"ts": 200.0, "v": "newest"}}, full=True)
cv = json.loads(m.COVERAGE_PATH.read_text())
check("merge.newer_replaces_and_full_flag_recorded", cv["seams"]["X"]["v"] == "newest" and cv.get("full_prove_ts"), str(cv))

# --- the recorder never breaks the program under test ------------------------------------------
sc = TMP / "sc"
sc.mkdir()
(sc / "sitecustomize.py").write_text(m.DEPMAP_SITECUSTOMIZE)
env = dict(os.environ, PYTHONPATH=str(sc), CANARY_DEPS_FILE="/nonexistent-dir/x/deps.txt", CANARY_DEPS_BIN=str(BASE))
r = subprocess.run([sys.executable, "-c", "print('alive')"], env=env, capture_output=True, text=True)
check("recorder.unwritable_deps_file_is_harmless", r.returncode == 0 and r.stdout.strip() == "alive", r.stderr[-200:])
env.pop("PYTHONPATH")
has_real = subprocess.run([sys.executable, "-c", "import importlib.util as u;print(u.find_spec('sitecustomize') is not None)"],
                          env=env, capture_output=True, text=True).stdout.strip() == "True"
env["PYTHONPATH"] = str(sc)
env["CANARY_DEPS_FILE"] = str(TMP / "d.txt")
r = subprocess.run([sys.executable, "-c", "import sys;print('_chained_sitecustomize' in sys.modules)"],
                   env=env, capture_output=True, text=True)
check("recorder.chains_to_interpreter_sitecustomize", r.stdout.strip() == str(has_real), f"real={has_real} out={r.stdout!r}")

# nested recorders (a seam that itself runs seams: PYTHONPATH carries two .canary-depmap dirs) must not
# chain into each other -- that recursed and made every nested python start ~0.5 s (a 14 s test)
import time as _t
dm1, dm2 = TMP / "n1" / ".canary-depmap", TMP / "n2" / ".canary-depmap"
for d_ in (dm1, dm2):
    d_.mkdir(parents=True)
    (d_ / "sitecustomize.py").write_text(m.DEPMAP_SITECUSTOMIZE)
env = dict(os.environ, PYTHONPATH=f"{dm1}{os.pathsep}{dm2}", CANARY_DEPS_FILE=str(TMP / "n.txt"), CANARY_DEPS_BIN=str(BASE))
t0 = _t.time()
r = subprocess.run([sys.executable, "-c", "import json;print('alive')"], env=env, capture_output=True, text=True)
check("recorder.nested_dirs_do_not_chain_into_each_other", r.stdout.strip() == "alive" and _t.time() - t0 < 0.3,
      f"{_t.time() - t0:.2f}s {r.stderr[-120:]}")

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
if not FAILS:
    print("CANARY_DEPMAP_OK")
sys.exit(1 if FAILS else 0)
