#!/usr/bin/env python3
"""Regression test: a slice worktree cleared while still REGISTERED must not wedge
the next authoring attempt (Rivian s3, 2026-10-02: AUTO rc=2 "git worktree add
failed: fatal: not a git repository: (null)" -> escalated on the first attempt).

  * ollama-dispatch-slice remove_worktree(): when `git worktree remove` fails and it
    falls back to rm -rf, the registration is pruned (no prunable entry is left);
  * ollama-dispatch-scaffold prunes before it inspects/creates the branch worktree.
Real git, temp repos. Run: python3 test-stale-worktree-prune.py [--revert-check]
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
SCAF = Path(os.environ.get("SCAF_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def main():
    loader = SourceFileLoader("slice_t", str(SLICE))
    spec = importlib.util.spec_from_loader("slice_t", loader)
    sl = importlib.util.module_from_spec(spec)
    loader.exec_module(sl)
    root = Path(tempfile.mkdtemp(prefix="stalewt-"))
    repo = root / "repo"
    repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "a")
    chain = root / "chain"
    g(repo, "worktree", "add", "-q", "-b", "chain", str(chain))
    wt = root / "wt-s3"
    g(chain, "worktree", "add", "-q", "-b", "dispatch/s3", str(wt))
    # make `git worktree remove` FAIL (as it did live) so the rm -rf fallback runs:
    # a worktree whose .git file is unreadable cannot be removed by git.
    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    sl.remove_worktree(str(chain), str(wt))
    check("worktree dir gone", wt.exists(), False)
    lst = g(chain, "worktree", "list", "--porcelain").stdout
    check("no prunable registration left behind", "prunable" in lst or str(wt) in lst, False)
    g(chain, "branch", "-D", "dispatch/s3")
    r = g(chain, "worktree", "add", "-q", "-b", "dispatch/s3", str(wt), "HEAD")
    check("the next attempt's worktree add succeeds", r.returncode, 0)
    src = SCAF.read_text()
    i_prune = src.find('sh(["git", "-C", str(repo), "worktree", "prune"])\n        if not a.force:')
    i_add = src.find('addcmd = ["git", "-C", str(repo), "worktree", "add",')
    check("scaffold prunes before the branch check and worktree add", 0 <= i_prune < i_add, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("SLICE_SRC", "slicer rm -rf without prune",
     '    subprocess.run(["git", "-C", cwt, "worktree", "prune"], capture_output=True, text=True)\n', ""),
    ("SCAF_SRC", "scaffold no early prune",
     '        sh(["git", "-C", str(repo), "worktree", "prune"])\n        if not a.force:',
     '        if not a.force:'),
]


def revert_check():
    bad = 0
    srcs = {"SLICE_SRC": SLICE, "SCAF_SRC": SCAF}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=120)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
