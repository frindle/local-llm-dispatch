#!/usr/bin/env python3
"""Scaffold base resolution is deterministic (smoke defect 2, 2026-10-09).

order-mismatch replay: creation_task came out true for a file that EXISTS at the base, because the
scaffold asked the source checkout's WORKING COPY, and its base came from an unlocked fetch. Pinned
against the real scaffold CLI on throwaway repos:
  1. creation is decided from the RESOLVED BASE TREE: a file only in the working copy (untracked) is
     a CREATION task; a tracked file deleted from the working copy but present at the base is NOT.
  2. the resolved base commit is recorded as base_sha in .dispatch-harness.json and equals the
     worktree's base commit.
  3. explicit --baseline-ref: no fetch (note says so), base_sha == that commit even when origin moved.
  4. default: HEAD strictly behind origin -> base_sha is origin's commit (fetch happens).
  5. the fetch takes the per-repo flock (ods-base-fetch.lock): a held lock is reported, never ignored
     (_fresh_base_ref(lock_timeout=1) -> HEAD + "lock busy" note and NO fetch).
SCAFFOLD_SRC overrides the file under test."""
import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SCAFFOLD_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def git(d, *a):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(d), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def scaffold(t, repo, label, target, *extra):
    dest = t / ("wt-" + label)
    env = {**os.environ, "HOME": str(t / "home")}
    p = subprocess.run([sys.executable, str(SRC), "--repo", str(repo), "--dest", str(dest), "--label", label,
                        "--lang", "typescript", "--ts-runner", "node-test", "--target", target, *extra],
                       capture_output=True, text=True, timeout=300, env=env, cwd=str(t))
    man = {}
    try:
        man = json.loads((dest / ".dispatch-harness.json").read_text())
    except Exception:
        pass
    return p, dest, man


def main():
    t = Path(tempfile.mkdtemp(prefix="scafbase-"))
    (t / "home").mkdir()
    try:
        origin = t / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = t / "seed"
        subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
        git(seed, "config", "user.email", "t@t"); git(seed, "config", "user.name", "t")
        (seed / "lib").mkdir()
        (seed / "lib/x.ts").write_text("export const a = 1;\n")
        git(seed, "add", "."); git(seed, "commit", "-qm", "a"); git(seed, "push", "-q", "origin", "HEAD:main")
        base_a = git(seed, "rev-parse", "HEAD")
        src = t / "src"
        subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True, capture_output=True)
        git(src, "config", "user.email", "t@t"); git(src, "config", "user.name", "t")

        # 1a. working-copy-only file -> CREATION relative to the base
        (src / "lib/wc_only.ts").write_text("export const w = 1;\n")
        p, dest, man = scaffold(t, src, "c1", "lib/wc_only.ts")
        chk("1a untracked working-copy file is a CREATION task at the base", man.get("creation_task"), True)
        # 1b. tracked at base but deleted from the working copy -> NOT creation
        (src / "lib/x.ts").unlink()
        p, dest, man = scaffold(t, src, "c2", "lib/x.ts")
        chk("1b file present at base (deleted in working copy) is NOT a creation task",
            man.get("creation_task"), False)
        chk("1b ... and the worktree has the base's file", (dest / "lib/x.ts").is_file(), True)
        git(src, "checkout", "--", "lib/x.ts")
        (src / "lib/wc_only.ts").unlink()

        # 2. base_sha recorded == worktree base commit
        p, dest, man = scaffold(t, src, "c3", "lib/x.ts")
        chk("2 base_sha recorded in manifest", man.get("base_sha"), base_a)
        chk("2 ... equals the worktree's base commit", git(dest, "rev-parse", "HEAD"), base_a)

        # 4. HEAD behind origin -> origin's commit
        (seed / "lib/y.ts").write_text("export const y = 1;\n")
        git(seed, "add", "."); git(seed, "commit", "-qm", "b"); git(seed, "push", "-q", "origin", "HEAD:main")
        base_b = git(seed, "rev-parse", "HEAD")
        p, dest, man = scaffold(t, src, "c4", "lib/y.ts")
        chk("4 default: behind origin -> base_sha is origin's commit (target exists there)",
            (man.get("base_sha"), man.get("creation_task")), (base_b, False))

        # 3. explicit --baseline-ref: no fetch, even though origin is ahead
        (seed / "lib/z.ts").write_text("export const z = 1;\n")
        git(seed, "add", "."); git(seed, "commit", "-qm", "c"); git(seed, "push", "-q", "origin", "HEAD:main")
        p, dest, man = scaffold(t, src, "c5", "lib/x.ts", "--baseline-ref", base_a)
        chk("3 explicit --baseline-ref -> base_sha is that commit", man.get("base_sha"), base_a)
        chk("3 ... and says no fetch", "no fetch" in p.stdout, True)
        chk("3 ... and origin was NOT fetched (new commit unknown locally)",
            subprocess.run(["git", "-C", str(src), "cat-file", "-e", git(seed, "rev-parse", "HEAD")],
                           capture_output=True).returncode != 0, True)

        # 5. held flock is reported, never ignored, and nothing is fetched
        ld = ld_m = SourceFileLoader("scaf_br", str(SRC))
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_br", ld))
        argv, sys.argv = sys.argv, [str(SRC)]
        try:
            ld.exec_module(m)
        finally:
            sys.argv = argv
        fh = open(src / ".git" / "ods-base-fetch.lock", "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            ref, note = m._fresh_base_ref(src, lock_timeout=1)
        finally:
            fh.close()
        chk("5 held lock -> HEAD + 'lock busy' note", (ref, "lock busy" in note), ("HEAD", True))
        chk("5 ... and no fetch happened under a busy lock",
            subprocess.run(["git", "-C", str(src), "cat-file", "-e", git(seed, "rev-parse", "HEAD")],
                           capture_output=True).returncode != 0, True)
        ref, note = m._fresh_base_ref(src)
        chk("5 lock free again -> fetches and cuts from origin", ref in ("origin/main", "origin/HEAD"), True)
    finally:
        shutil.rmtree(t, ignore_errors=True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
