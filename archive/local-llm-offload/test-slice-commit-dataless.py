#!/usr/bin/env python3
"""Regression test: a converged slice must land even when the chain's git files are
iCloud-evicted (ev-service s3, 2026-10-02: verify green + gate GO, then
"fatal: could not open .../COMMIT_EDITMSG: Resource deadlock avoided", rc=128, the
slice recorded FAILED and queued for a wipe-and-re-author).

  * _commit_deliverable retries ONCE after materializing when git fails with an
    eviction symptom, and the commit lands;
  * materialize_git_dirs reads exactly the dataless files of the worktree's OWN git dir
    and the common dir -- never another worktree's private dir;
  * an ordinary commit failure (no eviction symptom) is NOT retried and still errors.
A PATH-shimmed `git` fails the first commit with the live error text.
Run: python3 test-slice-commit-dataless.py [--revert-check]
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []
REAL_GIT = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    loader = SourceFileLoader("slice_cd", str(SRC))
    spec = importlib.util.spec_from_loader("slice_cd", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def g(cwd, *a):
    return subprocess.run([REAL_GIT, "-C", str(cwd), *a], capture_output=True, text=True)


def main():
    m = load()
    root = Path(tempfile.mkdtemp(prefix="slcd-")).resolve()
    repo, wt, other = root / "repo", root / "chain", root / "other"
    repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    (repo / "t.ts").write_text("a\n")
    g(repo, "add", "t.ts")
    g(repo, "commit", "-qm", "base")
    g(repo, "worktree", "add", "-q", "-b", "chain", str(wt))
    g(repo, "worktree", "add", "-q", "-b", "other", str(other))

    # --- shim: first commit fails like the live run
    shim = root / "shim"
    shim.mkdir()
    marker = root / "fail-once"
    msg = root / "fail-msg"
    (shim / "git").write_text(
        "#!/bin/bash\n"
        f'if [[ " $* " == *" commit "* ]] && [ -f "{marker}" ]; then rm -f "{marker}"; '
        f'cat "{msg}" >&2; exit 128; fi\n'
        f'exec "{REAL_GIT}" "$@"\n')
    (shim / "git").chmod(0o755)
    os.environ["PATH"] = f"{shim}:{os.environ['PATH']}"

    reads = []
    m.materialize_git_dirs_orig = m.materialize_git_dirs
    m.materialize_git_dirs = lambda w: (reads.append(w) or [])

    (wt / "t.ts").write_text("b\n")
    g(wt, "add", "t.ts")
    marker.write_text("1")
    msg.write_text(f"fatal: could not open '{repo}/.git/worktrees/chain/COMMIT_EDITMSG': "
                   "Resource deadlock avoided\n")
    res, info = m._commit_deliverable(str(wt), "s3", "pick", "t.ts")
    check("an eviction failure is retried after materializing, and LANDS", res, "ok")
    check("...materialize was called for the chain worktree", reads, [str(wt)])
    check("...the commit is on the chain", g(wt, "log", "-1", "--format=%s").stdout.strip(), "slice s3: pick")

    reads.clear()
    (wt / "t.ts").write_text("c\n")
    g(wt, "add", "t.ts")
    marker.write_text("1")
    msg.write_text("error: pre-commit hook rejected\n")
    res, info = m._commit_deliverable(str(wt), "s4", "x", "t.ts")
    check("an ordinary failure is NOT retried", (res, reads), ("error", []))

    # --- materialize_git_dirs scope (dataless simulated)
    m.materialize_git_dirs = m.materialize_git_dirs_orig
    own_gd = Path(g(wt, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    other_gd = Path(g(other, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    common = Path(g(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    evicted = {str(own_gd / "COMMIT_EDITMSG"), str(other_gd / "HEAD"), str(common / "config")}
    (own_gd / "COMMIT_EDITMSG").write_text("x\n")
    m._is_dataless = lambda p: str(p) in evicted
    got = []
    left = m.materialize_git_dirs(str(wt), read=lambda fp: (got.append(fp), evicted.discard(fp)),
                                  download=lambda fp: None)
    check("materialize reads the chain's own git dir + the common dir only",
          sorted(got), sorted([str(own_gd / "COMMIT_EDITMSG"), str(common / "config")]))
    check("...never another worktree's private dir", str(other_gd / "HEAD") in got, False)
    check("...and reports nothing left dataless", left, [])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no retry", "    if cp.returncode != 0 and any(x in _out for x in _DATALESS_SYMPTOMS):",
     "    if False:"),
    ("retry everything", "    if cp.returncode != 0 and any(x in _out for x in _DATALESS_SYMPTOMS):",
     "    if cp.returncode != 0:"),
    ("walk other worktrees", '                    dn[:] = [x for x in dn if x != "worktrees"]',
     "                    pass"),
    ("common dir skipped", '        for flag in ("--git-dir", "--git-common-dir"):', '        for flag in ("--git-dir",):'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = Path(tempfile.mkdtemp(prefix="slcdmut-"))
        dst = d / "ollama-dispatch-slice"
        dst.write_text(src.replace(old, new))
        for sib in os.listdir(HERE):
            if sib.endswith(".py"):
                try:
                    os.symlink(HERE / sib, d / sib)
                except OSError:
                    pass
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": str(dst)},
                           capture_output=True, text=True, timeout=120)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
