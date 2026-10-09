#!/usr/bin/env python3
"""A MODIFY slice's TS symbol is inferred from the TARGET's exports (2026-10-02).
Rivian s5 intent "Change the existing fetchRivianServiceState() so it ..." matched
none of the declaration patterns, auto passed no --symbol, and the scaffold died
rc=2 (deterministic escalation; s3 hit the same). Prose words followed by "(" must
NOT be picked -- only names the target file exports. Both call sites wire the path.
--revert-check mutates ollama-dispatch-auto (AUTO_SRC env) and requires RED."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("oda_tsm", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_tsm", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    d = Path(tempfile.mkdtemp(prefix="tsm-"))
    t = d / "rivian.ts"
    t.write_text("export async function fetchRivianServiceState() {}\n"
                 "export const helper = 1;\nfunction internal() {}\n")
    intent = ("Change the existing fetchRivianServiceState() so it resolves "
              "Promise<RivianServiceSnapshot | null>; keep (unchanged) the gateways.")
    check("modify intent + target -> the exported function it names",
          m.ts_symbol_from_spec(intent, None, target_path=t), "fetchRivianServiceState")
    check("no target -> unchanged legacy behaviour (None)", m.ts_symbol_from_spec(intent, None), None)
    check("a called name the target does NOT export is never picked",
          m.ts_symbol_from_spec("Make internal() call keep(x)", None, target_path=t), None)
    check("declaration pattern still wins first",
          m.ts_symbol_from_spec("Export function newThing(x): number", None, target_path=t), "newThing")
    src = AUTO.read_text()
    check("scaffold call site passes target_path", "target_path=(Path(a.repo) / target)" in src, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("fallback removed", "    if target_path:\n        exported = _exported_names(target_path)",
     "    if False:\n        exported = _exported_names(target_path)"),
    ("any called name accepted", "                if m.group(1) in exported:\n                    return m.group(1)",
     "                if True:\n                    return m.group(1)"),
    ("call site drops target", "            target_path=(Path(a.repo) / target) if (a.repo and not a.new_project) else None)",
     "            target_path=None)"),
]


def revert_check():
    bad = 0
    src = AUTO.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
