#!/usr/bin/env python3
"""Guards 2026-10-02 idle-test-tsslug (62dcd980447d / f5144683e2a0 output_cap_loop).

Suspected: a backslash round-trip bug in the Must-contain freeze. Measured: NO such
bug -- TASK.md -> check_literals.py (both freeze paths) -> match is byte-exact.
The real failure: refimpl emitted a different regex than the literal (the check
was right), and check_literals printed the missing literal with {!r}, which shows
2 backslashes as 4; the model read that as a freeze bug and looped on escaping.

This test pins: (a) the round trip through BOTH freeze paths (scaffold
--freeze-literals and auto-harness-check's inline freeze) for escape-heavy
literals; (b) the MISSING line shows the literal VERBATIM; (c) the author prompt
steers Must-contain away from regex/escape literals.

Run: python3 test-literal-backslash-roundtrip.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parent
SCAF = Path(os.environ.get("SCAF_SRC") or BIN / "ollama-dispatch-scaffold")
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
FAILS = []
LITS = [r"/\\D/g", r"/\D/g", r"replace(/\s+/g, '-')", r"'\n'", "slugify"]


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


def task_md():
    return ("# TASK\n\n## Must contain\n\n" + "".join(f"- `{l}`\n" for l in LITS)
            + "\n## Scope\nOnly edit `src/x.ts`.\n")


def run_check(root):
    r = subprocess.run([sys.executable, "check_literals.py"], cwd=root, capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    scaf = load(SCAF, "scaf_lit")
    auto = load(AUTO, "auto_lit")
    body_all = "\n".join(LITS) + "\n"
    for path_name in ("scaffold --freeze-literals", "auto-harness-check inline freeze"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "check_literals.py").write_text(scaf.CHECK_LITERALS.format(target="src/x.ts"))
            (root / "TASK.md").write_text(task_md())
            if path_name.startswith("scaffold"):
                scaf.freeze_literals(root)
            else:
                for f in ("verify.sh", "refimpl.py", "test_fixture.py"):
                    (root / f).write_text("")
                auto.write_harness_check(root, "python")
                subprocess.run([sys.executable, "auto-harness-check.py"], cwd=root,
                               capture_output=True, text=True, timeout=60)
            (root / "src/x.ts").write_text(body_all)
            rc, out = run_check(root)
            check(f"{path_name}: escape-heavy literals round-trip and match the same text", rc, 0)
            (root / "src/x.ts").write_text(body_all.replace(r"/\\D/g", "/[^a-z0-9]+/g"))
            rc, out = run_check(root)
            check(f"{path_name}: a really-missing literal still fails", rc, 1)
            check(f"{path_name}: MISSING shows the literal verbatim (2 backslashes, not 4)",
                  ">>>" + r"/\\D/g" + "<<<" in out, True)
    a = SimpleNamespace(intent="slugify", interface="", lang="ts")
    auto.load_attempts = lambda a, runs_dir=None: []
    check("author prompt steers Must-contain away from regex/backslash literals",
          "no regexes, no backslashes" in auto.author_prompt(a, "src/x.ts"), True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    bad = 0
    for var, path, name in (("SCAF_SRC", BIN / "ollama-dispatch-scaffold.bak-litdisplay", "repr display"),
                            ("AUTO_SRC", BIN / "ollama-dispatch-auto.bak-litdisplay", "prompt steer")):
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: str(path)},
                           capture_output=True, text=True, timeout=300)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
