#!/usr/bin/env python3
"""A hand-edit to a harness file made WHILE auto-harness-check.py (or the preflight's refimpl step)
runs must survive its revert (2026-10-09, rt-walmart-cancel-import).

Root cause: the check snapshots every untracked/dirty file's bytes up front and revert() replays
them all unconditionally -- TASK.md, check_literals.py, refimpl.py, verify.sh included. The owner removed
an unsatisfiable Must-contain literal and re-froze; minutes later the check finished and rewrote the
OLD bytes, twice. Now a harness file is replayed only if its bytes are still what the check last left.

  test-harness-operator-edit-survives.py                 # the live ollama-dispatch-auto template
  test-harness-operator-edit-survives.py --revert-check  # the pre-fix template (ollama-dispatch-auto.bak-20261009-opedit): must go RED
"""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVERT = "--revert-check" in sys.argv
SRC = HERE / ("ollama-dispatch-auto.bak-20261009-opedit" if REVERT else "ollama-dispatch-auto")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:400]))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    old = sys.argv
    sys.argv = [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = old
    return m


oda = load(SRC, "oda_opedit")
ORIG_TASK = "## Must contain\n- `MARK`\n- `GHOST`\nOnly edit `target.txt`; do not edit verify.sh\n"


def build(verify_extra, refimpl_extra=""):
    wt = tempfile.mkdtemp(prefix="opedit-")
    g = lambda *a: subprocess.run(["git", "-C", wt, *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    Path(wt, "TASK.md").write_text(ORIG_TASK)
    # verify.sh: $OPEDIT is the simulated operator, editing TASK.md mid-check (on the refimpl-applied run)
    Path(wt, "verify.sh").write_text(
        'f=0\ngrep -q MARK target.txt 2>/dev/null || { echo "no MARK"; f=1; }\n'
        + verify_extra +
        'echo "--- $f failed ---"; [ "$f" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    Path(wt, "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n" + refimpl_extra)
    Path(wt, "target.txt").write_text("stub\n")
    Path(wt, "test_fixture.py").write_text("# fixture\n")
    g("add", "-A"); g("commit", "-q", "-m", "base")
    oda.write_harness_check(Path(wt), "python")
    return wt


def run(wt):
    return subprocess.run(["python3", "auto-harness-check.py"], cwd=wt, capture_output=True, text=True)


EDIT = "# operator edit: GHOST literal removed\\n"
# 1. operator edits TASK.md while the refimpl-applied verify runs (the walmart shape)
wt = build('[ -f target.txt ] && grep -q MARK target.txt && printf "' + EDIT + '" >> TASK.md\n')
r = run(wt)
t = Path(wt, "TASK.md").read_text()
check("the check itself ran green", r.returncode == 0 and "VERIFY_OK" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
check("operator edit made mid-check SURVIVES the revert", "operator edit" in t, t)
check("the target is still reverted to HEAD (the check still cleans up after itself)",
      Path(wt, "target.txt").read_text() == "stub\n")

# 2. a harness file the REFIMPL clobbers is still restored (the guard did not turn restore off)
wt2 = build("", "open('TASK.md','w').write('CLOBBERED BY REFIMPL\\n')\n")
r2 = run(wt2)
check("a harness file clobbered by the refimpl itself is still restored to its pre-check bytes",
      Path(wt2, "TASK.md").read_text() == ORIG_TASK, Path(wt2, "TASK.md").read_text())

# 3. untouched harness: byte-identical after the check
wt3 = build("")
run(wt3)
check("untouched harness files are byte-identical after the check", Path(wt3, "TASK.md").read_text() == ORIG_TASK)

# 4. preflight twin: _may_restore_harness / _note_post_refimpl
pf_path = HERE / ("ollama-dispatch-preflight.bak-20261009-opedit" if REVERT else "ollama-dispatch-preflight")
pf = load(pf_path, "pf_opedit")
w = Path(tempfile.mkdtemp(prefix="opedit-pf-"))
(w / "TASK.md").write_text("pre\n")
(w / "target.txt").write_text("stub\n")
p = pf.Preflight(type("A", (), {"worktree": str(w)})())
if hasattr(p, "_note_post_refimpl"):
    p._pre_refimpl_bytes = {"TASK.md": b"pre\n", "target.txt": b"stub\n"}
    (w / "target.txt").write_text("SOLUTION\n")           # the refimpl clobbered the creation stub
    p._note_post_refimpl()
    (w / "TASK.md").write_text("operator edit\n")        # hand edit during the preflight
    p.git = lambda *a, **k: (0, "", ""); p.sh = lambda *a, **k: (0, "")
    p.revert_refimpl()
    check("preflight: operator-edited TASK.md survives revert_refimpl", (w / "TASK.md").read_text() == "operator edit\n")
    check("preflight: the clobbered creation stub is still restored", (w / "target.txt").read_text() == "stub\n")
else:
    check("preflight: operator-edited TASK.md survives revert_refimpl", False, "no _note_post_refimpl (pre-fix source)")

print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "OPERATOR_EDIT_OK")
if REVERT:
    print("REVERT-CHECK:", "RED as required" if FAILS else "STILL GREEN -- the suite does not bite")
    sys.exit(0 if FAILS else 1)
sys.exit(1 if FAILS else 0)
