#!/usr/bin/env python3
"""Guards the bug fixed 2026-10-02 (bfmr-superseded-reservations-v3 parked on
baseline-clean for 3 straight preflight rounds).

ollama-dispatch-auto writes `.refine-guard.json` into the worktree BEFORE enqueuing
each auto-refine-*-rN job and unlinks it after the round. The queue's
seal_prev_round_baseline() runs at that refine job's LAUNCH and commits every
_real_dirty_paths() entry -- and `.refine-guard.json` was not a known scaffold
basename, so it was COMMITTED (seal eac803c). auto's post-round unlink then left
the tree with ` D .refine-guard.json`: tracked-and-deleted, so preflight's
baseline-clean blocker fired on every subsequent round, whatever the harness.

Fix: `.refine-guard.json` is in the queue's _SCAFFOLD_BASENAMES (never sealed /
never counted dirty) and in the scaffold's .git/info/exclude list (invisible to
`git status` altogether).

Run:  python3 test-refine-guard-never-sealed.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
QUEUE_SRC = Path(os.environ.get("QUEUE_SRC", BIN / "ollama-queue.py"))
SCAF_SRC = Path(os.environ.get("SCAF_SRC", BIN / "ollama-dispatch-scaffold"))
FAILS = []


def _load(name, p):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(p)))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def git(root, *a):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                           "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, env=env)


def repo(tmp):
    r = Path(tmp) / "wt"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.ts").write_text("x\n")
    git(r, "add", "a.ts")
    git(r, "commit", "-qm", "init")
    return r


def main():
    q = _load("oq_rg", QUEUE_SRC)
    check("porcelain: untracked guard is not a real dirty path",
          q._real_dirty_paths("?? .refine-guard.json\n M notes.ts\n"), ["notes.ts"])

    with tempfile.TemporaryDirectory() as tmp:
        r = repo(tmp)
        (r / ".refine-guard.json").write_text('{"round": 2}\n')
        (r / "notes.ts").write_text("t\n")
        res = q.seal_prev_round_baseline({"label": "auto-refine-bfmr-x-r2", "cwd": str(r)})
        check("refine-round seal ran", bool(res and res.get("head")), True)
        tracked = git(r, "ls-files").stdout.split()
        check("seal committed the real harness edit", "notes.ts" in tracked, True)
        check("seal did NOT commit .refine-guard.json", ".refine-guard.json" in tracked, False)
        (r / ".refine-guard.json").unlink()          # auto's post-round cleanup
        check("tree clean after auto unlinks the guard", git(r, "status", "--porcelain").stdout, "")

    s = _load("ods_rg", SCAF_SRC)
    with tempfile.TemporaryDirectory() as tmp:
        r = repo(tmp)
        (r / "verify.sh").write_text("#!/bin/sh\n")
        try:
            s.seal_baseline(r)
        except SystemExit:
            pass
        excl = (r / ".git" / "info" / "exclude").read_text() if (r / ".git" / "info" / "exclude").exists() else ""
        check("scaffold seal excludes .refine-guard.json", ".refine-guard.json" in excl.split(), True)
        (r / ".refine-guard.json").write_text("{}\n")
        check("guard invisible to git status after scaffold seal",
              ".refine-guard.json" in git(r, "status", "--porcelain").stdout, False)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("queue", QUEUE_SRC, "QUEUE_SRC", '    ".refine-guard.json",\n', ""),
    ("scaffold", SCAF_SRC, "SCAF_SRC", '"auto-harness-check.py",\n                               ".refine-guard.json")',
     '"auto-harness-check.py")'),
]


def revert_check():
    bad = 0
    for name, path, envk, old, new in MUTATIONS:
        src = path.read_text()
        assert src.count(old) == 1, f"mutation anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-rg", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, envk: f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert {name} exclusion -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
