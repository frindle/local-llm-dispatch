#!/usr/bin/env python3
"""Pipeline commits on dispatch/chain branches must NOT fire post-commit hooks (2026-10-02).

Live: the machine's git template (and the resell-tracker / ev-dashboard repo hooks)
install a post-commit hook that AUTO-PUSHES the current branch. Every pipeline
commit on a dispatch worktree -- scaffold seal + baseline, preflight auto-seal,
draft seal, slicer creation-target seal + chain commit, queue between-round seal --
therefore published the branch, and frindle/resell-tracker (78) + frindle/ev-dashboard
(18) are PUBLIC repos carrying dispatch/* branches full of TASK.md / fixtures /
refimpl. `--no-verify` does NOT skip post-commit; `-c core.hooksPath=/dev/null` does.
Landing pushes the default branch explicitly, so nothing relied on the hook.

Each check calls the REAL commit path in a temp repo whose .git/hooks/post-commit
writes a marker (stand-in for the push) and asserts the commit happened AND the
marker did not. The scaffold `baseline for <label>` commit lives inside main()'s
worktree-add flow, so it is pinned by source anchor.
--revert-check removes the fix at each site and requires the suite to go RED.
*_SRC env vars override the files under test."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRCS = {k: Path(os.environ.get(f"{k.upper()}_SRC") or HERE / f)
        for k, f in (("scaffold", "ollama-dispatch-scaffold"), ("preflight", "ollama-dispatch-preflight"),
                     ("draft", "ollama-dispatch-draft"), ("slice", "ollama-dispatch-slice"),
                     ("queue", "ollama-queue.py"))}
FAILS = []
NOHOOK = "core.hooksPath=/dev/null"


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(key):
    ld = SourceFileLoader(f"t_{key}", str(SRCS[key]))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(f"t_{key}", ld))
    argv = sys.argv
    sys.argv = [str(SRCS[key])]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def g(d, *a):
    # the TEST's own git calls keep hooks off so only the code under test can fire one
    return subprocess.run(["git", "-c", NOHOOK, "-C", str(d), *a], capture_output=True, text=True)


def repo(tmp, name):
    d = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=tmp))
    g(d, "init", "-q", "-b", "dispatch/x")
    g(d, "config", "user.email", "t@t"); g(d, "config", "user.name", "t")
    (d / "README").write_text("base\n")
    g(d, "add", "README"); g(d, "commit", "-qm", "base")
    hook = d / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho fired >> "{d}/.git/AUTOPUSH_FIRED"\n')
    os.chmod(hook, 0o755)
    return d


def head(d):
    return g(d, "rev-parse", "HEAD").stdout.strip()


def fired(d):
    return (d / ".git" / "AUTOPUSH_FIRED").exists()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="nopush-"))

    # control: the hook stand-in really fires on an ordinary commit (else every
    # "did not fire" below is vacuous)
    d = repo(tmp, "control")
    (d / "f").write_text("1\n")
    subprocess.run(["git", "-C", str(d), "add", "f"], capture_output=True)
    subprocess.run(["git", "-C", str(d), "commit", "--no-verify", "-qm", "c"], capture_output=True)
    chk("control: a plain `git commit --no-verify` DOES fire post-commit", fired(d), True)

    sc = load("scaffold")
    d = repo(tmp, "scaffold-seal"); before = head(d)
    (d / "TASK.md").write_text("task\n"); (d / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    sc.seal_baseline(d)
    chk("scaffold seal_baseline: committed", head(d) != before, True)
    chk("scaffold seal_baseline: post-commit (auto-push) NOT fired", fired(d), False)
    src = SRCS["scaffold"].read_text()
    i = src.find('f"baseline for {a.label}"')
    chk("scaffold worktree baseline commit runs hooks off (source anchor)",
        i > 0 and NOHOOK in src[max(0, i - 200):i], True)

    pf = load("preflight")
    d = repo(tmp, "preflight"); before = head(d)
    (d / "TASK.md").write_text("t\n")
    stub = type("S", (), {"wt": d})()
    pf.Preflight.git(stub, "add", "TASK.md")
    rc, _, _ = pf.Preflight.git(stub, "commit", "-qm", "seal dispatch harness (clean launch baseline)")
    chk("preflight auto-seal (Preflight.git commit): committed", (rc, head(d) != before), (0, True))
    chk("preflight auto-seal: post-commit NOT fired", fired(d), False)

    dr = load("draft")
    d = repo(tmp, "draft"); before = head(d)
    (d / "cases.test.ts").write_text("import { test } from 'node:test';\ntest('a', () => {});\n")
    ok, why = dr.seal_harness(d)
    chk("draft seal_harness: committed", (ok, head(d) != before), (True, True))
    chk("draft seal_harness: post-commit NOT fired", fired(d), False)

    sl = load("slice")
    d = repo(tmp, "slice-creation"); before = head(d)
    (d / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/new.ts", "creation_task": True}))
    (d / "lib").mkdir(); (d / "lib" / "new.ts").write_text("export {};\n")
    r = sl._seal_creation_target(str(d))
    chk("slice _seal_creation_target: sealed", (r, head(d) != before), ("sealed", True))
    chk("slice _seal_creation_target: post-commit NOT fired", fired(d), False)

    d = repo(tmp, "slice-chain")
    (d / "lib.ts").write_text("x\n"); g(d, "add", "lib.ts")
    r = sl._commit_deliverable(str(d), "s1", "t", "lib.ts")
    chk("slice _commit_deliverable: chain advanced", r[0], "ok")
    chk("slice _commit_deliverable: post-commit NOT fired", fired(d), False)

    q = load("queue")
    d = repo(tmp, "queue"); before = head(d)
    (d / "README").write_text("round 1 work\n")
    r = q.seal_prev_round_baseline({"cwd": str(d), "auto_fix_round": 1, "label": "x"})
    chk("queue seal_prev_round_baseline: committed", bool(r and r.get("sealed")) and head(d) != before, True)
    chk("queue seal_prev_round_baseline: post-commit NOT fired", fired(d), False)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


H = '"-c", "core.hooksPath=/dev/null", '
MUTANTS = [
    ("scaffold", "seal hooks on", f'sh(["git", {H}"-C", str(root), "commit", "-qm",',
     'sh(["git", "-C", str(root), "commit", "-qm",'),
    ("scaffold", "baseline hooks on", 'sh(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm",\n',
     'sh(["git", "commit", "-qm",\n'),
    ("preflight", "Preflight.git hooks on", f'subprocess.run(["git", {H}"-C", str(self.wt), *a],',
     'subprocess.run(["git", "-C", str(self.wt), *a],'),
    ("draft", "draft seal hooks on", f'sh(["git", {H}"-C", str(wt), "commit", "-qm",',
     'sh(["git", "-C", str(wt), "commit", "-qm",'),
    ("slice", "creation seal hooks on", f'subprocess.run(["git", {H}"-C", wt, "commit", "--no-verify",',
     'subprocess.run(["git", "-C", wt, "commit", "--no-verify",'),
    ("slice", "chain commit hooks on", 'CHAIN_COMMIT_GIT = ["git", "-c", "core.hooksPath=/dev/null"]',
     'CHAIN_COMMIT_GIT = ["git"]'),
    ("queue", "between-round seal hooks on", '["git", "-C", str(cwd), "-c", "core.hooksPath=/dev/null",\n',
     '["git", "-C", str(cwd),\n'),
]


def revert_check():
    bad = 0
    for key, name, old, new in MUTANTS:
        src = SRCS[key].read_text()
        assert src.count(old) == 1, f"anchor missing/ambiguous: {name} ({src.count(old)})"
        suffix = ".py" if SRCS[key].suffix == ".py" else f"-{SRCS[key].name}"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, f"{key.upper()}_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
