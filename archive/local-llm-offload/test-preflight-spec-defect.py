#!/usr/bin/env python3
"""Regression (real preflight CLI, temp worktrees, 2026-10-09):

  A. already-satisfied: every Must-contain literal already at base AND the verify green =
     structured `SPEC_DEFECT: already satisfied at base` (check `already-satisfied`) on top of
     baseline-fails FAIL.
  B. baseline-red classification: an assertion failure is red; a bare import/crash is NOT
     ('red for the wrong reason' = baseline-fails FAIL); a creation task over a stub, and an
     edit task whose spec NAMES the missing symbol, may fail on the missing module/export.
     (Creation-task stubs keep working -- see also test-preflight-autoseal-creation-target.py.)
  C. spec-defect check: creation task over a stub passes; explicit modify wording exempts.

Run: python3 test-preflight-spec-defect.py [--pre PATH]      (prints ALL PASS)
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
    d = json.loads(p.stdout)
    return d, {c.get("check"): c for c in d.get("checks", [])}


def make_wt(root, name, base_src, task, verify_body, creation=False):
    repo = root / f"repo-{name}"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "t.py").write_text(base_src)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    wt = root / f"wt-{name}"
    git(repo, "worktree", "add", "-q", "-b", f"dispatch/{name}", str(wt))
    (wt / "TASK.md").write_text(task)
    (wt / "verify.sh").write_text(
        "#!/bin/bash\nexport PYTHONDONTWRITEBYTECODE=1\nfails=0\n" + verify_body +
        'echo "--- $fails failed ---"\n[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "check_literals.py").write_text("print('ok')\n")
    # no literal anchors: this test is about the BASELINE verdict, not refimpl anchors
    (wt / "refimpl.py").write_text("import sys\nprint('refimpl noop')\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({
        "authored": ["TASK.md", "verify.sh", "check_literals.py", "refimpl.py"],
        "target": "t.py", "creation_task": creation}))
    return wt


def task(required, literal="return 1"):
    return ("# TASK: t\n\n## Confirmed defect\n\nobserved.\n\n## Entry point\n\nt.py:1\n\n"
            f"## Required change\n\n{required}\n\n## Must contain\n\n- `{literal}`\n\n## Scope\n\n"
            "Only edit `t.py`; do not edit `verify.sh` or `TASK.md`.\n\nRun `bash verify.sh` after every "
            "edit until it prints VERIFY_OK.\n")


ASSERT_V = 'python3 -c "import t; assert t.f()==1, \'f() must return 1\'" || fails=$((fails+1))\n'


def main():
    root = Path(tempfile.mkdtemp(prefix="pf-sd-"))
    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True)
    pre = ("--no-refimpl",)

    # A. already satisfied at base
    wt = make_wt(root, "sat", "def f():\n    return 1\n", task("f() returns 1."), ASSERT_V)
    d, r = run_pre(wt, home, *pre)
    check("A already satisfied: NO-GO", d["verdict"], "NO-GO")
    check("A ... already-satisfied check FAILs with the structured SPEC_DEFECT",
          ((r.get("already-satisfied") or {}).get("status"),
           (r.get("already-satisfied") or {}).get("message", "").startswith("SPEC_DEFECT: already satisfied at base")),
          ("FAIL", True))
    check("A ... baseline-fails FAIL too", (r.get("baseline-fails") or {}).get("status"), "FAIL")
    wt = make_wt(root, "sat2", "def f():\n    return 0\n", task("f() returns 1."), ASSERT_V)
    d, r = run_pre(wt, home, *pre)
    check("A a genuinely red baseline is NOT flagged already-satisfied", "already-satisfied" in r, False)
    check("A ... and baseline-fails PASSes as an assertion red",
          ((r.get("baseline-fails") or {}).get("status"), "red = assertion" in (r.get("baseline-fails") or {}).get("message", "")),
          ("PASS", True))

    # B. classification
    wt = make_wt(root, "crash", "def f():\n    return 0\n", task("f() returns 1."),
                 'python3 -c "import tt_missing_mod" || fails=$((fails+1))\n')
    d, r = run_pre(wt, home, *pre)
    check("B edit task, verify crashes on an unrelated import: baseline-fails FAIL",
          (r.get("baseline-fails") or {}).get("status"), "FAIL")
    check("B ... says WRONG reason", "WRONG reason" in (r.get("baseline-fails") or {}).get("message", ""), True)
    check("B ... and the contract is FAIL (a crash is not 'fails before')",
          (r.get("fail-before-pass-after") or {}).get("status"), "FAIL")

    wt = make_wt(root, "syn", "def f():\n    return 0\n", task("f() returns 1."),
                 'python3 -c "def (" || fails=$((fails+1))\n')
    d, r = run_pre(wt, home, *pre)
    check("B a SyntaxError-only red does not count", (r.get("baseline-fails") or {}).get("status"), "FAIL")

    miss = 'python3 -c "import t; t.new_helper()" || fails=$((fails+1))\n'
    wt = make_wt(root, "unnamed", "def f():\n    return 0\n", task("f() returns 1."), miss)
    d, r = run_pre(wt, home, *pre)
    check("B edit task: missing attribute the spec never names = wrong-reason red",
          (r.get("baseline-fails") or {}).get("status"), "FAIL")
    wt = make_wt(root, "named", "def f():\n    return 0\n",
                 task("add a new function new_helper() that returns 1.", literal="def new_helper"), miss)
    d, r = run_pre(wt, home, *pre)
    check("B edit task: missing attribute the spec NAMES = expected red",
          ((r.get("baseline-fails") or {}).get("status"), "expected-missing" in (r.get("baseline-fails") or {}).get("message", "")),
          ("PASS", True))

    stub = '"""Stub for t.py -- implement per TASK.md."""\n'
    cverify = 'python3 -c "import t; t.f()" || fails=$((fails+1))\n'
    wt = make_wt(root, "creation", stub, task("create t.py with f() returning 1."), cverify, creation=True)
    d, r = run_pre(wt, home, *pre)
    check("B CREATION task over a stub: missing export is expected red (not broken)",
          ((r.get("baseline-fails") or {}).get("status"), "expected-missing" in (r.get("baseline-fails") or {}).get("message", "")),
          ("PASS", True))
    check("C ... and spec-defect PASSes (a stub at base is a baseline)", (r.get("spec-defect") or {}).get("status"), "PASS")
    wt = make_wt(root, "creation-assert", stub, task("create t.py with f() returning 1."), ASSERT_V, creation=True)
    d, r = run_pre(wt, home, *pre)
    check("B CREATION task, assertion red stays red", (r.get("baseline-fails") or {}).get("status"), "PASS")

    # C. spec-defect + explicit modify wording
    real = "def f():\n    return 0\n"
    wt = make_wt(root, "exists", real, task("create t.py with f() returning 1."), ASSERT_V, creation=True)
    d, r = run_pre(wt, home, *pre)
    check("C creation task over a REAL base file: spec-defect FAIL, nothing executed",
          ((r.get("spec-defect") or {}).get("status"), (r.get("baseline-fails") or {}).get("status")), ("FAIL", "UNPR"))
    wt = make_wt(root, "modify", real, task("update the existing t.py so f() returns 1."), ASSERT_V, creation=True)
    d, r = run_pre(wt, home, *pre)
    check("C explicit modify wording exempts the same manifest", (r.get("spec-defect") or {}).get("status"), "PASS")

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
