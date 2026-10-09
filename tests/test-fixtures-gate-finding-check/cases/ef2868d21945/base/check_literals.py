
"""Assert every `## Must contain` literal from TASK.md is in the right file.

ADDITIONAL to the behavioural cases, never a replacement: a literal check is a
proxy and passes on a file that contains the right text and does the wrong
thing. But a verify asserting NO spec literal cannot tell a correct
implementation from an inverted one -- a whole literal cannot match its own
negation. So: both.

LITERALS is written here VERBATIM by `ollama-dispatch-scaffold --freeze-literals`
rather than read from TASK.md at run time. A dynamic read is better engineering
and worse evidence: the literal never appears in the verify text, so a grader
scanning the verify cannot see that it is asserted, and verify-quality.py
correctly reported "the verify asserts none of them". The drift guard below is
what makes a second copy safe -- two records of one fact drift, always.

PER-FILE literals: a `## Must contain` bullet may PIN a literal to a specific
file, e.g.  `- in app/api/x/route.ts: ` + a backtick-quoted token. That literal
is then required in THAT file, not the default TARGET. A bare bullet (just a
backtick-quoted token) keeps checking TARGET. This lets a multi-file fix (a
helper file + the route wiring that calls it) pin each side to the file it
belongs in, instead of asserting everything against one file. Canonical frozen
form is a list of [file_or_null, literal] pairs; a legacy flat list of bare
strings is still accepted (each treated as a TARGET literal).
"""
import pathlib
import re
import sys

TARGET = pathlib.Path('dashboard_chat.py')

# frozen from TASK.md -- re-run `ollama-dispatch-scaffold --freeze-literals .`
LITERALS = [[None, 'enqueue_job'], [None, 'start_direct'], [None, 'is_job_request'], [None, 'daemon=True'], [None, 'threading'], [None, '"mode"']]


def _parse_must_contain(task_text):
    """(file_or_None, literal) pairs from the `## Must contain` block. A line
    carrying `in <path>:` pins every backtick-literal on that line to <path>;
    otherwise the literal is a default-TARGET literal (file None). Must stay in
    lockstep with freeze_literals in ollama-dispatch-scaffold, or the drift
    guard below false-fires."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)",
                  task_text, re.S | re.I)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        if not re.match(r"\s*-\s", line):
            continue  # only real `- ` list items; skip instructional prose/parentheticals
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        f = fm.group(1) if fm else None
        for lm in re.finditer(r"`([^`\n]{1,200})`", line):
            lit = lm.group(1)
            if "TODO" in lit:
                continue
            out.append([f, lit])
    return out


def _norm(entry):
    """Normalise a LITERALS entry to [file_or_None, literal]; a legacy bare
    string becomes a TARGET literal so old frozen files keep working."""
    if isinstance(entry, str):
        return [None, entry]
    return [entry[0], entry[1]]


frozen = [_norm(e) for e in LITERALS]
current = _parse_must_contain(pathlib.Path("TASK.md").read_text())

if not frozen:
    if pathlib.Path("auto-harness-check.py").is_file():
        # an AUTO-authoring worktree: the self-check freezes them (the authoring
        # model cannot run the scaffold and must not edit this file)
        print("  no frozen literals -- run: python3 auto-harness-check.py "
              "(it freezes the TASK.md Must-contain list first)")
    else:
        print("  no frozen literals -- run: ollama-dispatch-scaffold "
              "--freeze-literals .")
    sys.exit(1)
if frozen != current:
    print("  literals DRIFTED from TASK.md -- re-run --freeze-literals")
    print("    frozen : {!r}".format(frozen))
    print("    TASK.md: {!r}".format(current))
    sys.exit(1)

_bodies = {}
def _body(path):
    if path not in _bodies:
        p = pathlib.Path(path) if path else TARGET
        _bodies[path] = p.read_text() if p.is_file() else ""
    return _bodies[path]

missing = [[f, l] for f, l in frozen if l not in _body(f)]
# VERBATIM, not repr (2026-10-02, idle-test-tsslug 62dcd980447d): {!r} printed the
# 2-backslash literal /\\D/g as '/\\\\D/g', the model read that as a 4-backslash
# freeze bug and burned its last turns on escaping -- the check was right all along
# (refimpl emitted a different regex). Show the exact bytes searched for.
for f, l in missing:
    print("  MISSING literal in {} (searched for these exact {} chars, shown verbatim "
          "between the >>> <<< marks): >>>{}<<<".format(f or TARGET, len(l), l))
print("  {}/{} literal(s) present".format(len(frozen) - len(missing), len(frozen)))
sys.exit(1 if missing else 0)
