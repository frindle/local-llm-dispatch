#!/usr/bin/env python3
"""Regression test for the language-aware auto-harness self-check generator in
ollama-dispatch-auto (write_harness_check / HARNESS_CHECK).

Guards the bug fixed 2026-09-12: the generated auto-harness-check.py hardcoded
`test_fixture.py` in its required-files loop, so a --lang ts dispatch (whose
fixture is verify.test.ts / verify_impl.mts / *.test.ts) failed "test_fixture.py
is missing" from iteration 0 -- unsatisfiable, and it burned resell-gc-ingest to
its iteration cap while reading as a model/iteration-cap failure.

Run: python3 ~/bin/test-ollama-dispatch-auto-harness.py  -> AUTO_HARNESS_OK
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

AUTO = os.path.expanduser("~/bin/ollama-dispatch-auto")
loader = SourceFileLoader("oda", AUTO)
spec = importlib.util.spec_from_loader("oda", loader)
oda = importlib.util.module_from_spec(spec)
loader.exec_module(oda)


def build(wt, lang, fixture_name):
    """A minimal worktree whose verify FAILS at baseline and PASSES after
    refimpl.py, with only the given (language-appropriate) fixture present."""
    os.makedirs(wt, exist_ok=True)
    subprocess.run(["git", "init", "-q", wt], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", wt, "config", "user.name", "t"], check=True)
    Path(wt, "TASK.md").write_text(
        "## Must contain\n- `MARK`\nOnly edit `target.txt`; do not edit verify.sh\n")
    Path(wt, "verify.sh").write_text(
        'f=0\ngrep -q MARK target.txt 2>/dev/null || { echo "no MARK"; f=1; }\n'
        'echo "--- $f failed ---"; [ "$f" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    Path(wt, "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    Path(wt, "target.txt").write_text("stub\n")   # tracked stub, no MARK -> baseline fails
    Path(wt, fixture_name).write_text("// fixture\n")
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-q", "-m", "base"], check=True)
    oda.write_harness_check(Path(wt), lang)


def run_check(wt):
    return subprocess.run(["python3", "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True)


def main():
    fails = 0

    # Unit: the injected globs are language-correct.
    if "test_fixture.py" in oda.fixture_globs_for("ts"):
        print("  FAIL: ts globs must not require test_fixture.py"); fails += 1
    if "verify.test.ts" not in oda.fixture_globs_for("ts"):
        print("  FAIL: ts globs must accept verify.test.ts"); fails += 1
    if oda.fixture_globs_for("python") != ["test_fixture.py"]:
        print("  FAIL: python globs must be exactly test_fixture.py"); fails += 1

    # THE REGRESSION: ts tree with verify.test.ts and NO test_fixture.py passes.
    d = tempfile.mkdtemp(prefix="ts-hc-")
    build(d, "ts", "verify.test.ts")
    if Path(d, "test_fixture.py").exists():
        print("  FAIL: test setup leaked a test_fixture.py"); fails += 1
    r = run_check(d)
    if not (r.returncode == 0 and "VERIFY_OK" in r.stdout):
        print("  FAIL: ts self-check did not pass with verify.test.ts:\n   "
              + (r.stdout + r.stderr).strip()[-300:]); fails += 1

    # Python path still works.
    d2 = tempfile.mkdtemp(prefix="py-hc-")
    build(d2, "python", "test_fixture.py")
    r2 = run_check(d2)
    if not (r2.returncode == 0 and "VERIFY_OK" in r2.stdout):
        print("  FAIL: python self-check regressed:\n   "
              + (r2.stdout + r2.stderr).strip()[-300:]); fails += 1

    # Missing any fixture -> clean, informative failure (not a crash).
    d3 = tempfile.mkdtemp(prefix="none-hc-")
    build(d3, "ts", "verify.test.ts")
    os.remove(Path(d3, "verify.test.ts"))
    r3 = run_check(d3)
    if not (r3.returncode != 0 and "no test fixture found" in (r3.stdout + r3.stderr)):
        print("  FAIL: missing fixture should fail with the fixture message"); fails += 1

    if fails:
        print(f"  {fails} check(s) failed")
        return 1
    print("AUTO_HARNESS_OK: self-check is language-aware (ts/python/missing all correct)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
