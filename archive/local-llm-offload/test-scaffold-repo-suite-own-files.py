#!/usr/bin/env python3
"""The TS verify's repo-suite step must FAIL when `npm test` fails in a test file
THIS dispatch added/changed, and stay WARN for pre-existing red (2026-10-02).

Live: bfmr-replace-tracking's model added lib/bfmrReplaceTracking.test.ts with
`from './bfmrReplaceTracking'` (no `.ts`). Green under the verify's tsx runner, red
under the repo's `node --experimental-strip-types` npm test, and the WARN-only
repo-suite step let it pass the gate.

Renders the REAL NODE_TEST_VERIFY template, runs its repo-suite section in a temp
git repo with a fake `npm` on PATH. --revert-check mutates the template.
SCAFFOLD_SRC env overrides the scaffold under test."""
import importlib.util, os, subprocess, sys, tempfile
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


def section():
    ld = SourceFileLoader("scaf_rs", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_rs", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    txt = m.NODE_TEST_VERIFY.format(label="t", target="lib/a.ts", ts_parse="/x", bootstrap="",
                                    mark="SCAFFOLD_INCOMPLETE", target_err="lib/a", test_files="v.test.ts")
    a = txt.index('echo "=== repo suite')
    b = txt.index("rm -f /tmp/_verify_npmtest.$$.log", a)
    return txt[a:b] + "rm -f /tmp/_verify_npmtest.$$.log\n"


def run(sec, npm_out, npm_rc, setup):
    d = Path(tempfile.mkdtemp(prefix="rsown-"))
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (d / "lib").mkdir()
    (d / "lib" / "old.test.ts").write_text("// pre-existing\n")
    (d / "package.json").write_text('{"scripts": {"test": "node --test"}}\n')
    g("add", "-A"); g("commit", "-qm", "base")
    setup(d)
    bind = d / "_bin"
    bind.mkdir()
    (d / "npm.out").write_text(npm_out)
    (bind / "npm").write_text(f'#!/bin/bash\ncat "{d}/npm.out"\nexit {npm_rc}\n')
    os.chmod(bind / "npm", 0o755)
    (d / "s.sh").write_text("fails=0\n" + sec + 'echo "FAILS=$fails"\n')
    r = subprocess.run(["bash", "s.sh"], cwd=d, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{bind}:{os.environ['PATH']}"})
    return r.stdout


SPEC_RED = ("ℹ fail 1\n\n✖ failing tests:\n\ntest at lib/new.test.ts:1:1\n"
            "✖ lib/new.test.ts (101.8ms)\n  'test failed'\n")
SPEC_RED_OLD = SPEC_RED.replace("new.test.ts", "old.test.ts")
TAP_RED = "TAP version 13\nnot ok 3 - lib/old.test.ts\n# fail 1\n"


def main():
    sec = section()
    new_file = lambda d: (d / "lib" / "new.test.ts").write_text("x\n")
    out = run(sec, SPEC_RED, 1, new_file)
    chk("spec reporter: a NEW (untracked) failing test file -> FAIL", "FAILS=1" in out, True)
    chk("...and the FAIL names the file", "lib/new.test.ts" in out and "FAIL: npm test fails" in out, True)
    out = run(sec, SPEC_RED_OLD, 1, lambda d: None)
    chk("pre-existing red in an UNCHANGED test file -> WARN only", ("FAILS=0" in out, "WARN" in out), (True, True))
    out = run(sec, TAP_RED, 1, lambda d: (d / "lib" / "old.test.ts").write_text("// edited\n"))
    chk("TAP reporter: a MODIFIED failing test file -> FAIL", "FAILS=1" in out, True)
    out = run(sec, "ℹ pass 3\n", 0, new_file)
    chk("npm test green -> ok, no fail", ("FAILS=0" in out, "ok: npm test green" in out), (True, True))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("own-file FAIL branch never taken", '  if [ -n "$_own" ]; then\n', "  if false; then\n"),
    ("spec ✖/test-at lines not parsed", "(✖|not ok [0-9]+ -|test at|FAIL) ", "(not ok [0-9]+ -|FAIL) "),
    ("untracked files not counted", "git status --porcelain --untracked-files=all -- \"$_f\"",
     "git status --porcelain --untracked-files=no -- \"$_f\""),
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
