#!/usr/bin/env python3
"""Unit tests for dispatch_harness_gates.py (Phase 2, 2026-10-08).

Each block is the regression for one escalation class; the placeholder fixtures are
GENUINE scaffold output (ollama-dispatch-scaffold run on a temp repo), not hand-typed
look-alikes.

Run: python3 test-harness-gates.py [--mod PATH]     (prints ALL PASS)
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE / "dispatch_harness_gates.py"
if "--mod" in sys.argv:
    MOD = Path(sys.argv[sys.argv.index("--mod") + 1])
SCAFFOLD = HERE / "ollama-dispatch-scaffold"
sp = importlib.util.spec_from_file_location("dispatch_harness_gates", MOD)
hg = importlib.util.module_from_spec(sp)
sp.loader.exec_module(hg)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def make_scaffold(root: Path):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "app" / "api").mkdir(parents=True)
    (repo / "app/api/route.ts").write_text("export function handler() { return 1; }\n")
    (repo / "package.json").write_text('{"name":"x","scripts":{"test":"node --test"}}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    dest = root / "scf"
    p = subprocess.run([sys.executable, str(SCAFFOLD), "--label", "t1", "--repo", str(repo),
                        "--lang", "typescript", "--target", "app/api/route.ts", "--dest", str(dest),
                        "--defect", "x", "--property", "y"], capture_output=True, text=True,
                       env=dict(os.environ, HOME=str(root)))
    return dest if dest.is_dir() else None


def main():
    root = Path(tempfile.mkdtemp(prefix="hg-unit-"))
    wt = make_scaffold(root)
    check("scaffold produced a worktree", wt is not None, True)

    # --- 1. a REAL scaffold is incomplete, with every placeholder family named -------
    ok, fs = hg.harness_complete(wt)
    txt = " | ".join(fs)
    check("1 raw scaffold is NOT harness-complete", ok, False)
    check("1 ... TASK.md TODO placeholder named", "TASK.md still carries" in txt, True)
    check("1 ... refimpl OLD='TODO' named", "refimpl.py still carries" in txt, True)
    check("1 ... fixture guard case (SCAFFOLD_INCOMPLETE) named", "scaffold guard case" in txt, True)
    check("1 ... missing real must_contain literal named", "no REAL `## Must contain` literal" in txt, True)
    check("1 ... dead refimpl anchor named", "refimpl anchor" in txt and "can never pass" in txt, True)

    # --- 2. each defect alone is enough -------------------------------------------------
    task_ok = ("# TASK\n\n## Must contain\n\n- `export function handler(`\n- in app/api/route.ts: `zzTok`\n")
    ref_ok = 'OLD = "return 1;"\nNEW = "return 2; // zzTok"\n'
    fx_ok = "import test from 'node:test';\ntest('a', () => {});\n"
    check("2 task TODO alone", len(hg.placeholder_findings("- `TODO -- a literal`", ref_ok, {"v": fx_ok})), 1)
    check("2 refimpl TODO alone", len(hg.placeholder_findings(task_ok, 'OLD = """TODO -- x"""', {"v": fx_ok})), 1)
    check("2 fixture guard alone",
          len(hg.placeholder_findings(task_ok, ref_ok,
              {"v": "test('x', () => { assert.fail('SCAFFOLD_INCOMPLETE: author'); });"})), 1)
    check("2 a header COMMENT mentioning SCAFFOLD_INCOMPLETE is not a placeholder",
          hg.placeholder_findings(task_ok, ref_ok, {"v": "// fails (SCAFFOLD_INCOMPLETE) until...\n" + fx_ok}), [])
    check("2 clean harness has no findings", hg.placeholder_findings(task_ok, ref_ok, {"v": fx_ok}), [])

    # --- 3. a completed harness passes; mutate one thing at a time ---------------------
    (wt / "TASK.md").write_text(task_ok + "\nOnly edit `app/api/route.ts`.\n")
    (wt / "refimpl.py").write_text(
        "import pathlib,sys\nwt=pathlib.Path(sys.argv[1])\np=wt/'app/api/route.ts'\nt=p.read_text()\n"
        'OLD = "return 1;"\nNEW = "return 2; // zzTok"\nassert OLD in t\np.write_text(t.replace(OLD,NEW,1))\n')
    (wt / "verify.test.ts").write_text(fx_ok)
    ok, fs = hg.harness_complete(wt)
    check("3 completed harness is complete", (ok, fs), (True, []))
    (wt / "refimpl.py").write_text((wt / "refimpl.py").read_text().replace('"return 1;"', '"return 99;"', 1))
    ok, fs = hg.harness_complete(wt)
    check("3 refimpl anchor absent from the target -> incomplete", ok, False)
    check("3 ... names the anchor", any("return 99;" in f for f in fs), True)
    (wt / "refimpl.py").write_text((wt / "refimpl.py").read_text().replace('"return 99;"', '"return 1;"', 1))
    (wt / "TASK.md").write_text("# TASK\n\n## Must contain\n\n- `TODO -- a literal token`\n")
    ok, fs = hg.harness_complete(wt)
    check("3 only-placeholder must_contain -> incomplete", ok, False)
    # creation task: anchor check is skipped (refimpl writes the whole target)
    (wt / "TASK.md").write_text(task_ok)
    (wt / "refimpl.py").write_text('OLD = "nowhere"\n')
    man = json.loads((wt / ".dispatch-harness.json").read_text())
    man["creation_task"] = True
    (wt / ".dispatch-harness.json").write_text(json.dumps(man))
    check("3 creation task skips the anchor check", hg.refimpl_anchor_findings(wt, "app/api/route.ts"), [])

    # --- 4. stubs ------------------------------------------------------------------------
    for name, t, want in [("export {};", "export {};\n", True), ("empty", "", True),
                          ("whitespace", "  \n\n", True),
                          ("creation stub", "// Stub for lib/x.ts -- implement per TASK.md.\nexport {};\n", True),
                          ("comment only", "// nothing yet\n/* block */\n", True),
                          ("module.exports", "module.exports = {};\n", True),
                          ("py docstring", '"""Stub."""\n', True),
                          ("py pass", "pass\n", True),
                          ("real function", "export function a() { return 1; }\n", False),
                          ("export const", "export const A = 1;\n", False),
                          ("py def", "def a():\n    return 1\n", False)]:
        check(f"4 is_stub_text: {name}", hg.is_stub_text(t), want)

    # --- 5. literal satisfiability (8a4941aa8521 / a580c8793aa7) -------------------------
    tgt = "export function syncResult() {}\nconst MANUAL_SYNC = 1;\n"
    spec = "Add bfmrPushFailureMessage(err) and keep syncResult."
    f = hg.literal_findings(["MANUAL_SYNC_INIT", "syncResult", "bfmrPushFailureMessage"], tgt, spec)
    check("5 literal with 0 occurrences everywhere is reported", [x[0] for x in f], ["MANUAL_SYNC_INIT"])
    check("5 literal named only in the spec is derivable", hg.literal_findings(["bfmrPushFailureMessage"], tgt, spec), [])
    check("5 literal introduced by the refimpl is satisfiable",
          hg.literal_findings(["MANUAL_SYNC_INIT"], tgt, spec, introduced_text="x = 'MANUAL_SYNC_INIT'"), [])
    check("5 quote-variant counts", hg.count_occurrences("'a'", 'x = "a"'), 1)

    # --- 6. fail-before/pass-after contract -----------------------------------------------
    P, F, U = "PASS", "FAIL", "UNPROVEN"
    check("6 both measured -> PASS", hg.contract_status([{"id": "baseline-fails", "status": P}, {"id": "refimpl-passes", "status": P}])[0], "PASS")
    check("6 green at baseline -> FAIL", hg.contract_status([{"id": "baseline-fails", "status": F}, {"id": "refimpl-passes", "status": P}])[0], "FAIL")
    check("6 refimpl red -> FAIL", hg.contract_status([{"id": "baseline-fails", "status": P}, {"id": "refimpl-passes", "status": F}])[0], "FAIL")
    check("6 waived refimpl -> UNPROVEN", hg.contract_status([{"id": "baseline-fails", "status": P}])[0], "UNPROVEN")

    # --- 7. bounded refine budget ----------------------------------------------------------
    check("7 refine below cap", hg.refine_budget(0), ("refine", 1))
    check("7 refine at cap-1", hg.refine_budget(hg.HARNESS_REFINE_CAP - 1)[0], "refine")
    check("7 respec at the cap", hg.refine_budget(hg.HARNESS_REFINE_CAP)[0], "respec")

    # --- 8. decision log ---------------------------------------------------------------------
    lp = root / "dec.jsonl"
    hg.log_auto_decision("go-continue", "lbl", "enqueue", "why", path=lp, job="j1")
    rec = json.loads(lp.read_text().splitlines()[0])
    check("8 decision record carries kind/label/decision/extra",
          (rec["kind"], rec["label"], rec["decision"], rec["job"]), ("go-continue", "lbl", "enqueue", "j1"))

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
