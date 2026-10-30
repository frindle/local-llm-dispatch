#!/usr/bin/env python3
"""Guards the bug fixed 2026-09-19 (bg-captcha-s3-vision, esim-global-s1-parse-global).

Two defects, both in ollama-dispatch-auto:

1. UNWINNABLE VERIFY. Every author/refine job is enqueued with
   `--verify python3 auto-harness-check.py`, but that script was written exactly
   once (run_auto step 1). ollama-dispatch-slice.clean_and_seal() unlinks it to
   get a clean launch baseline, so a later refine enqueue into the same worktree
   shipped a job whose acceptance test could only ENOENT. The worker then answers
   every task_complete with "NOT ACCEPTED ... keep working" regardless of the
   model's work; the round is logged as DID-NOT-CONVERGE and misread as model
   incapacity. dispatch_model() must regenerate the script before enqueueing.

2. FALSE-PREMISE REFINE PROMPT. refine_prompt() emitted "For EACH survivor below"
   unconditionally while the survivor LIST was `if survivors`. A round triggered
   by a non-relevance blocker (relevance UNPROVEN => zero survivors) told the
   model to work through a list that was not there.

Run: python3 test-ollama-dispatch-auto-verify-script.py
"""
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

AUTO = Path(__file__).resolve().parent / "ollama-dispatch-auto"
spec = importlib.util.spec_from_loader(
    "oda", importlib.machinery.SourceFileLoader("oda", str(AUTO)))
oda = importlib.util.module_from_spec(spec)
sys.modules["oda"] = oda
spec.loader.exec_module(oda)

fails = []


def check(name, cond, detail=""):
    print(("  ok: " if cond else "  FAIL: ") + name + ("" if cond else " -- " + detail))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- defect 1
print("=== defect 1: enqueue regenerates a missing auto-harness-check.py ===")
with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)

    # Reproduce the clean_and_seal() end state: harness present, self-check GONE.
    oda.write_harness_check(wt, "python")
    check("write_harness_check seeds the script", (wt / "auto-harness-check.py").is_file())
    (wt / "auto-harness-check.py").unlink()

    calls = []

    def fake_capture(cmd, **kw):
        calls.append(cmd)
        return 0, "queued 0123456789ab\n", ""

    real_capture = oda.capture
    oda.capture = fake_capture
    try:
        a = SimpleNamespace(drafter_cmd=None, model="m", host="h", num_ctx=1024,
                            author_max_iters=5, max_tokens=None, lang="python",
                            timeout=1)
        # job_row is polled after enqueue; short-circuit to a terminal state.
        oda.job_row = lambda j: {"status": "done"}
        ok, why = oda.dispatch_model(wt, "PROMPT", "auto-refine-x-r1",
                                     "python3 auto-harness-check.py", a)
    finally:
        oda.capture = real_capture

    check("the missing verify script is regenerated before enqueue",
          (wt / "auto-harness-check.py").is_file(),
          "dispatch_model enqueued a job whose verify command cannot run")
    _hc = wt / "auto-harness-check.py"
    check("the regenerated script is the real self-check",
          _hc.is_file() and "harness DISCRIMINATES" in _hc.read_text())
    check("the job was still enqueued", ok, why)
    check("the enqueue carries the self-check verify",
          any("python3 auto-harness-check.py" in c for c in (calls[0] if calls else [])),
          repr(calls[:1]))

    # REVERT-TEST (run by hand, and recorded here): strip the regeneration block
    # from dispatch_model() and the "regenerated before enqueue" check above FAILS,
    # while "the job was still enqueued" still passes -- i.e. pre-fix, an unwinnable
    # job is launched silently. That is exactly the production failure.

# ---------------------------------------------------------------- defect 2
print("=== defect 2: refine_prompt does not invent survivors ===")
blockers = [{"check": "cwd-exclusive", "msg": "give this dispatch its OWN worktree"}]

empty = oda.refine_prompt("t.py", [], blockers, "python")
check("no-survivor prompt does not say 'For EACH survivor below'",
      "For EACH survivor below" not in empty, empty[:300])
check("no-survivor prompt says explicitly that none were reported",
      "did NOT report any surviving mutations" in empty, empty[:300])
check("no-survivor prompt forbids a DIY mutation sweep",
      "do not run" in empty.lower() and "mutation sweep" in empty.lower(), empty[:400])
check("no-survivor prompt still carries the blockers",
      "cwd-exclusive" in empty, empty[:400])

