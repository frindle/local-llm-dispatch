#!/usr/bin/env python3
"""Both-ways proof that a scaffolded Python fixture can IMPORT a dataclass target.

The scar (2026-09-17, found by the broker-guard build): the generated fixture
loads the target with importlib and never registers it in `sys.modules`. A module
loaded that way has no sys.modules entry, so `sys.modules[cls.__module__]` is
None -- and on Python 3.14 (the Studio worker's interpreter) `dataclasses`
resolves string annotations through exactly that lookup. A target carrying
`from __future__ import annotations` + `@dataclass` therefore died at IMPORT with

    AttributeError: 'NoneType' object has no attribute '__dict__'

...which false-failed EVERY dataclass dispatch for a reason that has nothing to
do with the task. A verify that fails identically before and after the model's
edit is indistinguishable, from the outside, from model incapacity.

Both ways, because a guard nobody proved bites is not a guard:
  * WITH the `sys.modules["target"] = target` line -- the fixture imports.
  * WITHOUT it (the line stripped back out) -- the ORIGINAL AttributeError
    returns. If this half stops reproducing, the test is no longer measuring
    anything and must be re-anchored, not deleted.

Run: test-scaffold-dataclass-import.py [-v]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
SCAFFOLD = BIN / "ollama-dispatch-scaffold"

# The exact shape that breaks: future-annotations (so the annotations are
# strings) + a dataclass (so something has to resolve them through sys.modules).
DATACLASS_TARGET = '''\
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Row:
    """A row of the thing under test."""
    count: int = 0
    flag: bool = False


def is_safe(row: Row) -> bool:
    # BUG: ignores flag entirely.
    return row.count > 0
'''

REGISTER_LINE = 'sys.modules["target"] = target'


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def scaffold(root: Path):
    """Generate a real scaffold around DATACLASS_TARGET; return the fixture."""
    proj = root / "proj"
    proj.mkdir(parents=True)
    (proj / "target.py").write_text(DATACLASS_TARGET)
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "baseline"]):
        run(c, cwd=str(proj))
    p = run([sys.executable, str(SCAFFOLD), "--repo", str(proj),
             "--dest", str(root / "wt"), "--label", "dcimport",
             "--lang", "python", "--target", "target.py",
             "--kind", "symbol", "--symbol", "is_safe"])
    wt = root / "wt"
    fixtures = sorted(wt.glob("test_*.py")) + sorted(wt.glob("*fixture*.py"))
    if not fixtures:
        return None, f"scaffold produced no python fixture (rc={p.returncode}): " \
                     f"{(p.stdout + p.stderr)[-400:]}"
    return fixtures[0], None


def imports_cleanly(fixture: Path, cwd: Path):
    """Import the fixture's module-load preamble only. Returns (ok, stderr)."""
    # Run the fixture itself: it exits non-zero on "author the cases", which is
    # FINE -- we are asserting only that it got past the import of the target.
    p = run([sys.executable, str(fixture)], cwd=str(cwd))
    err = p.stdout + p.stderr
    return ("AttributeError" not in err and "Traceback" not in err), err


def main():
    verbose = "-v" in sys.argv
    failures = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fixture, why = scaffold(root)
        if fixture is None:
            print(f"  FAIL scaffold-generates-fixture  {why}")
            print("\n--- 1 of 1 failed ---")
            return 1
        text = fixture.read_text()

        # 1) The generated fixture carries the registration at all.
        ok = REGISTER_LINE in text
        print(f"  {'ok  ' if ok else 'FAIL'} registers-in-sys-modules   "
              f"{REGISTER_LINE!r} {'present' if ok else 'MISSING'}")
        if not ok:
            failures.append("registers-in-sys-modules")

        # 2) ...and it actually imports a future-annotations dataclass target.
        ok, err = imports_cleanly(fixture, fixture.parent)
        print(f"  {'ok  ' if ok else 'FAIL'} dataclass-target-imports   "
              f"{'no import error' if ok else err.strip()[-200:]}")
        if not ok:
            failures.append("dataclass-target-imports")
        if verbose:
            print(err)

        # 3) REVERT TEST: strip the line back out and the original failure must
        #    come back. Without this the check above could be passing for an
        #    unrelated reason and nobody would know.
        reverted = fixture.parent / "reverted_fixture.py"
        reverted.write_text(text.replace(REGISTER_LINE + "\n", ""))
        ok_rev, err_rev = imports_cleanly(reverted, fixture.parent)
        bites = (not ok_rev) and "NoneType" in err_rev
        print(f"  {'ok  ' if bites else 'FAIL'} guard-bites-when-removed  "
              f"{'AttributeError NoneType returns' if bites else 'NO LONGER REPRODUCES -- re-anchor this test'}")
        if not bites:
            failures.append("guard-bites-when-removed")
        if verbose:
            print(err_rev)

    print(f"\n--- {len(failures)} of 3 failed ---")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("SELF_CHECK_OK: a scaffolded fixture imports a dataclass target, and "
          "the registration is what makes it work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
