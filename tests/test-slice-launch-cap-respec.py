#!/usr/bin/env python3
"""Hermetic test: the slicer's authoring cap is SIGNATURE-BLIND and names the problem class.

2026-10-09, aw-codec-floor s2: a slice was authored 6+ times over a CHANGING failure
reason (the identical-failure streak never tripped), one re-spec was burned on an
unrelated early error, and the cap then parked it with no one naming the real defect
(a NON-DISCRIMINATING FIXTURE: every case already passed on the original code).

Pinned here (real note_author_failure / respec_on_cap / bound_stale_worktree_retry,
nothing else real):
  1. every launch is counted (author_launches_total) whatever the failure reason, and the
     counter survives a re-spec (author_attempts is zeroed by it);
  2. a changing-reason failure window is classified 'fixture'; at the attempt cap it gets
     ONE re-spec of its own class even though the generic re-spec was already spent, and the
     intent names NON-DISCRIMINATING FIXTURE;
  3. that fixture re-spec is bounded (second one refused), a generic failure gets no extra
     re-spec, and MAX_AUTHOR_LAUNCHES_TOTAL parks the slice regardless of signature;
  4. end to end through the retry path: the lifetime cap ESCALATES; a fixture-class cap trip
     RE-SPECs (returns True, worktree cleared) instead of parking.
--revert-check un-applies the fix two ways and requires the test to go RED.
Marker: LAUNCH_CAP_RESPEC_OK
"""
import contextlib, importlib.util, io, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("sl_lc", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_lc", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    m.QUEUE = os.devnull        # HERMETIC: never the live queue
    return m


def run_retry(m, s):
    m.slice_job_awaiting_gate = lambda st, sid: None
    m.save_state = lambda st: None
    removed = []
    m.remove_worktree = lambda cwt, wt: removed.append(str(wt))
    import inspect
    fn = m.bound_stale_worktree_retry
    st = {"slices": {"sX": s}, "order": ["sX"]}
    args = {"st": st, "sid": "sX", "s": s, "wt": Path(tempfile.mkdtemp()),
            "cwt": Path(tempfile.mkdtemp()), "origin": "test"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r = fn(*[args[p] for p in inspect.signature(fn).parameters])
    return r, out.getvalue(), removed


def main():
    m = load()
    wt = Path(tempfile.mkdtemp())

    # 1. every launch counts, whatever the reason; the counter survives a re-spec
    s = {}
    for i, reason in enumerate(["ERROR: A broke", "ERROR: B broke", "ERROR: C broke"], 1):
        s["last_auto_error"] = reason
        streak, _ = m.note_author_failure(s, wt)
    check("changing reasons: identical streak stays 1", streak, 1)
    check("changing reasons: every launch counted", s.get("author_launches_total"), 3)
    check("changing reasons: classified as a fixture-class stall", m.respec_class(s, "ERROR: C broke"), "fixture")
    check("one repeated reason is NOT fixture-class", m.respec_class({"author_fail_shapes": ["x"]}, "ERROR: X"), "generic")
    check("unexercised-line text IS fixture-class", m.respec_class({}, "preflight: lines 3047-3050 unexercised"), "fixture")
    check("spec defect keeps its own class", m.respec_class({}, m.SPEC_DEFECT_MARK + " x"), "spec_defect")

    # 2. fixture re-spec after the generic one is spent; launches survive; intent names the class
    s.update({"respec_count": m.RESPEC_CAP, "respec_classes": ["generic"], "author_attempts": m.MAX_AUTHOR_ATTEMPTS})
    check("fixture-class cap trip gets its own re-spec", m.respec_on_cap(s, "ERROR: C broke", 1, m.MAX_AUTHOR_ATTEMPTS), True)
    check("re-spec names NON-DISCRIMINATING FIXTURE", "NON-DISCRIMINATING FIXTURE" in s["intent"] and "ORIGINAL code FAILS" in s["intent"], True)
    check("re-spec zeroes attempts", s["author_attempts"], 0)
    check("re-spec keeps the lifetime launch counter", s["author_launches_total"], 3)
    # 3. bounded
    check("second fixture re-spec refused", m.respec_on_cap(s, "ERROR: C broke", 1, 5), False)
    g = {"respec_count": m.RESPEC_CAP, "respec_classes": ["generic"], "author_launches_total": 2}
    check("generic failure gets no extra re-spec", m.respec_on_cap(g, "ERROR: plain", 1, 5), False)
    cap = {"respec_count": 0, "author_launches_total": m.MAX_AUTHOR_LAUNCHES_TOTAL}
    check("lifetime launch cap refuses even the first re-spec", m.respec_on_cap(cap, "ERROR: plain", 1, 5), False)

    # 4. through the real retry path
    s2 = {"author_launches_total": m.MAX_AUTHOR_LAUNCHES_TOTAL - 1, "author_attempts": 1,
          "respec_count": m.RESPEC_CAP, "respec_classes": ["generic"],
          "last_auto_error": "ERROR: yet another reason"}
    r, out, removed = run_retry(m, s2)
    check("lifetime cap ESCALATES through the retry path", (r, s2.get("status")), (False, m.ESCALATED))
    check("lifetime cap names the launch count", "launched %d times" % m.MAX_AUTHOR_LAUNCHES_TOTAL in s2.get("escalation_reason", ""), True)
    s3 = {"author_launches_total": 5, "author_attempts": m.MAX_AUTHOR_ATTEMPTS - 1,
          "respec_count": m.RESPEC_CAP, "respec_classes": ["generic"],
          "author_fail_shapes": ["ERROR: p", "ERROR: q"], "last_auto_error": "ERROR: r"}
    r, out, removed = run_retry(m, s3)
    check("fixture-class cap trip RE-SPECs instead of parking", (r, s3.get("status") == m.ESCALATED, bool(removed)), (True, False, True))
    check("... and the intent carries the class", "NON-DISCRIMINATING FIXTURE" in s3.get("intent", ""), True)

    print("LAUNCH_CAP_RESPEC_OK" if not FAILS else "FAILED: %s" % FAILS)
    return 1 if FAILS else 0


MUTANTS = [
    ("lifetime counter not incremented",
     '    s["author_launches_total"] = int(s.get("author_launches_total") or 0) + 1\n',
     '    pass\n'),
    ("re-spec ignores the lifetime cap",
     '    if int(s.get("author_launches_total") or 0) >= MAX_AUTHOR_LAUNCHES_TOTAL:\n        return False      # hard',
     '    if False:\n        return False      # hard'),
    ("fixture class gets no re-spec of its own",
     'if n >= RESPEC_CAP and not (cls == "fixture" and "fixture" not in classes):',
     'if n >= RESPEC_CAP:'),
    ("fixture re-spec unbounded",
     'if n >= RESPEC_CAP and not (cls == "fixture" and "fixture" not in classes):',
     'if n >= RESPEC_CAP and not cls == "fixture":'),
    ("note does not name the class",
     '    if cls == "fixture":\n        note +=',
     '    if False:\n        note +='),
    ("park ignores the lifetime cap",
     '            or _budget_why or _launches >= MAX_AUTHOR_LAUNCHES_TOTAL):',
     '            or _budget_why):'),
]


def revert_check():
    src = SRC.read_text()
    bad = 0
    for name, old, new in MUTANTS:
        if src.count(old) != 1:
            print("UNAPPLICABLE mutant (%d hits): %s" % (src.count(old), name)); bad += 1; continue
        with tempfile.NamedTemporaryFile("w", suffix="-slice", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites  " if red else "INERT  ") + name)
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else "REVERT-CHECK FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
