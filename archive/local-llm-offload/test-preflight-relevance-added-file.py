#!/usr/bin/env python3
"""verify-relevance measures a CREATION target the refimpl writes as an
UNTRACKED file (2026-10-02, idle-test-repo-size).

In --repo auto mode the creation stub stays untracked (preflight --auto-seal seals
the harness, never the target), so `git diff -U0` was empty and the gate bailed
"[UNPR] verify-relevance the reference impl produced no tracked diff to mutate
(added files only?)" -- an unattended run trusted whatever fixture was written.

Asserted (end to end, real preflight on a throwaway git repo):
  A. untracked STUB overwritten by the refimpl, STRONG fixture -> relevance PASS
     with a real kill count (not UNPROVEN), and the stub is restored afterwards
  B. same, WEAK fixture (only checks the function exists) -> relevance LOW/FAIL:
     proves the mutants really land on the untracked file and are judged
  C. refimpl ADDS a brand-new source file (no stub, no manifest) -> measured
  D. unit: untracked_refimpl_diff skips harness, __pycache__, untouched files
  E. edit target (tracked diff) still measured -- no regression

PF_SRC env overrides the preflight under test (revert-check: point it at the .bak).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

GATE = Path(os.environ.get("PF_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-preflight")
FAILS = []

STUB = '"""Stub for scripts/sz.py -- implement per TASK.md."""\n'
IMPL = '''"""Size parsing."""


def parse_gb(s):
    s = s.strip()
    if not s:
        raise ValueError("empty")
    num, unit = s[:-2], s[-2:]
    n = float(num)
    if n < 0:
        raise ValueError("negative")
    if unit == "GB":
        return n
    if unit == "MB":
        return n / 1000
    raise ValueError("unit")
'''
STRONG = '''import sys
sys.path.insert(0, "scripts")
from sz import parse_gb
fails = 0
def eq(a, b):
    global fails
    if a != b:
        print("FAIL", a, b); fails += 1
eq(parse_gb("2GB"), 2.0)
eq(parse_gb(" 500MB "), 0.5)
eq(parse_gb("0GB"), 0.0)
try:
    parse_gb("  "); print("FAIL no raise blank"); fails += 1
except ValueError as e:
    eq(str(e), "empty")
for bad in ("", "1TB", "-1GB"):
    try:
        parse_gb(bad); print("FAIL no raise", bad); fails += 1
    except ValueError:
        pass
print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
'''
WEAK = '''import sys
sys.path.insert(0, "scripts")
import sz
ok = hasattr(sz, "parse_gb")
print("--- 0 failed ---" if ok else "--- 1 failed ---")
sys.exit(0 if ok else 1)
'''


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def make(root, fixture, stub=True, manifest=True, edit=False):
    wt = root
    wt.mkdir(parents=True)
    g(wt, "init", "-q")
    (wt / "README.md").write_text("x\n")
    (wt / "scripts").mkdir()
    if edit:
        (wt / "scripts" / "sz.py").write_text(STUB)
    g(wt, "add", "-A")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    if stub and not edit:
        (wt / "scripts" / "sz.py").write_text(STUB)
    if manifest:
        (wt / ".dispatch-harness.json").write_text(json.dumps(
            {"authored": ["TASK.md", "verify.sh", "test_fixture.py"],
             "target": "scripts/sz.py", "creation_task": not edit}))
    (wt / "TASK.md").write_text(
        "# Task\nImplement parse_gb in `scripts/sz.py`.\n\nOnly edit `scripts/sz.py`.\n"
        "Run `bash verify.sh` after every edit until it prints VERIFY_OK.\n"
        "\n## Must contain\n\n- `parse_gb`\n")
    (wt / "test_fixture.py").write_text(fixture)
    (wt / "verify.sh").write_text(
        "export PYTHONDONTWRITEBYTECODE=1\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n")
    (wt / "refimpl.py").write_text(
        "from pathlib import Path\nPath('scripts').mkdir(exist_ok=True)\n"
        f"Path('scripts/sz.py').write_text({IMPL!r})\n")
    return wt


def run_gate(wt, home):
    r = subprocess.run(
        [sys.executable, str(GATE), str(wt), "--refimpl-cmd", "python3 refimpl.py",
         "--target", "scripts/sz.py", "--json", "--relevance-max-mutants", "25"],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "HOME": str(home)})
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    rows = {}
    for c in (d.get("checks") or d.get("results") or []):
        rows[c.get("id") or c.get("check") or c.get("name")] = c
    return r, d, rows


def rel_row(rows):
    return rows.get("verify-relevance") or {}


def status(row):
    return row.get("status") or row.get("verdict") or row.get("result")


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        home.mkdir()

        # A: untracked stub + strong fixture
        wt = make(root / "a", STRONG)
        r, d, rows = run_gate(wt, home)
        row = rel_row(rows)
        if "-v" in sys.argv or not row:
            print(r.stdout[-3000:], r.stderr[-2000:])
        msg = (row.get("message") or row.get("msg") or row.get("summary") or "")
        check("A: relevance is MEASURED (not 'no tracked diff')",
              "no tracked diff" in json.dumps(row), False)
        check("A: strong fixture -> relevance PASS", status(row), "PASS")
        check("A: kill count reported", "killed" in msg, True)
        check("A: stub restored after the gate",
              (wt / "scripts" / "sz.py").read_text(), STUB)

        # B: weak fixture -> must be judged LOW (mutants really applied)
        wt = make(root / "b", WEAK)
        r, d, rows = run_gate(wt, home)
        row = rel_row(rows)
        check("B: weak fixture -> relevance FAIL (LOW)", status(row), "FAIL")
        check("B: stub restored after the gate",
              (wt / "scripts" / "sz.py").read_text(), STUB)

        # C: brand-new file, no stub, no manifest
        wt = make(root / "c", STRONG, stub=False, manifest=False)
        r, d, rows = run_gate(wt, home)
        row = rel_row(rows)
        check("C: added file (no stub) -> relevance PASS", status(row), "PASS")
        check("C: added file removed after the gate",
              (wt / "scripts" / "sz.py").exists(), False)

        # E: edit target (tracked) still measured
        wt = make(root / "e", STRONG, edit=True)
        r, d, rows = run_gate(wt, home)
        check("E: tracked edit target -> relevance PASS", status(rel_row(rows)), "PASS")

        # D: unit on the helper
        loader = SourceFileLoader("pf_t", str(GATE))
        spec = importlib.util.spec_from_loader("pf_t", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        if not hasattr(m.Gate if hasattr(m, "Gate") else object, "untracked_refimpl_diff") \
                and not any(hasattr(v, "untracked_refimpl_diff") for v in vars(m).values()
                            if isinstance(v, type)):
            check("D: untracked_refimpl_diff exists", False, True)
        else:
            cls = next(v for v in vars(m).values()
                       if isinstance(v, type) and hasattr(v, "untracked_refimpl_diff"))
            wt = make(root / "d", STRONG)
            obj = cls.__new__(cls)
            obj.wt = wt
            obj.a = type("A", (), {})()
            obj.git = lambda *a: (lambda p: (p.returncode, p.stdout, p.stderr))(
                g(wt, *a))
            obj._pre_refimpl_untracked = {"scripts/sz.py", "TASK.md", "verify.sh",
                                          "test_fixture.py", "refimpl.py",
                                          ".dispatch-harness.json", "notes.txt"}
            (wt / "notes.txt").write_text("untouched\n")
            obj._pre_refimpl_bytes = {"scripts/sz.py": STUB.encode(),
                                      "notes.txt": b"untouched\n"}
            (wt / "scripts" / "sz.py").write_text(IMPL)
            (wt / "scripts" / "__pycache__").mkdir()
            (wt / "scripts" / "__pycache__" / "junk.py").write_text("x=1\n")
            (wt / "test_fixture.py").write_text(WEAK + "#edited\n")
            dt = obj.untracked_refimpl_diff()
            files = set(m2 for m2 in
                        __import__("re").findall(r"^\+\+\+ b/(\S+)", dt, __import__("re").M))
            check("D: only the target is diffed", files, {"scripts/sz.py"})
            check("D: diff is against the stub (stub line removed)",
                  "-" + STUB.strip() in dt, True)

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