survs = [{"file": "t.py", "line": 7, "mutation": "0.5->0.6", "snippet": "x = 0.5"}]
withs = oda.refine_prompt("t.py", survs, blockers, "python")
check("survivor prompt keeps the original 'For EACH survivor below' framing",
      "For EACH survivor below" in withs)
check("survivor prompt lists the survivor", "0.5->0.6" in withs)
check("survivor prompt does NOT claim there were none",
      "did NOT report any surviving mutations" not in withs)

# ---------------------------------------------------------------- defect 3
# In a SEALED tree (ollama-dispatch-slice.clean_and_seal commits the harness), a
# refine round's edits land on TRACKED files. The self-check's revert() did a
# whole-tree `git checkout -- .`, so it silently ATE the model's fixture edits and
# still exited 0; and step 1 measured "baseline" against the model's already-fixed
# target, failing with the unactionable "verify.sh PASSES at baseline".
print("=== defect 3: the self-check is non-destructive on a sealed refine tree ===")
with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    (wt / "pkg").mkdir()
    (wt / "pkg" / "mod.py").write_text("def f(x):\n    return None\n")
    (wt / "TASK.md").write_text(
        "# TASK\nOnly edit `pkg/mod.py`\n## Must contain\n- in pkg/mod.py: `return x * 2`\n")
    (wt / "refimpl.py").write_text(
        'import pathlib\npathlib.Path("pkg/mod.py").write_text('
        '"def f(x):\\n    return x * 2\\n")\n')
    (wt / "test_fixture.py").write_text(
        "import pkg.mod as m\nassert m.f(2) == 4\nprint('cases ok')\n")
    (wt / "verify.sh").write_text(
        'cd "$(dirname "$0")" || exit 1\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n')
    (wt / "check_literals.py").write_text("LITERALS = []\n")
    (wt / ".dispatch-harness.json").write_text('{"target": "pkg/mod.py"}')
    for c in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "seal dispatch harness (clean launch baseline)"]):
        subprocess.run(c, cwd=wt, check=True)

    oda.write_harness_check(wt, "python")
    # The sealed-refine state: the model edited the TRACKED fixture AND the target.
    with (wt / "test_fixture.py").open("a") as fh:
        fh.write("# MODEL_CASE\n")
    (wt / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n")

    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                       capture_output=True, text=True)
    check("the self-check passes on a sealed refine tree",
          r.returncode == 0 and "VERIFY_OK" in r.stdout,
          (r.stdout + r.stderr).strip()[-300:])
    check("it does NOT eat the model's edit to the tracked fixture",
          "MODEL_CASE" in (wt / "test_fixture.py").read_text(),
          "revert()'s whole-tree `git checkout -- .` wiped the model's work")
    check("it does NOT eat the model's edit to the tracked target",
          "x * 2" in (wt / "pkg" / "mod.py").read_text())

    # And it must still REJECT a fixture that genuinely cannot fail.
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=wt, check=True)
    (wt / "test_fixture.py").write_text("print('cases ok')\n")  # asserts nothing
    r2 = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                        capture_output=True, text=True)
    check("it still rejects a non-discriminating fixture",
          r2.returncode != 0 and "PASSES at baseline" in r2.stdout,
          (r2.stdout + r2.stderr).strip()[-300:])

# ---------------------------------------------------------------- defect 4
# The refine prompt hardcoded the `# relevance: unobservable` opt-out, so a JS/TS
# author was told to write a Python comment into a .js file; and it never hinted
# that an unkillable survivor usually means a too-permissive fake (2026-09-26,
# sidecar-bfmr-login-fetch s1: a fake querySelectorAll returning a plain Array).
print("=== defect 4: refine opt-out follows the target language; mock fidelity ===")
js_survs = [{"file": "sidecar/src/bfmr.js", "line": 12, "mutation": "'' -> '_X'",
             "snippet": "const t = el.textContent || ''"}]
jsp = oda.refine_prompt("sidecar/src/bfmr.js", js_survs, [], "ts")
check("JS target gets the // opt-out", "`// relevance: unobservable`" in jsp, jsp[-600:])
check("JS target is NOT told to write a # comment",
      "`# relevance: unobservable`" not in jsp, jsp[-600:])
pyp = oda.refine_prompt("t.py", survs, [], "python")
check("Python target keeps the # opt-out", "`# relevance: unobservable`" in pyp)
check("extension beats --lang (python lang, .ts target -> //)",
      oda.relevance_optout_comment("src/x.ts", "python") == "// relevance: unobservable")
