#!/usr/bin/env python3
"""Regression: preflight's Phase-2 gates, driven through the REAL CLI on temp worktrees.

  A. harness-complete: a raw scaffold (placeholders everywhere) is a NO-GO that names
     harness-complete and executes NOTHING (baseline-fails / refimpl stay unproven)
     -- rt-egift-link-s1-s4-api-route dispatched exactly such a harness.
  B. stub-aware baseline-clean (5b6b052149a7): a tracked target that is a stub in HEAD AND
     in the working tree (spelled differently) is baseline; an implementation in either
     side is still dirt.
  C. refimpl-written test files (the 'seal round baseline' leak): a modified tracked test
     file that refimpl.py names is harness, sealed by --auto-seal, never baseline dirt.
  D. fail-before/pass-after is its own check: PASS when both halves are measured.
  E. a dead refimpl anchor is caught statically.

Run: python3 test-preflight-harness-gates.py [--pre PATH]      (prints ALL PASS)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRE = HERE / "ollama-dispatch-preflight"
if "--pre" in sys.argv:
    PRE = Path(sys.argv[sys.argv.index("--pre") + 1])
SCAFFOLD = HERE / "ollama-dispatch-scaffold"
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def run_pre(wt, home, *extra):
    env = dict(os.environ, HOME=str(home), OLLAMA_DISPATCH_HOME=str(home / ".ollama-dispatch"))
    p = subprocess.run([sys.executable, str(PRE), str(wt), "--json", "--no-relevance", *extra],
                       capture_output=True, text=True, env=env, timeout=300)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        d = {}
    rows = {c.get("check") or c.get("id"): c for c in (d.get("checks") or [])}
    return d, rows


def make_repo(root, name, files):
    repo = root / f"repo-{name}"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    for rel, txt in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(txt)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    wt = root / f"wt-{name}"
    git(repo, "worktree", "add", "-q", "-b", f"dispatch/{name}", str(wt))
    return wt


TASK = ("# TASK: t\n\n## Confirmed defect\n\nobserved: f() returns 0.\n\n## Entry point\n\nt.py:1\n\n"
        "## Required change\n\nf() returns 1.\n\n## Must contain\n\n- `return 1`\n\n## Scope\n\n"
        "Only edit `t.py`; do not edit `verify.sh` or `TASK.md`.\n\nRun `bash verify.sh` after every "
        "edit until it prints VERIFY_OK.\n")
VERIFY = ('#!/bin/bash\nexport PYTHONDONTWRITEBYTECODE=1\nfails=0\npython3 -c "import t; assert t.f()==1" 2>/dev/null || fails=$((fails+1))\n'
          'echo "--- $fails failed ---"\n[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1\n')
REFIMPL = ('import pathlib, sys\nwt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")\n'
           'p = wt / "t.py"\nt = p.read_text()\nOLD = "return 0"\nNEW = "return 1"\n'
           'assert OLD in t\np.write_text(t.replace(OLD, NEW, 1))\n')


def seed(wt, creation=False, target_head=None):
    (wt / "TASK.md").write_text(TASK)
    (wt / "verify.sh").write_text(VERIFY)
    (wt / "check_literals.py").write_text("print('ok')\n")
    (wt / "refimpl.py").write_text(REFIMPL)
    (wt / ".dispatch-harness.json").write_text(json.dumps({
        "authored": ["TASK.md", "verify.sh", "check_literals.py", "refimpl.py"],
        "target": "t.py", "creation_task": creation}))


def main():
    root = Path(tempfile.mkdtemp(prefix="pf-hg-"))
    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True)

    # --- A. raw scaffold -> harness-complete NO-GO, nothing executed ------------------
    srepo = root / "srepo"
    srepo.mkdir()
    git(srepo, "init", "-q")
    git(srepo, "config", "user.email", "t@t")
    git(srepo, "config", "user.name", "t")
    (srepo / "t.py").write_text("def f():\n    return 0\n")
    git(srepo, "add", "-A")
    git(srepo, "commit", "-qm", "init")
    swt = root / "scf"
    subprocess.run([sys.executable, str(SCAFFOLD), "--label", "t1", "--repo", str(srepo), "--lang",
                    "python", "--target", "t.py", "--dest", str(swt), "--defect", "x", "--property", "y"],
                   capture_output=True, text=True, env=dict(os.environ, HOME=str(root)))
    d, rows = run_pre(swt, home, "--refimpl-cmd", "python3 refimpl.py")
    check("A raw scaffold is NO-GO", d.get("verdict"), "NO-GO")
    check("A ... blocked by harness-complete", "harness-complete" in [b["check"] for b in d.get("blockers", [])], True)
    check("A ... nothing executed: baseline-fails is UNPROVEN",
          (rows.get("baseline-fails") or {}).get("status"), "UNPR")
    check("A ... refimpl-passes is UNPROVEN, not run",
          (rows.get("refimpl-passes") or {}).get("status"), "UNPR")
    check("A ... the contract is not claimed",
          (rows.get("fail-before-pass-after") or {}).get("status"), "UNPR")

    # --- D. a complete harness: contract PASS ---------------------------------------------
    wt = make_repo(root, "ok", {"t.py": "def f():\n    return 0\n"})
    seed(wt)
    d, rows = run_pre(wt, home, "--refimpl-cmd", "python3 refimpl.py", "--auto-seal")
    check("D complete harness: harness-complete PASS", (rows.get("harness-complete") or {}).get("status"), "PASS")
    check("D ... baseline-fails PASS", (rows.get("baseline-fails") or {}).get("status"), "PASS")
    check("D ... refimpl-passes PASS", (rows.get("refimpl-passes") or {}).get("status"), "PASS")
    check("D ... fail-before-pass-after PASS", (rows.get("fail-before-pass-after") or {}).get("status"), "PASS")

    # --- E. dead refimpl anchor ------------------------------------------------------------
    wt = make_repo(root, "anchor", {"t.py": "def f():\n    return 0\n"})
    seed(wt)
    (wt / "refimpl.py").write_text(REFIMPL.replace('"return 0"', '"return 12345"'))
    d, rows = run_pre(wt, home, "--refimpl-cmd", "python3 refimpl.py", "--auto-seal")
    check("E dead refimpl anchor: harness-complete FAIL", (rows.get("harness-complete") or {}).get("status"), "FAIL")
    check("E ... names the anchor", "return 12345" in json.dumps(rows.get("harness-complete") or {}), True)

    # --- B. stub-aware baseline-clean --------------------------------------------------------
    def stub_case(name, head, work):
        w = make_repo(root, name, {"t.py": head})
        seed(w, creation=True)
        (w / "t.py").write_text(work)
        _d, r = run_pre(w, home, "--refimpl-cmd", "python3 refimpl.py", "--auto-seal")
        return (r.get("baseline-clean") or {})

    r = stub_case("stub-stub", "pass\n", '"""Stub for t.py -- implement per TASK.md."""\n')
    check("B stub in HEAD + differently-spelled stub on disk: baseline-clean PASS", r.get("status"), "PASS")
    check("B ... says so", "stub target" in (r.get("msg") or r.get("message") or json.dumps(r)), True)
    r = stub_case("stub-impl", "pass\n", "def f():\n    return 0\n")
    check("B stub in HEAD but an IMPLEMENTATION on disk is still dirt", r.get("status"), "FAIL")
    r = stub_case("impl-stub", "def f():\n    return 0\n", "pass\n")
    check("B an implementation in HEAD wiped to a stub is still dirt", r.get("status"), "FAIL")

    # --- C. a refimpl-written test file modified after a seal commit ---------------------------
    w = make_repo(root, "seal", {"t.py": "def f():\n    return 0\n", "test_t.py": "# round 1 attempt\n"})
    seed(w)
    (w / "refimpl.py").write_text(REFIMPL + "(wt / 'test_t.py').write_text('# refimpl test\\n')\n")
    (w / "test_t.py").write_text("# round 2 edit by the refine author\n")
    d, rows = run_pre(w, home, "--refimpl-cmd", "python3 refimpl.py")
    check("C refimpl-written test modified, no --auto-seal: FAIL with the SEAL fix (not checkout)",
          ((rows.get("baseline-clean") or {}).get("status"), "seal" in json.dumps(rows.get("baseline-clean") or {}).lower()),
          ("FAIL", True))
    d, rows = run_pre(w, home, "--refimpl-cmd", "python3 refimpl.py", "--auto-seal")
    check("C ... with --auto-seal baseline-clean PASS", (rows.get("baseline-clean") or {}).get("status"), "PASS")
    check("C ... and the edit was sealed, not discarded",
          git(w, "show", "HEAD:test_t.py").stdout, "# round 2 edit by the refine author\n")

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
