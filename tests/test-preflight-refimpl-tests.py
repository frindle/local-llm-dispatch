#!/usr/bin/env python3
"""preflight refimpl-tests: the refimpl is checked against the fixture's OWN tests
(TAP / node:test / pytest summary) before any coding job is enqueued.
--revert-check mutates the preflight and requires this suite to go RED.
PF_SRC env overrides the preflight under test."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

GATE = Path(os.environ.get("PF_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-preflight")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def fixture(root, tap_when_fixed):
    """t.py returns 0 at baseline; refimpl makes it 1. verify.sh prints TAP-ish output:
    `tap_when_fixed` is what the 'runner' prints once the refimpl is in."""
    wt = root / "wt"
    wt.mkdir(parents=True)
    g(wt, "init", "-q")
    (wt / "t.py").write_text("def f():\n    return 0\n")
    g(wt, "add", "t.py")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (wt / "TASK.md").write_text("# Task\nMake f() return 1 in `t.py`.\n\nOnly edit `t.py`.\n"
                                "Run `bash verify.sh` after every edit until it prints VERIFY_OK.\n"
                                "\n## Must contain\n\n- `return 1`\n")
    (wt / "tap.txt").write_text(tap_when_fixed)
    (wt / "verify.sh").write_text(
        'fails=0\nif grep -q "return 1" t.py; then cat tap.txt; '
        'grep -q "^# fail [1-9]" tap.txt && fails=$((fails+1)); '
        'else echo "not ok 1 - f returns 1"; echo "# tests 1"; echo "# fail 1"; fails=$((fails+1)); fi\n'
        'echo "--- $fails failed ---"\n[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "refimpl.py").write_text(
        "from pathlib import Path\nPath('t.py').write_text('def f():\\n    return 1\\n')\n")
    return wt


def run_gate(wt, home):
    r = subprocess.run(
        [sys.executable, str(GATE), str(wt), "--refimpl-cmd", "python3 refimpl.py",
         "--no-relevance", "--target", "t.py", "--json"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "HOME": str(home)})
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    rows = {c.get("id") or c.get("check"): c for c in (d.get("checks") or d.get("results") or [])}
    return r, d, rows


def main():
    loader = SourceFileLoader("pf_t", str(GATE))
    spec = importlib.util.spec_from_loader("pf_t", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    c = m.classify_refimpl_tests
    check("TAP 'not ok' names the failing fixture test",
          c("ok 1 - a\nnot ok 2 - picks row\n# tests 2\n# fail 1\n", False)[:1] + (c("not ok 2 - picks row\n# tests 2\n# fail 1\n", False)[2],),
          ("FAIL", "picks row"))
    check("TAP todo is an expected failure, not a refimpl failure",
          c("not ok 1 - later # TODO\n# tests 1\n# fail 0\n", True)[0], "PASS")
    check("zero tests executed is a FAIL even when the verify is green",
          c("# tests 0\n# pass 0\n", True)[0], "FAIL")
    check("node spec reporter count is read", c("ℹ tests 5\nℹ fail 0\n", True)[0], "PASS")
    check("pytest pass count is read", c("==== 3 passed, 1 warning in 0.12s ====", True)[0], "PASS")
    check("pytest FAILED line names the test",
          c("FAILED t.py::test_x - assert\n=== 1 failed, 2 passed in 0.1s ===", False)[2], "t.py::test_x")
    check("pytest 'no tests ran' is zero tests", c("===== no tests ran in 0.01s =====", True)[0], "FAIL")
    check("no runner summary -> SKIP, never a guessed FAIL", c("VERIFY_OK", True)[0], "SKIP")
    check("tests pass but verify red elsewhere -> PASS (non-test stage)",
          c("  FAIL: tsc\n# tests 4\n# fail 0", False)[0], "PASS")

    root = Path(tempfile.mkdtemp(prefix="pfrt-")).resolve()
    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True)
    wt = fixture(root / "a", "ok 1 - f returns 1\nnot ok 2 - picks the shipment row\n# tests 2\n# fail 1\n")
    r, d, rows = run_gate(wt, home)
    row = rows.get("refimpl-tests") or {}
    check("e2e: a refimpl that fails its own test is a refimpl-tests FAIL",
          row.get("status"), "FAIL")
    check("e2e: the failing test is named", "picks the shipment row" in (row.get("detail") or ""), True)
    check("e2e: verdict is NO-GO", d.get("verdict"), "NO-GO")
    check("e2e: refimpl-tests is a TASK-fixable blocker", "refimpl-tests" in m.TASK_FIXABLE_BLOCKERS, True)
    wt2 = fixture(root / "b", "# tests 0\n# pass 0\n# fail 0\n")
    r2, d2, rows2 = run_gate(wt2, home)
    check("e2e: a green verify that ran zero tests is a refimpl-tests FAIL",
          (rows2.get("refimpl-tests") or {}).get("status"), "FAIL")
    wt3 = fixture(root / "c", "ok 1 - f returns 1\n# tests 1\n# fail 0\n")
    r3, d3, rows3 = run_gate(wt3, home)
    check("e2e: a refimpl that passes its tests is a refimpl-tests PASS",
          (rows3.get("refimpl-tests") or {}).get("status"), "PASS")
    check("e2e: ...and does not block GO on its own",
          "refimpl-tests" in [b if isinstance(b, str) else b.get("check") for b in (d3.get("blockers") or [])], False)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("check not wired", '            self.add("refimpl-tests", _st, _msg, detail=_det,',
     '            _st = "SKIP"\n            if False: self.add("refimpl-tests", _st, _msg, detail=_det,'),
    ("zero tests tolerated", "    if ran == 0:", "    if False:"),
    ("TAP failures ignored", "    if names or (fails and fails[-1] > 0):", "    if False:"),
    ("TODO counted as failure", 'if "TODO" not in (cmt or "").upper()]', "]"),
    ("not task-fixable", '"refimpl-passes", "refimpl-satisfies", "refimpl-tests",', '"refimpl-passes", "refimpl-satisfies",'),
]


def revert_check():
    bad = 0
    src = GATE.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-pf", delete=False, dir=str(GATE.parent)) as f:
            f.write(src.replace(old, new))
        os.chmod(f.name, 0o755)
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "PF_SRC": f.name},
                           capture_output=True, text=True, timeout=900)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
