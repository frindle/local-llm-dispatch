#!/usr/bin/env python3
"""Hermetic test: harness-authoring aids born from the aw-codec-floor s2b hand-repair (2026-10-09).

Pins, against the REAL scaffold / slicer / dispatch_authoring_aids.py (nothing else real, no queue,
no model, no network):

  A. HEAVY-GLOBAL FIXTURE EXAMPLE: a python target that is big (> DISPATCH_HEAVY_TARGET_LINES) or has a
     top-level IO import gets a worked example in the SCAFFOLDED fixture stub (load -> patch every IO
     global AFTER exec_module and BEFORE the call; frame-local probe); a light target does not; the stub
     stays inert; and the example is PROVEN to work (it observes a local-only change, and the real
     network-calling global never runs).
  B. NO INVENTED DEFECTS: the scaffold's TASK.md placeholder offers "behavior to add" and forbids
     uninvented consumers; defect_section_findings flags an invented consumer / a defect claim on an
     additive intent and passes an honest section.
  C. SELF-CHECK REPORT: selfcheck_failure_report names the stage, file:line and first failing assertion
     (a NameError in the fixture is a FIXTURE error, not "refimpl.py does not make verify.sh pass").
  D. RUNBOOK: the ollama-dispatch skill documents hand-repair + --auto-seal / --seal-baseline.
  E. EDIT-SURFACE LINT: the slicer WARNS (never blocks) when a slice's target is > DISPATCH_EDIT_SURFACE_LINES
     lines with no excerpt/extraction hint; the warning is in the plan output (dry_run); thresholds are
     env-tunable; an excerpt hint / surface_ack / WARN=0 silence it.

  test-authoring-aids.py [--bin DIR]
Marker: AUTHORING_AIDS_OK
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
import pwd
SKILL = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "skills" / "ollama-dispatch" / "SKILL.md"
FAILS = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(extra)[:400]))
    if not ok:
        FAILS.append(name)


def load_aids():
    p = BIN / "dispatch_authoring_aids.py"
    if not p.is_file():
        return None
    sp = importlib.util.spec_from_file_location("dispatch_authoring_aids_t", p)
    m = importlib.util.module_from_spec(sp)
    sys.modules["dispatch_authoring_aids_t"] = m
    sp.loader.exec_module(m)
    return m


def load_script(name, alias):
    ld = SourceFileLoader(alias, str(BIN / name))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(alias, ld))
    old = sys.argv
    sys.argv = [str(BIN / name)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = old
    return m


@contextlib.contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


aids = load_aids()
check("module dispatch_authoring_aids.py exists and loads", aids is not None)
if aids is None:
    print(f"\n--- {len(FAILS)} failed ---")
    sys.exit(1)

# ---------------------------------------------------------------- A. heavy target
HEAVY_SRC = '''\
import sqlite3
import logging

log = logging.getLogger("x")
_recent = set()


def login_to_service():
    raise RuntimeError("REAL NETWORK CALL RAN: login_to_service")


def get_all_items():
    raise RuntimeError("REAL NETWORK CALL RAN: get_all_items")


def handler(source, data):
    login_to_service()
    items = get_all_items()
    name = None
    if source == "a":
        name = data.get("name") or "fallback"
    incoming_name = name
    log.info("will skip new torrent %s", incoming_name)
    return sorted(items)
'''
LIGHT_SRC = "def f(x):\n    return x + 1\n"

check("A heavy: top-level IO import (sqlite3) is heavy", bool(aids.heavy_python_reasons(HEAVY_SRC, "m.py")))
check("A heavy: urllib.request is IO, urllib.parse is not",
      bool(aids.heavy_python_reasons("import urllib.request\n", "m.py"))
      and not aids.heavy_python_reasons("import urllib.parse\n", "m.py"))
check("A light: pure small module is NOT heavy", aids.heavy_python_reasons(LIGHT_SRC, "m.py") == [])
check("A size rule: 1501 pure lines is heavy, 1500 is not",
      bool(aids.heavy_python_reasons("x = 1\n" * 1501, "m.py"))
      and not aids.heavy_python_reasons("x = 1\n" * 1500, "m.py"))
with env(DISPATCH_HEAVY_TARGET_LINES="10"):
    check("A size threshold is env-tunable", bool(aids.heavy_python_reasons("x = 1\n" * 11, "m.py")))
with env(DISPATCH_HEAVY_TARGET="0"):
    check("A DISPATCH_HEAVY_TARGET=0 disables", aids.heavy_python_reasons(HEAVY_SRC, "m.py") == [])
check("A non-python target is never heavy", aids.heavy_python_reasons(HEAVY_SRC, "m.ts") == [])
check("A io candidates found", {"login_to_service", "get_all_items"} <= set(aids.io_candidate_globals(HEAVY_SRC)),
      aids.io_candidate_globals(HEAVY_SRC))

SESS_SRC = ("import requests\nsession = requests.Session()\n"
            "def relabel(h):\n    return session.post('u', json={}).json()\n"
            "def pure(x):\n    return x + 1\n")
check("A io candidates: function using a module-level `session = requests.Session()` is IO",
      "relabel" in aids.io_candidate_globals(SESS_SRC) and "pure" not in aids.io_candidate_globals(SESS_SRC),
      aids.io_candidate_globals(SESS_SRC))
_many = "import requests\n" + "".join(f"def get_all_{i}():\n    return requests.get('u')\n" for i in range(40))
_sec = aids.heavy_prompt_section("m.py", _many)
check("A prompt lists more than 25 IO candidates (the handler's stubs sit past the old cut)",
      "get_all_39" in _sec, _sec[-300:])

# scaffold end to end
sc = load_script("ollama-dispatch-scaffold", "sc_aids")


def scaffold(src, target="m.py"):
    td = Path(tempfile.mkdtemp(prefix="aids-"))
    proj = td / "proj"
    proj.mkdir()
    (proj / target).write_text(src)
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "b"]):
        subprocess.run(c, cwd=str(proj), capture_output=True)
    p = subprocess.run([sys.executable, str(BIN / "ollama-dispatch-scaffold"), "--repo", str(proj),
                        "--dest", str(td / "wt"), "--label", "aids", "--lang", "python", "--target", target,
                        "--kind", "symbol", "--symbol", "handler",
                        "--property", "Add a local variable incoming_name before the log line"],
                       capture_output=True, text=True)
    return td / "wt", p


wt, p = scaffold(HEAVY_SRC)
fx = (wt / "test_fixture.py").read_text() if (wt / "test_fixture.py").is_file() else ""
check("A scaffold produced the python fixture", bool(fx), (p.stdout + p.stderr)[-300:])
check("A heavy stub carries the HEAVY-GLOBAL worked example",
      "HEAVY-GLOBAL TARGET" in fx and "_patch_io" in fx and "sys._getframe" in fx
      and "AFTER exec_module" in fx, fx[:300])
check("A heavy stub names the detected IO globals", "login_to_service" in fx and "get_all_items" in fx)
check("A heavy stub is still a valid inert scaffold (compiles; CASES empty -> SCAFFOLD guard fires)",
      bool(fx) and compile(fx, "t", "exec") is not None and "SCAFFOLD_INCOMPLETE" in fx
      or "mark" in fx)
r = subprocess.run([sys.executable, "test_fixture.py"], cwd=str(wt), capture_output=True, text=True)
check("A heavy stub runs and fails the empty-CASES guard (not an import crash)",
      r.returncode != 0 and "Traceback" not in (r.stdout + r.stderr), (r.stdout + r.stderr)[-300:])
wt2, p2 = scaffold(LIGHT_SRC)
fx2 = (wt2 / "test_fixture.py").read_text() if (wt2 / "test_fixture.py").is_file() else ""
check("A light stub has NO heavy example", bool(fx2) and "HEAVY-GLOBAL" not in fx2)

# the example is PROVEN, not just present: fill its placeholders and run it
ex = aids._example_code("m.py", ["login_to_service", "get_all_items"])
ex = ex.replace('    # "login_to_service": lambda *a, **k: None,', '    "login_to_service": lambda *a, **k: None,')
ex = ex.replace('    # "get_all_items": lambda *a, **k: dict(ITEMS),', '    "get_all_items": lambda *a, **k: {"b": 1, "a": 2},')
ex = ex.replace("<substring of the log line that runs AFTER the local is set>", "will skip new torrent")
ex = ex.replace("<local_name>", "incoming_name")
ex = ex.replace("target.handler(*handler_args)", "target.handler(*handler_args)")
ex += '''
got_a = _run(("a", {"name": "N1"}))
got_b = _run(("b", {}))
print("RESULT", got_a, got_b)
'''
td = Path(tempfile.mkdtemp(prefix="aids-ex-"))
(td / "m.py").write_text(HEAVY_SRC)
(td / "ex.py").write_text(ex)
r = subprocess.run([sys.executable, "ex.py"], cwd=str(td), capture_output=True, text=True)
check("A example runs: IO globals patched after exec_module (real network global never ran), local observed "
      "from the caller frame", r.returncode == 0 and "RESULT ['N1'] [None]" in r.stdout, (r.stdout + r.stderr)[-400:])
# ...and at baseline (local does not exist) the probe reports MISSING -> the case would FAIL for the right reason
(td / "m_base.py").write_text(HEAVY_SRC.replace("    incoming_name = name\n", "").replace("%s\", incoming_name", "%s\", name"))
ex_b = ex.replace("'m.py'", "'m_base.py'")
(td / "ex_b.py").write_text(ex_b)
r = subprocess.run([sys.executable, "ex_b.py"], cwd=str(td), capture_output=True, text=True)
check("A example at baseline reports the local as MISSING (discriminates)",
      r.returncode == 0 and "<local undefined>" in r.stdout, (r.stdout + r.stderr)[-300:])
# the prompt section (for ollama-dispatch-auto's author prompt)
sec = aids.heavy_prompt_section("m.py", HEAVY_SRC)
check("A prompt section present for heavy, empty for light",
      "HEAVY-GLOBAL TARGET" in sec and "```python" in sec and aids.heavy_prompt_section("m.py", LIGHT_SRC) == "")

# ---------------------------------------------------------------- B. no invented defects
class NS:
    pass


a = NS()
a.label, a.target, a.defect = "x", "m.py", None
a.property = "Add a local variable incoming_name before the log line"
a.entry_point, a.edit_file, a.fixture_rel = None, [], "test_fixture.py"
a.lang = "python"
try:
    t = sc.task_md(a)
except Exception as e:       # pragma: no cover
    t = "EXC " + repr(e)
check("B scaffold TASK.md placeholder offers 'BEHAVIOR TO ADD' and forbids invented consumers",
      "BEHAVIOR TO ADD" in t and "callers, logging, activity records" in t and "TODO" in t, t[:400])
check("B placeholder tells an ADDITIVE intent to use the behavior-to-add form",
      "intent is ADDITIVE" in t)
a.property = "Fix the off-by-one in clamp()"
t2 = sc.task_md(a)
check("B a fix-shaped intent is not told it is additive", "intent is ADDITIVE" not in t2)
check("B heading kept (preflight task-shape / gate priority key on it)", "## Confirmed defect" in t)

TARGET = "def handler(x):\n    name = x\n    log.info('will skip %s', name)\n"
GOOD = ("# T\n## Confirmed defect (observed, not suspected)\n\nBehavior to add (no defect observed at baseline): "
        "`handler` does not define `incoming_name` before the log line (m.py:3).\n\n## Required change\nx\n")
BAD = ("# T\n## Confirmed defect (observed, not suspected)\n\nThe handler is broken: `incoming_name` is never "
       "recorded to the `activity_log` and `pushover_notify` receives the wrong name, so notifications fail.\n\n"
       "## Required change\nx\n")
INTENT = "Add a local variable incoming_name before the log line"
check("B honest 'behavior to add' section is clean",
      aids.defect_section_findings(GOOD, target_text=TARGET, intent=INTENT) == [])
f = aids.defect_section_findings(BAD, target_text=TARGET, intent=INTENT)
check("B invented consumers (activity_log, pushover_notify) are flagged",
      any("activity_log" in x and "pushover_notify" in x for x in f), f)
check("B a defect claim on an additive intent is flagged",
      any("additive" in x for x in f), f)
check("B an unfilled TODO section is not linted", aids.defect_section_findings(
    "# T\n## Confirmed defect\n\nTODO -- x\n", target_text=TARGET, intent=INTENT) == [])

# ---------------------------------------------------------------- C. self-check report
rep = aids.selfcheck_failure_report(
    "=== target parses ===\n  ok: m.py parses\n=== behavioural cases ===\nTraceback (most recent call last):\n"
    '  File "/w/test_fixture.py", line 42, in <module>\n    x\nNameError: name \'x\' is not defined\n'
    "  FAIL: adversarial cases failed\n", fixture_names=["test_fixture.py"], target="m.py")
check("C fixture NameError -> kind fixture-error with stage, file:line and error",
      rep["kind"] == "fixture-error" and rep["stage"] == "behavioural cases" and rep["file"] == "test_fixture.py"
      and rep["line"] == 42 and "NameError" in rep["text"] and "not refimpl.py" in rep["text"], rep)
rep = aids.selfcheck_failure_report(
    "=== behavioural cases ===\n  FAIL Radarr: x -- got [1], want [2]\n  3/4 case(s) passed\n  FAIL: adversarial cases failed\n",
    fixture_names=["test_fixture.py"], target="m.py")
check("C failing case -> case-failed and the first failing assertion is quoted",
      rep["kind"] == "case-failed" and "FAIL Radarr: x -- got [1], want [2]" in rep["text"], rep)
rep = aids.selfcheck_failure_report(
    "=== behavioural cases ===\nTraceback (most recent call last):\n"
    '  File "/w/test_fixture.py", line 9, in <lambda>\n    target.f()\n'
    '  File "/w/m.py", line 3, in f\n    return undefined_name\nNameError: name \'undefined_name\' is not defined\n',
    fixture_names=["test_fixture.py"], target="m.py")
check("C exception inside the target called from the fixture -> target-error naming both locations",
      rep["kind"] == "target-error" and rep["file"] == "m.py" and rep.get("called_from") == "test_fixture.py:9", rep)
rep = aids.selfcheck_failure_report("=== spec literals ===\n  FAIL: a Must-contain literal is missing\n", fixture_names=["test_fixture.py"])
check("C missing literal -> kind literals", rep["kind"] == "literals", rep)
check("C nothing recognisable -> empty text (caller keeps its old message)",
      aids.selfcheck_failure_report("")["text"] == "")

# ---------------------------------------------------------------- D. runbook
sk = SKILL.read_text() if SKILL.is_file() else ""
check("D runbook: hand-repair section documents --auto-seal and --seal-baseline",
      "Hand-repairing a harness" in sk and "--auto-seal" in sk and "--seal-baseline" in sk, SKILL)

# ---------------------------------------------------------------- E. edit-surface lint
sl = load_script("ollama-dispatch-slice", "sl_aids")
root = Path(tempfile.mkdtemp(prefix="aids-sl-"))
(root / "big.py").write_text("x = 1\n" * 2500)
(root / "small.py").write_text("x = 1\n" * 50)
S0 = {"status": "pending", "title": "t", "intent": "define a local before the log line", "depends_on": [],
      "must_contain": ["incoming_name"], "verify_shape": "v"}
st = {"label": "L", "repo": str(root), "target": "big.py", "lang": "python", "chain_branch": "slice/L",
      "chain_worktree": str(root), "order": ["s1"], "slices": {"s1": dict(S0)}}
w = sl.edit_surface_warnings(st, st["slices"]["s1"], str(root))
check("E big target + no excerpt hint -> one warning naming the file, size and the recommendation",
      len(w) == 1 and "big.py" in w[0] and "2500" in w[0] and "excerpt" in w[0] and "EXTRACTS" in w[0], w)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sl.dry_run(st)
check("E the warning is surfaced in the plan output (dry_run)", "WARNING (spec): edit-surface" in buf.getvalue(), buf.getvalue()[-400:])
check("E it is a WARNING, not a block: no lint reason, slice status untouched",
      sl.pre_author_lint_reason(st, dict(S0, id="s1")) in (None, "") or "edit-surface" not in str(
          sl.pre_author_lint_reason(st, dict(S0, id="s1"))))
st_s = dict(st, target="small.py")
check("E small target -> no warning", sl.edit_surface_warnings(st_s, st_s["slices"]["s1"], str(root)) == [])
with env(DISPATCH_EDIT_SURFACE_LINES="3000"):
    check("E threshold is env-tunable (3000 > 2500 lines)", sl.edit_surface_warnings(st, st["slices"]["s1"], str(root)) == [])
with env(DISPATCH_EDIT_SURFACE_LINES="100"):
    check("E threshold is env-tunable (100 < 2500, small.py 50 still quiet)",
          len(sl.edit_surface_warnings(st, st["slices"]["s1"], str(root))) == 1
          and sl.edit_surface_warnings(st_s, st_s["slices"]["s1"], str(root)) == [])
with env(DISPATCH_EDIT_SURFACE_WARN="0"):
    check("E DISPATCH_EDIT_SURFACE_WARN=0 disables", sl.edit_surface_warnings(st, st["slices"]["s1"], str(root)) == [])
for label, patch in (("excerpt key", {"excerpt": "def handler + globals log, _recent"}),
                     ("surface_ack", {"surface_ack": True}),
                     ("intent says extract a pure helper", {"intent": "Extract a pure function pick_name into its own module"}),
                     ("intent mentions an excerpt", {"intent": "Using the excerpt of handler, define a local"})):
    sx = dict(S0, **patch)
    check(f"E silenced by: {label}", sl.edit_surface_warnings(st, sx, str(root)) == [])
check("E plan fields excerpt / surface_ack are carried into the run state (SPEC_FIELDS)",
      "excerpt" in sl.SPEC_FIELDS and "surface_ack" in sl.SPEC_FIELDS)

print(f"\n--- {len(FAILS)} failed ---")
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("AUTHORING_AIDS_OK")
