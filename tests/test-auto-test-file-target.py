#!/usr/bin/env python3
"""Guards the fix of 2026-10-02 (plex-automation s4 pa-upgrade-batches-order).

With --target <a test file>, auto's generic python fixture re-tested the
IMPLEMENTATION (which was right), so it passed at baseline and certified
nothing, and the model edited the test file directly instead of authoring
refimpl.py/TASK.md. Fix: a test-file target gets a GENERATED, LOCKED fixture
that runs the target's own tests (red on the wrong expectation, green once
corrected) and asserts no other tracked file moved; the target is reset to HEAD
before every gate round; a JS/TS test target is refused with a clear message.

Run: python3 test-auto-test-file-target.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

AUTO = Path(os.environ.get("AUTO_SRC", Path(__file__).resolve().parent / "ollama-dispatch-auto"))
FAILS = []

IMPL = '''def sort_ids_by_year_desc(items):
    """Newest year first; zero/invalid year last; ties keep input order (stable)."""
    def y(it):
        try:
            return int(str(it.get("year") or 0)[:4])
        except ValueError:
            return 0
    return [it["id"] for it in sorted(items, key=lambda it: -y(it))]
'''
TEST_WRONG = '''from sorter import sort_ids_by_year_desc

ITEMS = [{"id": 1, "year": 2021}, {"id": 2, "year": 2023}, {"id": 3},
         {"id": 4, "year": 2023}, {"id": 5, "year": "not-a-date"}]


def test_empty():
    assert sort_ids_by_year_desc([]) == []


def test_ordering():
    assert sort_ids_by_year_desc(ITEMS) == [4, 2, 1, 5, 3]
'''
TEST_RIGHT = TEST_WRONG.replace("[4, 2, 1, 5, 3]", "[2, 4, 1, 3, 5]")
# "bend the code to match the wrong test": reverse-stable on ties
IMPL_BENT = IMPL.replace("key=lambda it: -y(it))]", "key=lambda it: -y(it), reverse=False)][::1]") \
    .replace('return [it["id"] for it in sorted(items, key=lambda it: -y(it), reverse=False)][::1]',
             'return [4, 2, 1, 5, 3] if len(items) == 5 else [it["id"] for it in items]')


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = importlib.machinery.SourceFileLoader("auto_tt", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_tt", ld))
    ld.exec_module(m)
    return m


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def run_fixture(wt):
    r = subprocess.run([sys.executable, "test_fixture.py"], cwd=wt, capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    return r.returncode


def main():
    m = load()
    check("detect: test_x.py", m.is_test_file_target("test_upgrade_batches.py"), True)
    check("detect: tests/x.py", m.is_test_file_target("tests/sorting.py"), True)
    check("detect: pkg/x_test.py", m.is_test_file_target("pkg/sort_test.py"), True)
    check("detect: lib/x.test.ts", m.is_test_file_target("lib/x.test.ts"), True)
    check("detect: arr-webhook.py is NOT a test", m.is_test_file_target("arr-webhook.py"), False)
    check("detect: app/main.py is NOT a test", m.is_test_file_target("app/main.py"), False)
    check("detect: latest_tool.py is NOT a test", m.is_test_file_target("latest_tool.py"), False)
    check("refuse: python test target handled", m.test_target_refusal("test_s.py", "python"), None)
    check("refuse: normal target never refused", m.test_target_refusal("lib/x.ts", "ts"), None)
    why = m.test_target_refusal("lib/x.test.ts", "ts") or ""
    check("refuse: TS test target refused, names enqueue", "ollama-queue.py enqueue" in why, True)

    a = SimpleNamespace(intent="fix the wrong expectation", interface="", lang="python")
    m.load_attempts = lambda a, runs_dir=None: []
    p_test = m.author_prompt(a, "test_sorter.py")
    p_code = m.author_prompt(a, "sorter.py")
    check("prompt: test target says fixture ALREADY WRITTEN", "ALREADY WRITTEN" in p_test, True)
    check("prompt: test target drops the adversarial-fixture section", "ADVERSARIAL fixture" in p_test, False)
    check("prompt: normal target keeps the adversarial-fixture section", "ADVERSARIAL fixture" in p_code, True)

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        (wt / "sorter.py").write_text(IMPL)
        (wt / "test_sorter.py").write_text(TEST_WRONG)
        git(wt, "init", "-q")
        git(wt, "add", "-A")
        git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        (wt / "test_fixture.py").write_text(m.test_target_fixture("test_sorter.py"))
        check("fixture: FAILS at baseline (wrong expectation)", run_fixture(wt) != 0, True)
        (wt / "test_sorter.py").write_text(TEST_RIGHT)
        check("fixture: PASSES once the expectation is corrected", run_fixture(wt), 0)
        (wt / "test_sorter.py").write_text(TEST_WRONG)
        (wt / "sorter.py").write_text(IMPL_BENT)
        check("fixture: FAILS when the impl is bent to match the wrong test", run_fixture(wt) != 0, True)
        (wt / "sorter.py").write_text(IMPL)
        # enforcement: a directly-edited target and an edited fixture are undone
        (wt / "test_sorter.py").write_text(TEST_RIGHT)
        (wt / "test_fixture.py").write_text("print('model fixture')\n")
        undone = m.enforce_test_target_harness(wt, "test_sorter.py")
        check("enforce: reports both undos", len(undone), 2)
        check("enforce: target back at HEAD", (wt / "test_sorter.py").read_text(), TEST_WRONG)
        check("enforce: fixture re-locked",
              (wt / "test_fixture.py").read_text(), m.test_target_fixture("test_sorter.py"))
        check("enforce: idempotent on a clean tree", m.enforce_test_target_harness(wt, "test_sorter.py"), [])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("detector returns False",
     "    return bool(_PY_TEST_FILE_RE.search(t) or _JS_TEST_FILE_RE.search(t))\n",
     "    return False\n"),
    ("fixture drops the impl-unchanged case",
     "changed = [f for f in r.stdout.split() if f != TARGET and f not in HARNESS]",
     "changed = []"),
    ("enforce no longer resets the target",
     '        subprocess.run(["git", "-C", str(wt), "checkout", "HEAD", "--", target], check=False)\n',
     ""),
    ("prompt not switched",
     "    if is_test_file_target(target) and not jsish:\n        fixture_section",
     "    if False:\n        fixture_section"),
    ("JS test target not refused",
     "    if (lang or \"python\").lower() == \"python\" and target.endswith(\".py\"):\n        return None\n    return (",
     "    return None\n    return ("),
]


def revert_check():
    src = AUTO.read_text()
    bad = 0
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"mutation anchor missing/ambiguous: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
