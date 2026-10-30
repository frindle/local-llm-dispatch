#!/usr/bin/env python3
"""Guards 2026-10-05: rt-bg-commitments-fix-getcommitments (87ef87235fd3, -c1
95234578bc40) and rt-egift-link-s1-s0-db-schema (35803fc92ce8).

(a) auto-harness-check carries section FAIL lines (tsc / parse / literals) that sit
    ABOVE the long TAP output past its 1500-char tail truncation -- the tsc error
    that pinned the refimpl's bug was clipped on every run.
(b) when the type gate failed on the target, the TYPE/PARSE hint is given and the
    SUSPECT THE FIXTURE addendum is suppressed; control: without a tsc FAIL, a
    repeated case still gets SUSPECT THE FIXTURE (TS wording, no `chk`).
(c) scaffold TS bootstrap regenerates a worktree-local prisma client when the
    schema differs from HEAD / from the last generated stamp (stale client made a
    correct impl unsatisfiable: TS2339 on the new model), and NOT when unchanged.

Run: python3 test-harness-failcarry-prisma-stale.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
SCAF = Path(os.environ.get("SCAF_SRC") or BIN / "ollama-dispatch-scaffold")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[-600:]))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def mkwt(with_tsc_fail):
    wt = Path(tempfile.mkdtemp(prefix="failcarry-")) / "wt"
    wt.mkdir()
    git(wt, "init", "-q"); git(wt, "config", "user.email", "t@t"); git(wt, "config", "user.name", "t")
    (wt / "target.txt").write_text("stub\n"); git(wt, "add", "target.txt"); git(wt, "commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `target.txt`\n")
    tsc = ("echo '=== types (tsc --noEmit) ==='\n"
           "echo '  FAIL: tsc --noEmit reports errors in target.txt'\n"
           "echo 'target.txt(377,18): error TS2362: The left-hand side of an arithmetic operation'\n"
           ) if with_tsc_fail else "echo '=== types (tsc --noEmit) ==='\necho '  ok: tsc --noEmit clean'\n"
    filler = "\n".join(f"echo '  noisy TAP stack line {i} ..............................'" for i in range(120))
    (wt / "verify.sh").write_text(
        "echo '=== env bootstrap ==='\necho '  ok: prisma client present'\n"
        'grep -q MARK target.txt || { echo "FAIL - no MARK"; exit 1; }\n'
        + tsc +
        "echo '=== behavioural tests (tsx) ==='\n"
        + filler + "\n" "echo 'not ok 5 - returns valid data on SUCCESS'\n"
        "echo '# fail 1'\necho '--- 2 failed ---'\nexit 1\n")
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    return wt


def selfcheck_cases(m):
    run = lambda wt: subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                                    capture_output=True, text=True, timeout=120).stdout
    wt = mkwt(True)
    m.write_harness_check(wt, "ts")
    o1 = run(wt)
    check("tsc FAIL line carried past the tail", "FAIL: tsc --noEmit reports errors in target.txt" in o1, o1)
    check("tsc file:line detail carried past the tail", "TS2362" in o1, o1)
    check("TYPE/PARSE hint given when tsc failed on the target", "TYPE/PARSE gate failed" in o1, o1)
    o2 = run(wt)
    check("repeat case with tsc FAIL: SUSPECT THE FIXTURE suppressed", "SUSPECT THE FIXTURE" not in o2, o2)
    wt2 = mkwt(False)
    m.write_harness_check(wt2, "ts")
    p1 = run(wt2)
    check("control: no tsc FAIL -> no TYPE/PARSE hint", "TYPE/PARSE gate failed" not in p1, p1)
    p2 = run(wt2)
    check("control: repeat case still SUSPECT THE FIXTURE", "SUSPECT THE FIXTURE" in p2, p2)
    check("control: TS fixture hint has no Python `chk` wording", "chk's 3rd argument" not in p2, p2)


def prisma_cases(scaf):
    td = Path(tempfile.mkdtemp(prefix="prismastale-"))
    fb = td / "fakebin"; fb.mkdir()
    log = td / "gen-calls"
    # fake npx: records the call and "generates" (writes a file into the output dir)
    (fb / "npx").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nmkdir -p app/generated/prisma\n"
                            "echo gen > app/generated/prisma/client.ts\nexit 0\n")
    (fb / "npx").chmod(0o755)
    wt = td / "wt"; (wt / "prisma").mkdir(parents=True)
    (wt / "package.json").write_text("{}")
    (wt / "node_modules").mkdir()
    (wt / "prisma" / "schema.prisma").write_text(
        'generator client {\n  provider = "prisma-client"\n  output   = "../app/generated/prisma"\n}\nmodel A { id Int @id }\n')
    git(wt, "init", "-q"); git(wt, "config", "user.email", "t@t"); git(wt, "config", "user.name", "t")
    git(wt, "add", "prisma/schema.prisma", "package.json"); git(wt, "commit", "-qm", "b")
    (wt / "app/generated/prisma").mkdir(parents=True)
    (wt / "app/generated/prisma/client.ts").write_text("old\n")
    script = "fails=0\n" + scaf.TS_BOOTSTRAP + "\n"
    env = {**os.environ, "PATH": f"{fb}:{os.environ['PATH']}"}
    sh = lambda: subprocess.run(["bash", "-c", script], cwd=wt, capture_output=True, text=True, env=env, timeout=60)
    ncalls = lambda: len(log.read_text().splitlines()) if log.exists() else 0
    sh()
    check("prisma: unchanged schema + present client -> no generate (control)", ncalls() == 0, ncalls())
    with open(wt / "prisma" / "schema.prisma", "a") as f:
        f.write("model OrderEgmtLink { id Int @id }\n")
    r = sh()
    check("prisma: schema changed vs HEAD -> regenerate", ncalls() == 1, r.stdout)
    sh()
    check("prisma: second run, schema same as stamp -> no regenerate", ncalls() == 1, ncalls())
    git(wt, "checkout", "--", "prisma/schema.prisma")
    sh()
    check("prisma: schema reverted to HEAD after stamp -> regenerate again", ncalls() == 2, ncalls())


def main():
    m = load(AUTO, "oda_fc")
    selfcheck_cases(m)
    prisma_cases(load(SCAF, "scaf_fc"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTANTS = [
    ("AUTO_SRC", AUTO, "section FAIL carry disabled",
     "    if any(l.strip() not in tail for l in _secf):", "    if False:"),
    ("AUTO_SRC", AUTO, "type-gate hint disabled",
     '    if _typef and "SyntaxError" not in tail:', "    if False:"),
    ("AUTO_SRC", AUTO, "fixture-suspect no longer suppressed",
     "    if _rep and not _typef:", "    if _rep:"),
    ("SCAF_SRC", SCAF, "stale-vs-HEAD detection removed",
     '  elif ! git diff --quiet HEAD -- "$_SSRC" 2>/dev/null; then\n    _STALE=1\n',
     "  elif false; then\n    _STALE=1\n"),
    ("SCAF_SRC", SCAF, "stamp comparison removed",
     '    [ "$(cat "$_ABS/.verify-schema-sig")" = "$_CUR_SIG" ] || _STALE=1\n', "    :\n"),
]


def revert_check():
    bad = 0
    for var, path, name, old, new in MUTANTS:
        s = Path(path).read_text()
        assert s.count(old) == 1, f"anchor missing/ambiguous: {old[:60]!r}"
        f = tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False, dir=str(BIN))
        f.write(s.replace(old, new)); f.close()
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