check("extensionless target falls back to lang",
      oda.relevance_optout_comment("bin/tool", "js") == "// relevance: unobservable"
      and oda.relevance_optout_comment("bin/tool", "python") == "# relevance: unobservable")
check("survivor prompt carries the mock-fidelity hint",
      "NodeList" in jsp and "more permissive" in jsp, jsp[-900:])
check("no-survivor prompt does not carry the mock-fidelity hint",
      "NodeList" not in empty)
ap_js = SimpleNamespace(intent="parse the login page", interface="", lang="ts")
ap_py = SimpleNamespace(intent="parse the login page", interface="", lang="python")
check("JS/TS authoring prompt warns about faithful DOM/fetch fakes",
      "NodeList" in oda.author_prompt(ap_js, "src/x.ts"))
check("Python authoring prompt is unchanged by the JS fake rule",
      "NodeList" not in oda.author_prompt(ap_py, "app/x.py"))

# ---------------------------------------------------------------- defect 5
# bfmr s2 (2026-09-26): 3 overnight authoring jobs, 24/24 iterations each, died on
# "0 !== 2" -- the JS fixture restored its fake window/XMLHttpRequest globals in a
# finally before the handlers ran, so the model's OWN refimpl failed its OWN tests
# and nothing told it to look at the fake. node:test prints "# fail N", which the
# python-shaped "N/M case(s) passed" hint never matched.
print("=== defect 5: refimpl-fails-own-tests surfaces the fake-environment hint ===")
with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    for c in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=wt, check=True)
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `target.txt`\n")
    # node:test-shaped failure output, both at baseline and after the refimpl
    (wt / "verify.sh").write_text(
        'echo "not ok 1 - captures"\necho "  AssertionError: 0 !== 2"\n'
        'echo "# pass 1"\necho "# fail 1"\nexit 1\n')
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "target.txt").write_text("stub\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    for c in (["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
        subprocess.run(c, cwd=wt, check=True)
    (wt / "verify.test.ts").write_text("// fixture\nimport { installFakeBrowser } from './dispatch-env.ts'; installFakeBrowser({});\n")
    oda.write_harness_check(wt, "ts")
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    check("ts self-check names the refimpl-fails-own-tests signal",
          "YOUR OWN refimpl.py fails YOUR OWN tests" in out, out[-500:])
    check("...and points at restored globals / shared fakes",
          "finally" in out and "FRESH fake" in out, out[-500:])
    oda.write_harness_check(wt, "python")
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                       capture_output=True, text=True)
    check("a python fixture does not get the JS fake hint",
          "YOUR OWN refimpl.py" not in (r.stdout + r.stderr))

_lc = ("  FAIL: the reference impl (refimpl.py) does not make verify.sh print "
       "VERIFY_OK -- ...\n# fail 1\n")
ap_ts = SimpleNamespace(intent="x", interface="", lang="ts")
check("continuation prompt surfaces the signal when the refimpl fails its own tests",
      "FAILS your own tests" in oda.author_continue_prompt(ap_ts, "src/x.js", _lc))
check("...but not on a SyntaxError (that hint owns the case)",
      "FAILS your own tests" not in oda.author_continue_prompt(
          ap_ts, "src/x.js", _lc + "SyntaxError: bad\n"))
check("...nor when the check failed for another reason",
      "FAILS your own tests" not in oda.author_continue_prompt(
          ap_ts, "src/x.js", "FAIL: TASK.md still has TODO placeholders"))
_rb = [{"check": "refimpl-passes", "msg": "verify.sh did not pass after refimpl"}]
check("refine prompt surfaces the signal on a refimpl-passes blocker",
      "FAILS your own tests" in oda.refine_prompt("src/x.js", [], _rb, "ts"))
check("refine prompt stays quiet without that blocker",
      "FAILS your own tests" not in oda.refine_prompt("src/x.js", [], blockers, "ts"))
check("authoring prompt carries the lazy-globals + fresh-fake rules for JS/TS",
      all(k in oda.author_prompt(ap_js, "src/x.ts")
          for k in ("LAST", "FRESH fake class")))

print()
if fails:
    print("AUTO_VERIFY_SCRIPT_FAIL: " + ", ".join(fails))
    sys.exit(1)
print("AUTO_VERIFY_SCRIPT_OK: enqueue self-heals the verify script; "
      "refine_prompt is truthful about survivors")
