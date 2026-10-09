#!/usr/bin/env python3
"""Regression test: iCloud-evicted ('dataless') .git files must not kill the
scaffold's worktree add (resell-tracker 2026-10-02: "fatal: mmap failed: Operation
canceled", scaffold rc=2 twice, an empty dispatch/ branch left behind each time).

  * _ensure_git_materialized reads every dataless file under the git dir and falls
    back to `brctl download` for any still dataless; returns what is left;
  * _dataless_files walks the COMMON git dir and finds flagged files;
  * _drop_unused_branch deletes the branch a failed add left, never one a worktree
    is using (real git);
  * the scaffold materializes BEFORE the add and, on an mmap failure, drops the
    empty branch and retries once.
Run: python3 test-scaffold-dataless-git.py [--revert-check]
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
    loader = SourceFileLoader("scaf_t", str(SCAF))
    spec = importlib.util.spec_from_loader("scaf_t", loader)
    sc = importlib.util.module_from_spec(spec)
    loader.exec_module(sc)
    root = Path(tempfile.mkdtemp(prefix="dataless-")).resolve()
    repo = root / "repo"
    repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "a")
    gd = repo / ".git"
    a, b = gd / "objects" / "pack" / "x.rev", gd / "objects" / "pack" / "y.rev"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"1")
    b.write_bytes(b"2")

    evicted = {str(a), str(b)}
    sc._is_dataless = lambda p: str(p) in evicted
    found = sorted(sc._dataless_files(repo))
    check("_dataless_files finds the flagged files in the common git dir",
          found, sorted(evicted))

    reads, dls = [], []

    def read(fp):                       # reading x.rev faults it in; y.rev stays
        reads.append(fp)
        if fp == str(a):
            evicted.discard(fp)

    def download(fp):                   # brctl fallback fixes y.rev
        dls.append(fp)
        evicted.discard(fp)
    left = sc._ensure_git_materialized(repo, read=read, download=download)
    check("every dataless file is READ first", sorted(reads), sorted([str(a), str(b)]))
    check("brctl download only for what a read did not fault in", dls, [str(b)])
    check("nothing left dataless", left, [])
    evicted.add(str(a))
    left = sc._ensure_git_materialized(repo, read=lambda fp: None, download=lambda fp: None)
    check("an unfixable file is reported, not swallowed", left, [str(a)])
    evicted.clear()
    check("a clean repo is a no-op", sc._ensure_git_materialized(repo), [])

    g(repo, "branch", "dispatch/x")
    sc._drop_unused_branch(repo, "dispatch/x")
    check("the empty branch a failed add left is deleted",
          g(repo, "rev-parse", "--verify", "--quiet", "refs/heads/dispatch/x").returncode != 0, True)
    wt = root / "wt"
    g(repo, "worktree", "add", "-q", "-b", "dispatch/y", str(wt))
    sc._drop_unused_branch(repo, "dispatch/y")
    check("a branch a worktree uses is NEVER deleted",
          g(repo, "rev-parse", "--verify", "--quiet", "refs/heads/dispatch/y").returncode, 0)

    src = SCAF.read_text()
    i_mat = src.find("        _ensure_git_materialized(repo)\n        rc, out, err = sh(addcmd)")
    i_retry = src.find("if rc != 0 and _MMAP_FAIL in err:\n            # iCloud")
    i_drop = src.find("_drop_unused_branch(repo, branch)\n            _ensure_git_materialized(repo)\n            rc, out, err = sh(addcmd)")
    check("materialize runs before the add", i_mat > 0, True)
    check("an mmap failure drops the branch, re-materializes and retries once",
          0 < i_mat < i_retry < i_drop, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no read fault-in", "        try:\n            read(fp)\n        except OSError:\n            pass\n",
     ""),
    ("no brctl fallback", "            try:\n                download(fp)\n            except Exception:\n                pass\n",
     ""),
    # (git itself refuses `branch -D` on a checked-out branch, so "drop a used
    # branch" is not a reachable mutant; the cleanup itself is what must bite.)
    ("never drop the empty branch",
     '        return\n    sh(["git", "-C", str(repo), "branch", "-D", branch])\n',
     '        return\n    pass\n'),
    ("no materialize before add", "        _ensure_git_materialized(repo)\n        rc, out, err = sh(addcmd)\n        if rc != 0 and _MMAP_FAIL",
     "        rc, out, err = sh(addcmd)\n        if rc != 0 and _MMAP_FAIL"),
]


def revert_check():
    bad = 0
    src = SCAF.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SCAF_SRC": f.name},
                           capture_output=True, text=True, timeout=120)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
