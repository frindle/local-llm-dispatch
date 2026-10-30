#!/usr/bin/env python3
"""Worktree node_modules must NOT link into an iCloud-synced repo (2026-10-02).
Rivian s5 refine 545697b42a35 hit "VERIFY TIMED OUT (300s)": node_modules linked
to ~/Desktop/.../ev-dashboard/node_modules, whose files iCloud Optimize Storage had
evicted; cold tsc spent 23s of 28.6s in I/O and repeated verifies blew the budget.
The scaffold now links a local mirror keyed on package-lock.json instead.
--revert-check mutates ollama-dispatch-scaffold (SCAFFOLD_SRC env) and requires RED."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SCAF = Path(os.environ.get("SCAFFOLD_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    d = Path(tempfile.mkdtemp(prefix="nmm-")).resolve()
    icloud, local, cache = d / "icloud", d / "local", d / "cache"
    os.environ["DISPATCH_ICLOUD_ROOTS"] = str(icloud)
    os.environ["DISPATCH_NM_CACHE"] = str(cache)
    # a stand-in npm: `npm ci` installs from the lockfile it finds in cwd, and
    # logs every call so the test can prove the evicted source was NOT copied.
    fb = d / "fakebin"
    fb.mkdir()
    (fb / "npm").write_text('#!/bin/sh\necho "$PWD $*" >> "%s"\n'
                            '[ "$1" = ci ] && [ -f package-lock.json ] || exit 1\n'
                            'mkdir -p node_modules/pkg && echo "module.exports=1" > node_modules/pkg/index.js\n'
                            % (d / "npm.log"))
    (fb / "npm").chmod(0o755)
    os.environ["PATH"] = f"{fb}{os.pathsep}{os.environ['PATH']}"
    ld = SourceFileLoader("scaf_nmm", str(SCAF))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_nmm", ld))
    sys.argv = [str(SCAF)]
    ld.exec_module(m)

    def repo(base, lock):
        r = base / "app"
        (r / "node_modules" / "pkg").mkdir(parents=True)
        (r / "node_modules" / "pkg" / "index.js").write_text("module.exports=1\n")
        (r / "node_modules" / ".prisma" / "client").mkdir(parents=True)
        (r / "node_modules" / ".prisma" / "client" / "index.js").write_text("gen\n")
        (r / "node_modules" / "only-in-source.txt").write_text("x\n")
        (r / "package-lock.json").write_text(lock)
        return r

    ir, lr = repo(icloud, '{"v":1}'), repo(local, '{"v":1}')
    wt1, wt2, wt3 = d / "wt1", d / "wt2", d / "wt3"
    for w in (wt1, wt2, wt3):
        w.mkdir()
    m._link_node_modules(wt1, SimpleNamespace(repo=str(ir)))
    t1 = (wt1 / "node_modules").resolve()
    check("iCloud repo -> link points OUTSIDE the iCloud root", icloud in t1.parents, False)
    check("iCloud repo -> link points into the local cache", cache in t1.parents, True)
    check("mirror has the deps", (wt1 / "node_modules" / "pkg" / "index.js").read_text(), "module.exports=1\n")
    check("built by npm ci (not a copy of the evicted tree)",
          (wt1 / "node_modules" / "only-in-source.txt").exists(), False)
    check("npm ci --prefer-offline was the install", "ci --prefer-offline" in (d / "npm.log").read_text(), True)
    check("generated .prisma client carried over",
          (wt1 / "node_modules" / ".prisma" / "client" / "index.js").read_text(), "gen\n")
    check("lockfile scaffolding not left beside the mirror", (t1.parent / "package-lock.json").exists(), False)
    m._link_node_modules(wt2, SimpleNamespace(repo=str(ir)))
    check("same lockfile -> the same mirror is reused", (wt2 / "node_modules").resolve(), t1)
    (ir / "package-lock.json").write_text('{"v":2}')
    m._link_node_modules(wt3, SimpleNamespace(repo=str(ir)))
    check("changed lockfile -> a fresh mirror", (wt3 / "node_modules").resolve() != t1, True)
    wl = d / "wtl"
    wl.mkdir()
    m._link_node_modules(wl, SimpleNamespace(repo=str(lr)))
    check("non-iCloud repo -> still links the repo's own node_modules",
          (wl / "node_modules").resolve(), (lr / "node_modules").resolve())
    check("no stray temp dirs left in the cache", [p.name for p in cache.glob(".*tmp*")], [])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("mirror disabled", "    if _icloud_synced(src):\n        mirror, how",
     "    if False:\n        mirror, how"),
    ("key ignores lockfile", 'hashlib.sha1(lock.read_bytes()).hexdigest()[:12] if lock.is_file() else "nolock"',
     '"fixed"'),
    ("copy instead of install", '["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]',
     '["cp", "-R", str(src), "node_modules"]'),
    ("generated client dropped", 'for gen in (".prisma",):', 'for gen in ():'),
    ("mirror every repo", "    return any(rp == r or r in rp.parents for r in roots)", "    return True"),
]


def revert_check():
    bad = 0
    src = SCAF.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-scaf", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SCAFFOLD_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
