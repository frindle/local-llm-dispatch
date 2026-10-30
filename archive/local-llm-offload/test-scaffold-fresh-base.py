#!/usr/bin/env python3
"""A dispatch worktree must start from the freshest SAFE base (2026-10-02).

Live: resell-tracker landings went out from separate clones, the Desktop source
repo had not fetched them, and `worktree add ... HEAD` started new dispatches
hours stale (their landings then conflict with main). _fresh_base_ref fetches
origin and cuts from origin's default branch ONLY when HEAD is strictly behind it;
ahead/diverged/no-origin/fetch-failure keep HEAD. The node_modules mirror is then
keyed on the WORKTREE's lockfile (a newer base can carry a different lock).
--revert-check mutates the fix. SCAFFOLD_SRC env overrides the file under test."""
import hashlib, importlib.util, os, subprocess, sys, tempfile
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
    # hooks off: the machine's git template installs a post-commit AUTO-PUSH hook,
    # which would push the "local unpushed" commit this test needs to stay local.
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(d), *a],
                          check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(d, name):
    (Path(d) / name).write_text(name + "\n")
    git(d, "add", name)
    git(d, "commit", "-qm", name)


def main():
    ld = SourceFileLoader("scaf_fb", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_fb", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    t = Path(tempfile.mkdtemp(prefix="freshbase-"))
    origin = t / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = t / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    for d in (seed,):
        git(d, "config", "user.email", "t@t"); git(d, "config", "user.name", "t")
    commit(seed, "a"); git(seed, "push", "-q", "origin", "HEAD:main")
    src = t / "src"
    subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True, capture_output=True)
    git(src, "config", "user.email", "t@t"); git(src, "config", "user.name", "t")

    ref, note = m._fresh_base_ref(src)
    chk("HEAD == origin/main -> HEAD", ref, "HEAD")
    commit(seed, "b"); git(seed, "push", "-q", "origin", "HEAD:main")      # landing elsewhere
    ref, note = m._fresh_base_ref(src)
    chk("HEAD strictly BEHIND origin (unfetched landing) -> origin's default branch",
        ref in ("origin/main", "origin/HEAD"), True)
    chk("...and that ref resolves to the landed commit",
        git(src, "rev-parse", ref), git(seed, "rev-parse", "HEAD"))
    git(src, "merge", "-q", "--ff-only", "origin/main"); commit(src, "local")
    chk("HEAD AHEAD of origin (local unpushed work) -> HEAD", m._fresh_base_ref(src)[0], "HEAD")
    commit(seed, "c"); git(seed, "push", "-q", "origin", "HEAD:main")
    chk("HEAD DIVERGED from origin -> HEAD (never drop local work)", m._fresh_base_ref(src)[0], "HEAD")
    git(src, "remote", "set-url", "origin", str(t / "nope.git"))
    chk("fetch fails -> HEAD", m._fresh_base_ref(src)[0], "HEAD")

    # mirror keyed on the WORKTREE lockfile
    nm = t / "srcrepo" / "node_modules"
    nm.mkdir(parents=True)
    (nm.parent / "package-lock.json").write_text('{"old": 1}')
    wt = t / "wt"
    wt.mkdir()
    (wt / "package-lock.json").write_text('{"new": 2}')
    m.NM_CACHE = t / "cache"
    want_key = hashlib.sha1(b'{"new": 2}').hexdigest()[:12]
    (m.NM_CACHE / f"srcrepo-{want_key}" / "node_modules").mkdir(parents=True)
    path, how = m._local_node_modules_mirror(nm, lock_dir=wt)
    chk("mirror keyed on the WORKTREE lockfile, not the source checkout's",
        (path.parent.name if path else None, how), (f"srcrepo-{want_key}", "cached"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("always HEAD (old behaviour)", "    if behind:\n        return remote,", "    if False:\n        return remote,"),
    ("remote even when ahead/diverged", '    return "HEAD", f"HEAD {head[:8]} is ahead of/diverged from {remote} -- keeping HEAD"',
     '    return remote, "x"'),
    ("mirror keyed on source lock", "    pkg_dir = Path(lock_dir) if lock_dir and (Path(lock_dir) / \"package-lock.json\").is_file() else repo\n", "    pkg_dir = repo\n"),
]


def revert_check():
    src, bad = SRC.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-scaffold", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SCAFFOLD_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
