#!/usr/bin/env python3
"""Regression test for the Rivian s1/s2 auto-authoring failures (2026-10-01).

Each asserted by BEHAVIOUR:
  ctx-budget  -- the darkbloom CLI lives in ~/.darkbloom/bin, NOT on the launchd
                 PATH of the daemon / detached advances: _darkbloom_bin must find
                 it anyway; the ceiling must size from the PEAK idle memory, not
                 the "usable now" a busy lane reports; a fallback is cached briefly.
  cwd-exclusive -- a job whose cwd is a non-checkout ANCESTOR ($HOME esc-review)
                 does not own the worktree; a real checkout/same tree still does;
                 auto waits out a cwd-exclusive-only NO-GO without a refine round
                 and never sends operational blockers to the model.
  refine SKIPPED -- with survivors outstanding, auto-harness-check FAILS while
                 nothing changed and stops failing once something did; survivors
                 whose force-false mutant lived are named as a REDUNDANT line.
  literal drift -- freezing a literal with backslashes (replace(/\\D/g, '')) keeps
                 every backslash (auto's self-check AND the scaffold).
Run: python3 test-rivian-rootcause.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PRE = Path(os.environ.get("PRE_SRC") or HERE / "ollama-dispatch-preflight")
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
SCAF = Path(os.environ.get("SCAF_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_ctx(q):
    tmp = Path(tempfile.mkdtemp())
    fake = tmp / "darkbloom"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(0o755)
    q._DARKBLOOM_BIN_CANDIDATES = (tmp / "missing", fake)
    check("darkbloom CLI found off-PATH (launchd PATH)", q._darkbloom_bin(which=lambda n: None), str(fake))
    check("PATH hit still preferred", q._darkbloom_bin(which=lambda n: "/x/darkbloom"), "/x/darkbloom")
    up = q.darkbloom_usable_peak_update
    r = up({}, "a,b", 25.3, 100)
    r = up(r, "a,b", 9.4, 200)
    check("busy-lane reading does not lower the peak", r["gb"], 25.3)
    check("a new warm-model set resets the peak", up(r, "a", 12.0, 300)["gb"], 12.0)
    check("a stale peak is replaced", up(r, "a,b", 9.4, 100 + 8 * 86400)["gb"], 9.4)
    st = {"usable_gb": 9.4, "warm": ["a", "b"]}
    pk = tmp / "peak.json"
    pk.write_text(json.dumps({"warm": "a,b", "gb": 25.3, "at": 1e12}))
    check("peak persisted across processes is used", q._darkbloom_usable_peak(st, 1e12 + 5, path=pk), 25.3)
    cache = {}
    os.environ.pop("DARKBLOOM_CTX", None)
    q.darkbloom_ctx_ceiling("m", now=0, probe=lambda m: None, cache=cache, derive=lambda m: None)
    check("a fallback answer is cached <= 30s, not the full TTL", cache["m"][0] <= 30, True)


def test_cwd(pre):
    root = Path(tempfile.mkdtemp())
    wt = root / "wt"
    (wt / ".git").mkdir(parents=True)
    state = root / "state.json"

    def run(jobs):
        state.write_text(json.dumps({"jobs": jobs}))
        pre.QUEUE_STATE = state
        p = pre.Preflight.__new__(pre.Preflight)
        p.a = SimpleNamespace()
        p.wt = wt
        p.results = []
        return p.check_cwd_exclusive()
    check("esc-review in a non-checkout ancestor ($HOME) does not own the tree",
          run([{"id": "f35f39e71313", "status": "running", "cwd": str(root)}]), pre.PASS)
    (root / ".git").mkdir()
    check("an ancestor CHECKOUT still collides", run([{"id": "x", "status": "running", "cwd": str(root)}]), pre.FAIL)
    check("the same tree still collides", run([{"id": "y", "status": "running", "cwd": str(wt)}]), pre.FAIL)


def test_auto(auto):
    # operational wait: cwd-exclusive only -> poll preflight, no refine round
    seq = [{"verdict": "NO-GO", "blockers": [{"check": "cwd-exclusive", "detail": "f35f [running]"}]},
           {"verdict": "NO-GO", "blockers": [{"check": "verify-relevance"}]}]
    calls = []
    rerun = lambda wt, req, a: (calls.append(1), (1, seq.pop(0)))[1]
    rc, data = auto.wait_out_operational_blockers("wt", [], None, 1,
                                                  {"verdict": "NO-GO", "blockers": [{"check": "cwd-exclusive", "detail": ""}]},
                                                  sleep=lambda s: None, rerun=rerun, max_wait=300, poll=30)
    check("cwd-exclusive-only NO-GO waits and re-runs preflight", len(calls), 2)
    check("...and acts on the post-wait verdict", data["blockers"][0]["check"], "verify-relevance")
    calls.clear()
    auto.wait_out_operational_blockers("wt", [], None, 1, {"verdict": "NO-GO", "blockers": [{"check": "ctx-budget"}, {"check": "cwd-exclusive"}]},
                                       sleep=lambda s: None, rerun=rerun, max_wait=300, poll=30)
    check("mixed blockers do not wait", len(calls), 0)
    check("operational blockers are never sent to the model",
          set(auto.OPERATIONAL_BLOCKERS) >= {"cwd-exclusive", "ctx-budget"}, True)
    survs = [{"file": "lib/rivian.ts", "line": 1317, "class": "cond-force-false", "mutation": "force false",
              "snippet": "if (false) return 'queued';"},
             {"file": "lib/rivian.ts", "line": 1317, "class": "compare-flip", "mutation": "=== -> !==", "snippet": "x"}]
    check("force-false survivor -> redundant line", auto.redundant_survivor_lines(survs), [("lib/rivian.ts", 1317)])
    rp = auto.refine_prompt("lib/rivian.ts", survs, [], lang="typescript")
    check("refine prompt names the redundant line", "LIKELY REDUNDANT" in rp and "lib/rivian.ts:1317" in rp, True)

    # refine guard enforced by auto-harness-check
    wt = Path(tempfile.mkdtemp())
    (wt / "TASK.md").write_text("# T\nOnly edit `lib/rivian.ts`.\n## Must contain\n- `mapServiceRequestStatus`\n")
    (wt / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    (wt / "refimpl.py").write_text("print('x')\n")
    (wt / "verify_impl.mts").write_text("// cases\n")
    (wt / "lib").mkdir()
    (wt / "lib" / "rivian.ts").write_text("export const a = 1;\n")
    subprocess.run(["git", "init", "-q"], cwd=wt)
    auto.write_harness_check(wt, "typescript")
    auto.write_refine_guard(wt, "lib/rivian.ts", survs, "typescript")
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt, capture_output=True, text=True, timeout=120)
    check("unchanged harness with survivors -> self-check FAILS 'REFINE NOT DONE'",
          (r.returncode != 0, "REFINE NOT DONE" in r.stdout), (True, True))
    (wt / "verify_impl.mts").write_text("// cases\n// + OPEN_SCHEDULED case\n")
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt, capture_output=True, text=True, timeout=120)
    check("after an edit the guard no longer fires", "REFINE NOT DONE" in r.stdout, False)
    auto.clear_refine_guard(wt)
    check("guard removed after the round", (wt / auto.REFINE_GUARD).exists(), False)
    auto.write_refine_guard(wt, "lib/rivian.ts", [], "typescript")
    check("no survivors -> no guard", (wt / auto.REFINE_GUARD).exists(), False)

    # literal freeze keeps backslashes (auto's self-check copy)
    wt2 = Path(tempfile.mkdtemp())
    for f in ("verify.sh", "refimpl.py", "verify_impl.mts"):
        (wt2 / f).write_text("x\n")
    (wt2 / "TASK.md").write_text("# T\nOnly edit `lib/r.ts`.\n## Must contain\n- `replace(/\\\\D/g, '')`\n")
    (wt2 / "check_literals.py").write_text("LITERALS = []\n")
    subprocess.run(["git", "init", "-q"], cwd=wt2)
    auto.write_harness_check(wt2, "typescript")
    subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt2, capture_output=True, text=True, timeout=120)
    ns = {}
    exec((wt2 / "check_literals.py").read_text(), ns)
    check("auto freeze keeps every backslash", ns.get("LITERALS"), [[None, "replace(/\\\\D/g, '')"]])


def test_scaffold(sc):
    wt = Path(tempfile.mkdtemp())
    (wt / "TASK.md").write_text("# T\n## Must contain\n- `replace(/\\\\D/g, '')`\n")
    (wt / "check_literals.py").write_text("LITERALS = []\n")
    sc.freeze_literals(wt)
    ns = {}
    exec((wt / "check_literals.py").read_text(), ns)
    want = [[f, l] for f, l in sc._parse_must_contain_pairs((wt / "TASK.md").read_text())]
    check("scaffold freeze == TASK.md parse (no drift)", [list(x) for x in ns.get("LITERALS", [])], want)


def main():
    test_ctx(load(QUEUE, "q_rc"))
    test_cwd(load(PRE, "pf_rc"))
    test_auto(load(AUTO, "oda_rc"))
    test_scaffold(load(SCAF, "sc_rc"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("QUEUE_SRC", "CLI looked up on PATH only", "    for p in _DARKBLOOM_BIN_CANDIDATES:\n        if os.access(str(p), os.X_OK):\n            return str(p)\n",
     ""),
    ("QUEUE_SRC", "peak replaced by any reading", "            or gb >= float(rec.get(\"gb\") or 0)):", "            or True):"),
    ("PRE_SRC", "any ancestor owns the tree", "            anc = other in mine.parents and (other / \".git\").exists()",
     "            anc = other in mine.parents"),
    ("AUTO_SRC", "no operational wait", "    while (data.get(\"verdict\") != \"GO\"\n", "    while (False\n"),
    ("AUTO_SRC", "refine guard not enforced", "        if _cur == _g.get(\"hashes\"):", "        if False:"),
    ("AUTO_SRC", "auto freeze back to string template", "    new = _re.sub(r\"^LITERALS = \\[.*?\\]$\", lambda _m: _frozen,",
     "    new = _re.sub(r\"^LITERALS = \\[.*?\\]$\", _frozen,"),
    ("SCAF_SRC", "scaffold freeze back to string template", "                        lambda _m: _frozen,\n", "                        _frozen,\n"),
]


def revert_check():
    bad = 0
    srcs = {"PRE_SRC": PRE, "AUTO_SRC": AUTO, "QUEUE_SRC": QUEUE, "SCAF_SRC": SCAF}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
