#!/usr/bin/env python3
"""Regression test: `ollama-dispatch-preflight --auto-seal` must seal a CREATION
task's target stub into the launch baseline, exactly like
`ollama-dispatch-scaffold --seal-baseline` and the slicer's _seal_creation_target.

Found by pipeline-canary soak seed 4 (2026-10-06): the s2-fmt authoring job died
under an injected model fault, leaving the untracked creation target lib/fmt.ts
holding an IMPLEMENTATION. auto's preflight round ran with --auto-seal, which
committed only the harness files, so (round 1) the verify PASSED at baseline
(baseline-fails NO-GO), a later revert deleted the untracked file, and (rounds
2-4) target-parses NO-GO'd "target ABSENT and TASK.md does not declare a creation
task" until the no-progress abort escalated the slice.

Asserted by behaviour, against the real preflight CLI on a linked worktree:
  1. clobbered creation target (untracked, holds an impl) -> after --auto-seal it
     is TRACKED at HEAD as the stub, and the impl is preserved outside the tree.
  2. creation target missing from disk -> re-seeded and TRACKED as the stub.
  3. creation target already an untracked stub -> TRACKED, content unchanged.
  4. EDIT task (creation_task false) with a tracked target the model may not
     touch -> target content and tracked state unchanged by the seal.
  5. without --auto-seal nothing is committed (the check only reports).

Run: python3 test-preflight-autoseal-creation-target.py   (PRE_SRC=<file> to test a copy)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRE = Path(os.environ.get("PRE_SRC") or HERE / "ollama-dispatch-preflight")
FAILS = []
IMPL = "export function formatRange(lo: number, hi: number): string {\n  return `[${lo}, ${hi}]`;\n}\n"


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def make_wt(root: Path, name: str, creation: bool, target_content):
    repo = root / f"repo-{name}"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "lib").mkdir()
    (repo / "lib" / "calc.ts").write_text("export const one = 1;\n")
    if not creation:
        (repo / "lib" / "fmt.ts").write_text("export const fmt = 0;\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    wt = root / f"wt-{name}"
    git(repo, "worktree", "add", "-q", "-b", f"dispatch/{name}", str(wt))
    (wt / "TASK.md").write_text(
        "# TASK: t\n\n## Required change\n\nformatRange(lo, hi) returns [lo, hi].\n\n"
        "## Must contain\n\n- `export function formatRange(`\n\n## Scope\n\n"
        "Only edit `lib/fmt.ts`; do not edit `verify.sh` or `TASK.md`.\n")
    (wt / "verify.sh").write_text("#!/bin/bash\necho nope; exit 1\n")
    (wt / "check_literals.py").write_text("print('ok')\n")
    (wt / "refimpl.py").write_text("print('refimpl')\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({
        "authored": ["TASK.md", "verify.sh", "check_literals.py", "refimpl.py"],
        "target": "lib/fmt.ts", "creation_task": creation}))
    t = wt / "lib" / "fmt.ts"
    if target_content is None:
        if creation and t.exists():
            t.unlink()
    elif creation:
        t.write_text(target_content)
    return wt


def run_pre(wt, home, seal=True):
    env = dict(os.environ, HOME=str(home), OLLAMA_DISPATCH_HOME=str(home / ".ollama-dispatch"))
    cmd = [sys.executable, str(PRE), str(wt), "--json", "--refimpl-cmd", "python3 refimpl.py"] + (["--auto-seal"] if seal else [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)


def tracked(wt, rel):
    return git(wt, "ls-files", "--error-unmatch", "--", rel).returncode == 0


def head_text(wt, rel):
    p = git(wt, "show", f"HEAD:{rel}")
    return p.stdout if p.returncode == 0 else None


def is_stub(txt):
    return txt is not None and "implement per TASK.md" in txt and "formatRange" not in txt


def main():
    root = Path(tempfile.mkdtemp(prefix="pf-autoseal-creation-"))
    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True)

    # 1. clobbered creation target
    wt = make_wt(root, "clobbered", True, IMPL)
    run_pre(wt, home)
    check("1 clobbered creation target is tracked after --auto-seal", tracked(wt, "lib/fmt.ts"), True)
    check("1 ... and HEAD holds the STUB, not the implementation", is_stub(head_text(wt, "lib/fmt.ts")), True)
    kept = [p for p in (home / ".ollama-dispatch").rglob("*") if p.is_file()
            and "presealed" in p.parts and p.read_text(errors="replace") == IMPL]
    check("1 ... and the implementation was preserved outside the tree", len(kept) >= 1, True)
    check("1 ... and harness files sealed too", tracked(wt, "TASK.md") and tracked(wt, "verify.sh"), True)

    # 2. creation target missing
    wt = make_wt(root, "missing", True, None)
    run_pre(wt, home)
    check("2 missing creation target is re-seeded and tracked", tracked(wt, "lib/fmt.ts"), True)
    check("2 ... as the stub", is_stub(head_text(wt, "lib/fmt.ts")), True)

    # 3. creation target already a stub
    stub = "// Stub for lib/fmt.ts -- implement per TASK.md.\nexport {};\n"
    wt = make_wt(root, "stub", True, stub)
    run_pre(wt, home)
    check("3 stub creation target is tracked", tracked(wt, "lib/fmt.ts"), True)
    check("3 ... content unchanged", head_text(wt, "lib/fmt.ts"), stub)

    # 4. edit task: tracked target untouched
    wt = make_wt(root, "edit", False, None)
    before = head_text(wt, "lib/fmt.ts")
    run_pre(wt, home)
    check("4 edit-task target unchanged at HEAD", head_text(wt, "lib/fmt.ts"), before)
    check("4 edit-task target unchanged on disk", (wt / "lib" / "fmt.ts").read_text(), before)
    log = git(wt, "log", "--format=%s", "-n", "5").stdout
    check("4 no creation-target seal commit on an edit task", "creation" in log.lower(), False)

    # 5. no --auto-seal: nothing committed
    wt = make_wt(root, "noseal", True, IMPL)
    run_pre(wt, home, seal=False)
    check("5 without --auto-seal the target is not committed", tracked(wt, "lib/fmt.ts"), False)

    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
