#!/usr/bin/env python3
"""Shared-tail append resolver (2026-09-27, BFMR s2 vs s3).

s2 was authored at chain tip c0f0c08; s3 landed first (56f49a7). Both appended a
function right above `module.exports = { ORDERS_URL, isLoggedOut, X };` and added
their name to it, so the stale-seed 3-way rebase refused s2 on one conflicting
hunk. resolve_shared_tail_appends keeps both blocks in PLAN order, unions the
export list, and _stage_deliverable re-verifies with the slice's verify.sh AND
every landed slice's verify.sh. Anything else still refuses.

Red on revert: drop the resolver hook from _stage_deliverable -> the end-to-end
"auto-resolves" assertions fail (the conflict comes back).
Sandboxed: temp HOME + temp git repos. Run: python3 test-slice-shared-tail-resolve.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SLICER = os.environ.get("SLICER") or str(BIN / "ollama-dispatch-slice")
failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def load():
    loader = SourceFileLoader("slicer_tail", SLICER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


BASE = ("'use strict';\n\nconst ORDERS_URL = 'u';\n\nfunction isLoggedOut() { return 1; }\n\n"
        "module.exports = { ORDERS_URL, isLoggedOut };\n")
S2 = "function installInterceptor() { return 2; }\n\n"
S3 = "async function confirmLoggedIn() { return 3; }\n\n"


def with_fn(text, fn, name):
    head, tail = text.rsplit("module.exports", 1)
    names = tail.split("{", 1)[1].split("}", 1)[0]
    return head + fn + "module.exports = {" + names.rstrip() + ", " + name + " };\n"


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def verify_sh(must):
    return ("#!/bin/bash\nset -e\n"
            f"node -e \"const m=require('./a.js'); if (typeof m.{must} !== 'function') process.exit(1)\"\n"
            "echo VERIFY_OK\n")


def main():
    home = tempfile.mkdtemp(prefix="tail-home-")
    os.environ["HOME"] = home
    m = load()
    R = m.resolve_shared_tail_appends

    print("pure resolver")
    ours = with_fn(BASE, S3, "confirmLoggedIn")          # chain: s3 landed
    theirs = with_fn(BASE, S2, "installInterceptor")      # slice s2
    got, why = R(BASE, ours, theirs, theirs_first=True)
    ok("CommonJS export-tail conflict resolves", got is not None)
    if got:
        ok("both blocks kept, s2 before s3 (plan order)",
           0 <= got.find("installInterceptor()") < got.find("confirmLoggedIn()"))
        ok("export list unioned in plan order",
           "module.exports = { ORDERS_URL, isLoggedOut, installInterceptor, confirmLoggedIn };"
           in got)
        ok("exactly one export line", got.count("module.exports") == 1)
    got2, _ = R(BASE, ours, theirs, theirs_first=False)
    ok("chain-first order when the chain holds EARLIER slices",
       got2 is not None and got2.find("confirmLoggedIn()") < got2.find("installInterceptor()"))
    es_base = "export const A = 1;\n\nexport { A };\n"
    es_o = "export const A = 1;\n\nfunction b() {}\n\nexport { A, b };\n"
    es_t = "export const A = 1;\n\nfunction c() {}\n\nexport { A, c };\n"
    g3, _ = R(es_base, es_o, es_t, theirs_first=False)
    ok("ES `export { }` tail resolves", g3 is not None and "export { A, b, c };" in g3)
    edit_o = BASE.replace("return 1;", "return 10;")
    edit_t = BASE.replace("return 1;", "return 11;")
    ok("a real overlapping edit REFUSES", R(BASE, edit_o, edit_t, True)[0] is None)
    ok("a deletion on one side REFUSES",
       R(BASE, with_fn(BASE, S3, "confirmLoggedIn").replace("const ORDERS_URL = 'u';\n", ""),
         theirs, True)[0] is None)
    drop = ours.replace("{ ORDERS_URL, isLoggedOut, confirmLoggedIn }",
                        "{ ORDERS_URL, confirmLoggedIn }")
    ok("removing an exported name REFUSES", R(BASE, drop, theirs, True)[0] is None)

    print("end to end through _stage_deliverable")
    if subprocess.run(["which", "node"], capture_output=True).returncode != 0:
        ok("node available for the e2e verify.sh", False)
        return finish()

    def build(s3_verify_extra=""):
        root = Path(tempfile.mkdtemp(prefix="tail-repo-"))
        cwt = root / "chain"
        cwt.mkdir()
        git(cwt, "init", "-q")
        git(cwt, "config", "user.email", "t@t")
        git(cwt, "config", "user.name", "t")
        (cwt / "a.js").write_text(BASE)
        git(cwt, "add", "a.js")
        git(cwt, "commit", "-qm", "slice s1: base")
        seed = git(cwt, "rev-parse", "HEAD")
        wts = {}
        for sid in ("s2", "s3"):
            w = root / f"wt-{sid}"
            git(cwt, "worktree", "add", "-q", "--detach", str(w), seed)
            wts[sid] = w
        (wts["s3"] / "a.js").write_text(with_fn(BASE, S3, "confirmLoggedIn"))
        (wts["s3"] / "verify.sh").write_text(verify_sh("confirmLoggedIn") + s3_verify_extra)
        (cwt / "a.js").write_text(with_fn(BASE, S3, "confirmLoggedIn"))
        git(cwt, "add", "a.js")
        git(cwt, "commit", "-qm", "slice s3: confirmLoggedIn")
        (wts["s2"] / "a.js").write_text(with_fn(BASE, S2, "installInterceptor"))
        (wts["s2"] / "verify.sh").write_text(verify_sh("installInterceptor"))
        st = {"order": ["s1", "s2", "s3"],
              "slices": {"s1": {"status": "done", "worktree": None},
                         "s2": {"status": "escalated", "worktree": str(wts["s2"])},
                         "s3": {"status": "done", "worktree": str(wts["s3"])}}}
        # s1 is the plan's seed commit; give it a verify.sh like any landed slice
        w1 = root / "wt-s1"
        git(cwt, "worktree", "add", "-q", "--detach", str(w1), seed)
        (w1 / "verify.sh").write_text(verify_sh("isLoggedOut"))
        st["slices"]["s1"]["worktree"] = str(w1)
        return cwt, wts, st

    cwt, wts, st = build()
    oc, msg = m._stage_deliverable(str(cwt), str(wts["s2"]), "a.js", st=st, sid="s2")
    ok("export-tail conflict AUTO-RESOLVES", oc == "ok")
    ok("...and says so", "AUTO-RESOLVED" in msg)
    staged = git(cwt, "show", ":a.js")
    ok("staged file has both functions, s2 first",
       0 <= staged.find("installInterceptor()") < staged.find("confirmLoggedIn()"))
    ok("landed slice s3's worktree target restored",
       (wts["s3"] / "a.js").read_text() == with_fn(BASE, S3, "confirmLoggedIn"))

    oc0, _ = m._stage_deliverable(str(cwt), str(wts["s2"]), "a.js")
    git(cwt, "reset", "-q", "HEAD")
    cwt, wts, st = build()
    oc0, _ = m._stage_deliverable(str(cwt), str(wts["s2"]), "a.js")
    ok("without plan context the old refusal stands", oc0 == "conflict")

    cwt, wts, st = build(s3_verify_extra="grep -q installInterceptor a.js && exit 1 || true\n")
    oc2, msg2 = m._stage_deliverable(str(cwt), str(wts["s2"]), "a.js", st=st, sid="s2")
    ok("a landed slice's verify.sh failing on the result REFUSES",
       oc2 == "conflict" and "verify.sh" in msg2)

    cwt, wts, st = build()
    import shutil
    shutil.rmtree(wts["s3"], ignore_errors=True)
    oc3, msg3 = m._stage_deliverable(str(cwt), str(wts["s2"]), "a.js", st=st, sid="s2")
    ok("a landed slice with no verify.sh REFUSES", oc3 == "conflict" and "gone" in msg3)
    return finish()


def finish():
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL PASSED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
