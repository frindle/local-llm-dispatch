#!/usr/bin/env python3
"""Authoring continuation rounds reset an UNTRACKED creation target to its scaffold
STUB, and `auto-author-<label>-cN` (--continues) rounds count as continuations
(ollama-queue.py seal_prev_round_baseline / _reset_path_to_head /
_is_continuation_round, 2026-10-06).

Canary soak seed 4 (pcanary s2-fmt, creation target lib/fmt.ts):
  * the c1 author continuation was not recognised as a continuation -> it launched
    "dirty" (the previous author round's target edit) -> gate UNTRUSTED;
  * the r1 refine round's seal "reset" the untracked creation target by DELETING
    it -> target-parses NO-GO on every later round -> NO PROGRESS -> ESCALATED.

Hermetic: temp git repos. QS_SRC points at another queue source (--revert-check)."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("QS_SRC") or HERE / "ollama-queue.py")
FAILS = []
STUB = "// Stub for lib/fmt.ts -- implement per TASK.md.\nexport {};\n"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def repo(root, creation=True):
    root.mkdir(parents=True)
    g(root, "init", "-q")
    (root / "lib").mkdir()
    (root / "lib" / "calc.ts").write_text("export const c = 1;\n")
    g(root, "add", "-A")
    g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    hj = {"authored": ["TASK.md", "verify.sh", "refimpl.py"],
          "target": "lib/fmt.ts" if creation else "lib/calc.ts", "creation_task": creation}
    (root / ".dispatch-harness.json").write_text(json.dumps(hj))
    for f in hj["authored"]:
        (root / f).write_text(f"# {f}\n")
    return root


def main():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="cr-home-")
    ld = SourceFileLoader("oq_cr", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_cr", ld))
    ld.exec_module(m)
    T = Path(tempfile.mkdtemp(prefix="cr-"))

    # 1. refine round over an untracked creation target holding the author round's
    #    (artifact) implementation -> reset to the STUB, not deleted
    r = repo(T / "a")
    (r / "lib" / "fmt.ts").write_text("export function formatRange(a,b){return `[${a}, ${b}]`}\n")
    res = m.seal_prev_round_baseline({"label": "auto-refine-p-s2-fmt-r1", "cwd": str(r)})
    check("refine round: creation target reported reset", (res or {}).get("target_reset"), "lib/fmt.ts")
    check("...the target still EXISTS (not deleted)", (r / "lib" / "fmt.ts").is_file(), True)
    check("...and holds the scaffold creation stub",
          (r / "lib" / "fmt.ts").read_text() if (r / "lib" / "fmt.ts").is_file() else None, STUB)
    check("...launch baseline is clean (stub exempt, harness sealed)",
          (m.measure_baseline(str(r)) or {}).get("dirty"), 0)

    # 2. author continuation -cN with --continues -> a continuation round
    check("auto-author-<label>-c1 with continues is a continuation round",
          m._is_continuation_round({"label": "auto-author-p-s2-fmt-c1", "continues": "abc"}), True)
    check("...without continues it is NOT (first-pass guard intact)",
          m._is_continuation_round({"label": "auto-author-p-s2-fmt-c1"}), False)
    check("...the first author pass is NOT",
          m._is_continuation_round({"label": "auto-author-p-s2-fmt", "continues": "abc"}), False)
    r = repo(T / "b")
    (r / "lib" / "fmt.ts").write_text("export const half = 1;\n")
    res = m.seal_prev_round_baseline({"label": "auto-author-p-s2-fmt-c1", "continues": "x",
                                      "cwd": str(r)})
    check("c1 round: sealed + target reset to stub",
          ((res or {}).get("target_reset"), (r / "lib" / "fmt.ts").read_text()
           if (r / "lib" / "fmt.ts").is_file() else None), ("lib/fmt.ts", STUB))
    check("...and launches CLEAN (no UNTRUSTED dirty baseline)",
          (m.measure_baseline(str(r)) or {}).get("dirty"), 0)

    # 3. guards not weakened
    r = repo(T / "c", creation=False)
    (r / "lib" / "calc.ts").write_text("export const c = 2;\n")
    m.seal_prev_round_baseline({"label": "auto-refine-p-s1-r1", "cwd": str(r)})
    check("tracked (non-creation) target still reset to HEAD",
          (r / "lib" / "calc.ts").read_text(), "export const c = 1;\n")
    r = repo(T / "d")
    (r / "lib" / "fmt.ts").write_text("export const impl = 1;\n")
    check("a first-round coding job is never sealed",
          m.seal_prev_round_baseline({"label": "p-s2-fmt", "cwd": str(r)}), None)
    check("...its untracked non-stub target still counts as launch dirt",
          (m.measure_baseline(str(r)) or {}).get("dirty"), 1)
    r = repo(T / "e")
    (r / "lib" / "fmt.ts").write_text("export const impl = 1;\n")
    m.seal_prev_round_baseline({"label": "p-s2-fmt [auto-fix r1]", "auto_fix_round": 1,
                                "cwd": str(r)})
    check("a CODING continuation keeps its target (the deliverable)",
          (r / "lib" / "fmt.ts").read_text(), "export const impl = 1;\n")

    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--revert-check":
        env = dict(os.environ, QS_SRC=sys.argv[2])
        p = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True)
        print(p.stdout[-1500:])
        if p.returncode == 0:
            print("REVERT-CHECK FAILED: pre-fix source passes -- test is inert")
            sys.exit(1)
        print("REVERT-CHECK OK: pre-fix source goes RED")
        sys.exit(0)
    sys.exit(main())
