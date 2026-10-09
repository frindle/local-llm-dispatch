#!/usr/bin/env python3
"""Repair must_contain literals truncated by the pre-c350afc _TOKEN_RE bug.

THE BUG (fixed for GENERATION in c350afc, pinned by a test in 3726130)
----------------------------------------------------------------------
omnibus_slice._TOKEN_RE's camelCase branch required a LOWERCASE first char, so a
PascalCase identifier failed at position 0, the engine backtracked to position 1,
and the literal FROZEN into a slice's must_contain gate was a leading-char-stripped
tail: ValueError -> 'alueError', FastAPI -> 'astAPI', SeatsAeroClient ->
'eatsAeroClient'.

Fixing the regex does NOT fix existing plans: must_contain is frozen at plan-
authoring time. Those gates are live right now and assert something weaker than
they claim.

WHY A TRUNCATED LITERAL IS A WEAKER GATE
----------------------------------------
'alueError' is a SUBSTRING of 'ValueError', so it still passes wherever the real
identifier is present -- which is exactly why nothing ever failed and the bug went
unnoticed. What it loses is the ability to REJECT. A file containing `SalueError`
or `xalueError` -- a typo, a wrong identifier, a hallucinated name -- satisfies
'alueError' while containing no ValueError at all. The gate stops asserting "this
identifier is present" and starts asserting only "some word ending in this is".

REPAIR RULE (deliberately conservative -- it only ever RESTORES)
---------------------------------------------------------------
For each literal L on a slice, using that slice's own prose (intent/title/etc):
  * if L already appears as a WHOLE WORD in the prose -> leave it alone;
  * else if exactly ONE capital letter C makes C+L appear as a whole word
    -> repair L to C+L;
  * else (no candidate, or more than one) -> leave it alone and REPORT it.
A repair therefore never invents a literal: the restored string must already occur
verbatim in the slice's own text. Ambiguous cases are never guessed.

Default is a DRY RUN. --apply writes, atomically (os.replace), after a .bak.

Never pass --apply for a plan with a live `ollama-dispatch-slice --execute`: that
process holds the same file for read-modify-write and the two writers would clobber
each other. --apply refuses any file whose mtime moves while it works.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PLANS = Path("/Users/user/.ollama-dispatch/slice-plans")
RUNS = Path("/Users/user/.ollama-dispatch/slice-runs")
PROSE_KEYS = ("intent", "title", "verify_shape", "entry_point", "notes")


def prose_of(slice_obj):
    return " ".join(str(slice_obj.get(k) or "") for k in PROSE_KEYS)


def whole_word(word, text):
    if not word:
        return False
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])",
                     text) is not None


def restore(lit, prose):
    """The repaired literal, or None to leave it alone."""
    if not lit or whole_word(lit, prose):
        return None
    cands = sorted({chr(c) + lit for c in range(ord("A"), ord("Z") + 1)
                    if whole_word(chr(c) + lit, prose)})
    return cands[0] if len(cands) == 1 else None


def iter_slices(doc):
    s = doc.get("slices")
    if isinstance(s, dict):
        return list(s.items())
    if isinstance(s, list):
        return [(x.get("id"), x) for x in s if isinstance(x, dict)]
    return []


def plan_repairs(doc):
    """[(slice_id, old, new)] plus [(slice_id, literal, reason)] left alone."""
    fixes, skipped = [], []
    for sid, sl in iter_slices(doc):
        if not isinstance(sl, dict):
            continue
        p = prose_of(sl)
        for lit in (sl.get("must_contain") or []):
            lit = str(lit)
            if whole_word(lit, p):
                continue
            new = restore(lit, p)
            if new:
                fixes.append((sid, lit, new))
            else:
                cands = sorted({chr(c) + lit for c in range(ord("A"), ord("Z") + 1)
                                if whole_word(chr(c) + lit, p)})
                skipped.append((sid, lit,
                                "ambiguous: " + ", ".join(cands) if cands
                                else "no single-capital restoration in the prose"))
    return fixes, skipped


def apply_to_doc(doc, fixes):
    by_slice = {}
    for sid, old, new in fixes:
        by_slice.setdefault(sid, {})[old] = new
    n = 0
    for sid, sl in iter_slices(doc):
        m = by_slice.get(sid)
        if not m or not isinstance(sl, dict):
            continue
        out = []
        for lit in (sl.get("must_contain") or []):
            lit = str(lit)
            if lit in m:
                out.append(m[lit]); n += 1
            else:
                out.append(lit)
        sl["must_contain"] = out
    return n


def process(path, apply_changes):
    try:
        raw = path.read_text()
        doc = json.loads(raw)
    except Exception as e:
        return 0, [f"  !! unreadable: {e}"]
    if not isinstance(doc, dict):
        return 0, []
    fixes, skipped = plan_repairs(doc)
    if not fixes and not skipped:
        return 0, []
    lines = [f"\n## {path.name}"]
    for sid, old, new in fixes:
        lines.append(f"   {sid:40s} {old!r} -> {new!r}")
    for sid, lit, why in skipped:
        lines.append(f"   {sid:40s} {lit!r} LEFT ALONE ({why})")
    if not apply_changes or not fixes:
        return len(fixes), lines

    mtime = path.stat().st_mtime
    n = apply_to_doc(doc, fixes)
    if path.stat().st_mtime != mtime:
        lines.append("   !! REFUSED to write: file changed while working "
                     "(a live --execute holds it). Re-run when it is idle.")
        return 0, lines
    bak = path.with_name(path.name + ".bak-truncfix")
    if not bak.exists():
        shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".tmp-truncfix")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)
    lines.append(f"   -> wrote {n} repair(s) (backup: {bak.name})")
    return n, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs (default is a dry run)")
    ap.add_argument("--skip", default="",
                    help="comma-separated plan-name substrings to leave untouched "
                         "(use for any plan with a live --execute)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    skips = [s.strip() for s in a.skip.split(",") if s.strip()]
    total = 0
    for root, pat in ((PLANS, "*.slices.json"), (RUNS, "*.json")):
        if not root.is_dir():
            continue
        print(f"\n===== {root}")
        for f in sorted(root.glob(pat)):
            if any(s in f.name for s in skips):
                print(f"\n## {f.name}\n   SKIPPED (live writer)")
                continue
            n, lines = process(f, a.apply)
            total += n
            for l in lines:
                print(l)
    print(f"\n==== {'repaired' if a.apply else 'would repair'}: {total} literal(s)")
    if not a.apply:
        print("     dry run -- re-run with --apply to write")
    return 0


def self_test():
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"  [ok  ] {name}")
        else:
            ok = False
            print(f"  [FAIL] {name} -> got {got!r} want {want!r}")

    # --- the repair rule itself -------------------------------------------
    p = "Raise ValueError when the broker id is blank."
    check("restores a truncated literal", restore("alueError", p), "ValueError")
    check("leaves a correct literal alone", restore("ValueError", p), None)
    check("no restoration when the prose lacks it", restore("alueError", "nothing"), None)
    check("does not invent a literal absent from the prose",
          restore("otAWord", "unrelated prose"), None)
    amb = "Handle AFoo and BFoo the same way."
    check("ambiguous restoration is refused", restore("Foo", amb), None)
    check("whole-word: substring of a longer identifier does not count",
          whole_word("Value", "MyValueError"), False)
    check("whole-word: real occurrence counts", whole_word("ValueError", p), True)

    # --- the gate-strength property this exists for -----------------------
    # A truncated literal cannot REJECT a wrong identifier; the repaired one can.
    wrong = 'raise SalueError("x")'
    check("BEFORE: truncated literal passes on a WRONG identifier",
          "alueError" in wrong, True)
    check("AFTER: repaired literal rejects that same file",
          "ValueError" in wrong, False)
    right = 'raise ValueError("x")'
    check("AFTER: repaired literal still passes on the RIGHT identifier",
          "ValueError" in right, True)

    # --- end-to-end on a doc, both schemas --------------------------------
    doc = {"slices": {"s1": {"intent": "Raise ValueError on blank input.",
                             "must_contain": ["alueError", "broker_id"]}}}
    fixes, skipped = plan_repairs(doc)
    check("doc: finds exactly the truncated literal", fixes, [("s1", "alueError", "ValueError")])
    check("doc: leaves the healthy literal out of the fix list",
          [f for f in fixes if f[1] == "broker_id"], [])
    apply_to_doc(doc, fixes)
    check("doc: must_contain rewritten in place",
          doc["slices"]["s1"]["must_contain"], ["ValueError", "broker_id"])
    check("doc: idempotent -- a second pass finds nothing",
          plan_repairs(doc)[0], [])

    lst = {"slices": [{"id": "s1", "intent": "use FastAPI here",
                       "must_contain": ["astAPI"]}]}
    f2, _ = plan_repairs(lst)
    apply_to_doc(lst, f2)
    check("list-schema plans are handled too",
          lst["slices"][0]["must_contain"], ["FastAPI"])

    print()
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
