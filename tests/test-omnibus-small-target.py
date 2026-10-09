#!/usr/bin/env python3
"""A SMALL (or not-yet-existing) target is ONE slice: the auto-slicer must not split
a ~30-line one-file helper with a few exported functions into 3 slices (2026-10-02).
Large targets still auto-slice. --revert-check mutates omnibus_slice.py /
ollama-dispatch-auto (OMNI_SRC / AUTO_SRC env) and requires RED."""
import ast, importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
OMNI = Path(os.environ.get("OMNI_SRC") or HERE / "omnibus_slice.py")
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []

INTENT = ("Create lib/util.ts exporting three PURE functions.\n"
          "1) parseA(x): returns the A field.\n"
          "2) parseB(x): returns the B field.\n"
          "3) parseC(x): returns the C field.\n")


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("omni_t", str(OMNI))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("omni_t", ld))
    ld.exec_module(m)
    env = {}
    n_props = len(m.build_slices(INTENT, None))
    check("fixture intent lists >= threshold properties (else the test proves nothing)",
          n_props >= m.DEFAULT_THRESHOLD, True)
    root = Path(tempfile.mkdtemp(prefix="omst-"))
    small = root / "small.ts"
    small.write_text("export const x = 1;\n" * 30)
    big = root / "big.ts"
    big.write_text("export const y = 2; // padding padding padding padding\n" * 600)
    check("small existing target -> NOT omnibus",
          m.is_omnibus(INTENT, None, env=env, target_path=small)[0], False)
    check("missing target (creation task) -> NOT omnibus",
          m.is_omnibus(INTENT, None, env=env, target_path=root / "new.ts")[0], False)
    check("large target -> still omnibus",
          m.is_omnibus(INTENT, None, env=env, target_path=big)[0], True)
    check("no target path -> legacy behaviour (omnibus by property count)",
          m.is_omnibus(INTENT, None, env=env)[0], True)
    check("byte cap alone makes a target big (few, very long lines)",
          m.target_is_small(_write(root / "wide.ts", "x" * 20000 + "\n"))[0], False)
    check("failure on a small target -> escalate, never convert",
          m.plan_failure_action("omst-nolabel-xyz", INTENT, env=env, target_path=small),
          "cannot-slice-escalate")
    check("failure on a large target -> convert",
          m.plan_failure_action("omst-nolabel-xyz", INTENT, env=env, target_path=big), "convert")
    # auto passes the target path at BOTH call sites
    tree = ast.parse(AUTO.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("is_omnibus", "plan_failure_action")]
    check("auto passes target_path= to is_omnibus and plan_failure_action",
          sorted((c.func.attr, any(k.arg == "target_path" for k in c.keywords)) for c in calls),
          [("is_omnibus", True), ("plan_failure_action", True)])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def _write(p, text):
    p.write_text(text)
    return p


MUTATIONS = [
    ("OMNI_SRC", "is_omnibus ignores size", "    if small:\n        return False, f\"{why_small}", "    if False:\n        return False, f\"{why_small}"),
    ("OMNI_SRC", "failure path ignores size", "    if target_is_small(target_path)[0]:\n        return \"cannot-slice-escalate\"",
     "    if False:\n        return \"cannot-slice-escalate\""),
    ("OMNI_SRC", "missing file treated as big", "        return True, f\"target {p.name} does not exist yet", "        return False, f\"target {p.name} does not exist yet"),
    ("OMNI_SRC", "byte cap dropped", "if lines < SMALL_TARGET_LINES and len(data) < SMALL_TARGET_BYTES:", "if lines < SMALL_TARGET_LINES:"),
    ("AUTO_SRC", "auto drops target_path (up-front)", "            target_path=Path(a.repo) / target)", "            )"),
]


def revert_check():
    bad = 0
    srcs = {"OMNI_SRC": OMNI, "AUTO_SRC": AUTO}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
