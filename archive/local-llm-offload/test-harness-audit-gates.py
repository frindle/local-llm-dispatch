#!/usr/bin/env python3
"""harness_audit_gates (wave 3 spec/harness defect gates E, C, A, D, B) + their preflight wiring.
Run: python3 test-harness-audit-gates.py [--gm PATH] [--pre PATH]   (prints ALL PASS)"""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GM = Path(sys.argv[sys.argv.index("--gm") + 1]) if "--gm" in sys.argv else HERE / "harness_audit_gates.py"
PRE = Path(sys.argv[sys.argv.index("--pre") + 1]) if "--pre" in sys.argv else HERE / "ollama-dispatch-preflight"
sp = importlib.util.spec_from_file_location("hag", GM)
g = importlib.util.module_from_spec(sp); sp.loader.exec_module(g)
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


codes = lambda fs: sorted({f["code"] for f in fs})

# ---------------- C: determinism + flake lint ----------------
same = [(1, "not ok 1 - a\nFAIL x"), (1, "not ok 1 - a\nFAIL x"), (1, "not ok 1 - a\nFAIL x")]
check("C: three identical red runs -> no finding", g.determinism_findings(same), [])
flaky = [(1, "not ok 1 - a"), (0, "VERIFY_OK"), (1, "not ok 1 - a")]
check("C: red/green/red -> NONDETERMINISTIC_VERIFY", codes(g.determinism_findings(flaky)), ["NONDETERMINISTIC_VERIFY"])
diff_set = [(1, "not ok 1 - a"), (1, "not ok 2 - b")]
check("C: same rc but a different failing case -> finding", codes(g.determinism_findings(diff_set)), ["NONDETERMINISTIC_VERIFY"])
check("C: Math.random in a fixture is a flake source", codes(g.flake_lint("it('x', () => { if (Math.random() > .5) {} })")), ["FLAKE_SOURCE"])
check("C: time.time()/os.listdir/set iteration flagged",
      len(g.flake_lint("t = time.time()\nfor f in os.listdir('.'):\n    pass\nfor x in set(a):\n    pass\n")), 3)
check("C: flake-ok silences a line", g.flake_lint("x = Date.now()  // flake-ok"), [])
check("C: comments are not flagged", g.flake_lint("# time.time() is banned here"), [])
check("C: a clean fixture has no findings", g.flake_lint("assert f(2) == 4\n"), [])

# ---------------- A: traceability ----------------
task_md = "## Required change\nAdd `double(x)` which returns twice x. Raise ValueError('negative') below 0.\n"
base = "def other():\n    return 'hello world'\n"
fx_ok = "from app.x import double\n\ndef test_a():\n    assert double(2) == 4\n    try:\n        double(-1)\n    except ValueError as e:\n        assert 'negative' in str(e)\n"
check("A: names/strings from TASK.md trace", g.untraceable_assertions(fx_ok, task_md, base, "app/x.py", "python"), [])
fx_bad = "from app.x import double, triple_it\n\ndef test_a():\n    assert double(2) == 4\n    assert str(double(3)) == 'six!!'\n    assert double(1234) == 2468\n"
r = g.untraceable_assertions(fx_bad, task_md, base, "app/x.py", "python")
toks = sorted(f["token"] for f in r)
check("A: an invented export, string constant and number are all flagged", toks, ["1234", "2468", "six!!", "triple_it"])
check("A: a string traced to the BASE file is fine",
      g.untraceable_assertions("from app.x import other\nassert other() == 'hello world'\n", task_md, base, "app/x.py", "python"), [])
check("A: spec-ok on the line exempts it",
      g.untraceable_assertions("from app.x import double\nassert double(2) == 'zzz'  # spec-ok\n", task_md, base, "app/x.py", "python"), [])
js = "import { double, mystery } from './lib/x.ts';\nassert.equal(double(2), 4);\nassert.equal(msg, 'invented text');\n"
r = g.untraceable_assertions(js, task_md, base, "lib/x.ts", "ts")
check("A (ts): invented import + string flagged, spec-named import not", sorted(f["token"] for f in r), ["invented text", "mystery"])
check("A: unparseable python fixture -> no crash, no finding", g.untraceable_assertions("def (:", task_md, base, "a.py", "python"), [])

# ---------------- B: constants ----------------
check("B: prose 1 space vs fixture 2 spaces conflict",
      codes(g.constant_conflicts("Indent with 1 space.", "assert out == ' ' * 2  # 2 spaces\n")), ["CONSTANT_CONFLICT"])
check("B: agreeing constants -> none", g.constant_conflicts("Retry 3 times.", "# 3 times\n"), [])
check("B: different units never conflict", g.constant_conflicts("Wait 5 seconds.", "# 200 ms\n"), [])

# ---------------- D: trivial variants ----------------
py_base = "def double(x):\n    \"\"\"d\"\"\"\n    return x * 2\n\ndef other():\n    return 1\n"
vs = dict(g.trivial_variants(py_base, "app/x.py", ["def double("], "python", False))
check("D: touch + literals-only + return-none for py", sorted(vs), ["literals-only", "return-none", "touch-only"])
check("D: return-none keeps the module parseable and only touches the named function",
      "def double(x):" in vs["return-none"] and "return None" in vs["return-none"] and "return 1" in vs["return-none"], True)
check("D: literals-only contains the literal as dead data", "def double(" in vs["literals-only"], True)
cv = dict(g.trivial_variants("", "app/new.py", ["def double("], "python", True))
check("D: creation task adds noop-exports (empty-stub identical to an empty base is dropped)", "noop-exports" in cv and "empty-stub" not in cv, True)
check("D: creation over a non-empty base adds empty-stub", "empty-stub" in dict(g.trivial_variants("x = 1\n", "app/new.py", ["def double("], "python", True)), True)
tsb = "export function double(x: number): number {\n  return x * 2;\n}\n"
tv = dict(g.trivial_variants(tsb, "lib/x.ts", ["export function double("], "ts", False))
check("D (ts): noop-bodies replaces the named function body", "return undefined as any;" in tv.get("noop-bodies", "") and "x * 2" not in tv.get("noop-bodies", ""), True)

tmp = Path(tempfile.mkdtemp(prefix="hag-"))
(tmp / "app").mkdir()
(tmp / "app/x.py").write_text(py_base)
seen = []
def verify_lit():   # a grep-only verify: green as soon as the literal text is in the file
    seen.append(1)
    return (0, "VERIFY_OK") if "def double(" in (tmp / "app/x.py").read_text() else (1, "")
fs = g.run_trivial_battery(tmp, "app/x.py", g.trivial_variants(py_base, "app/x.py", ["def double("], "python", False), verify_lit)
check("D: a grep-only verify accepts a trivial variant", "TRIVIAL_SOLUTION_PASSES" in codes(fs), True)
check("D: ...and the target is restored byte-for-byte", (tmp / "app/x.py").read_text(), py_base)
red_base = "def double(x):\n    return x\n\ndef other():\n    return 1\n"
(tmp / "app/x.py").write_text(red_base)
def verify_beh():
    import importlib
    ns = {}
    exec((tmp / "app/x.py").read_text(), ns)
    try:
        return (0, "VERIFY_OK") if ns["double"](4) == 8 else (1, "")
    except Exception:
        return (1, "")
check("D: a behavioural verify rejects every degenerate variant",
      g.run_trivial_battery(tmp, "app/x.py", g.trivial_variants(red_base, "app/x.py", ["def double("], "python", False), verify_beh), [])
(tmp / "app/new.py").unlink() if (tmp / "app/new.py").exists() else None
g.run_trivial_battery(tmp, "app/new.py", [("empty-stub", "")], lambda: (1, ""))
check("D: a creation target that did not exist is deleted again", (tmp / "app/new.py").exists(), False)

# ---------------- E: freshness / revalidate ----------------
repo = Path(tempfile.mkdtemp(prefix="hag-e-"))
git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
(repo / "t.py").write_text("def f():\n    return 0\n")
(repo / "verify.sh").write_text("#!/bin/bash\npython3 -B -c 'import t; assert t.f()==1' && echo VERIFY_OK\n")
(repo / "TASK.md").write_text("# T\n## Required change\nmake f return 1\n## Must contain\n- `return 1`\nOnly edit `t.py`\n")
(repo / "refimpl.py").write_text("print('x')\n")
(repo / ".dispatch-harness.json").write_text(json.dumps({"target": "t.py", "creation_task": False}))
git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
snap = g.freshness_snapshot(repo, "t.py")
(repo / ".preflight-state.json").write_text(json.dumps({"freshness": snap}))
check("E: unchanged tree is fresh and runs NO verify", g.revalidate(repo, run=lambda c: (_ for _ in ()).throw(AssertionError("verify ran")))["status"], "fresh")
(repo / "TASK.md").write_text("# T\n## Required change\nmake f return 2\n## Must contain\n- `return 2`\nOnly edit `t.py`\n")
r = g.revalidate(repo)
check("E: a changed TASK.md blob is detected as drift", [d["item"] for d in r["drift"]], ["TASK.md"])
check("E: drifted but still red on the untouched tree -> revalidated", r["status"], "revalidated")
(repo / "t.py").write_text("def f():\n    return 1\n")
git(repo, "commit", "-qam", "base moved: spec already satisfied")
r = g.revalidate(repo)
check("E: base moved and the verify is green on the untouched tree -> NOP_GREEN (already satisfied)", r["status"], "NOP_GREEN")
check("E: ...drift names HEAD and the base blob", {"HEAD", "base blob of t.py"} <= {d["item"] for d in r["drift"]}, True)
# creation task over a base that now holds a real implementation -> SPEC_DEFECT
(repo / ".dispatch-harness.json").write_text(json.dumps({"target": "t.py", "creation_task": True}))
(repo / "TASK.md").write_text("# T\n## Required change\nCreate t.py with f returning 1.\n## Must contain\n- `return 1`\nOnly edit `t.py`\n")
(repo / "t.py").write_text("def f():\n    return 1\n\ndef g():\n    return 2\n" * 3)
git(repo, "commit", "-qam", "real impl lands at base")
r = g.revalidate(repo)
check("E: creation task whose target is now a real implementation -> SPEC_DEFECT", r["status"], "SPEC_DEFECT")
env = dict(os.environ)
p = subprocess.run([sys.executable, str(GM), "revalidate", str(repo)], capture_output=True, text=True, env=env)
check("E: the CLI exits 3 on SPEC_DEFECT", p.returncode, 3)
check("E: no snapshot -> no-snapshot (exit 0, caller keeps its old behaviour)",
      g.revalidate(tmp)["status"], "no-snapshot")

# ---------------- preflight wiring (real CLI) ----------------
def run_pre(wt, *extra):
    env = dict(os.environ, HOME=str(tmp / "home"), OLLAMA_DISPATCH_HOME=str(tmp / "home/.ollama-dispatch"))
    p = subprocess.run([sys.executable, str(PRE), str(wt), "--json", "--no-relevance", *extra],
                       capture_output=True, text=True, env=env, timeout=600)
    return json.loads(p.stdout)

(tmp / "home").mkdir(exist_ok=True)
r0 = Path(tempfile.mkdtemp(prefix="hag-p-"))
git(r0, "init", "-q"); git(r0, "config", "user.email", "t@t"); git(r0, "config", "user.name", "t")
(r0 / "t.py").write_text("def f():\n    return 0\n")
git(r0, "add", "-A"); git(r0, "commit", "-qm", "init")
wt = tmp / "wt-p"
git(r0, "worktree", "add", "-q", "-b", "dispatch/p", str(wt))
(wt / "TASK.md").write_text("# TASK: t\n\n## Confirmed defect\n\nobs.\n\n## Entry point\n\nt.py:1\n\n"
                            "## Required change\n\nMake f() return 1.\n\n## Must contain\n\n- `return 1`\n\n## Scope\n\n"
                            "Only edit `t.py`; do not edit `verify.sh` or `TASK.md`.\n\nRun `bash verify.sh` after every edit until it prints VERIFY_OK.\n")
# a GREP-ONLY verify: certifies text, not behaviour
(wt / "verify.sh").write_text("#!/bin/bash\ngrep -q 'return 1' t.py && echo VERIFY_OK || exit 1\n")
(wt / "check_literals.py").write_text("print('ok')\n")
(wt / "refimpl.py").write_text("import re\np='t.py'\ns=open(p).read()\nopen(p,'w').write(s.replace('return 0','return 1'))\n")
(wt / ".dispatch-harness.json").write_text(json.dumps({"authored": ["TASK.md", "verify.sh", "check_literals.py", "refimpl.py"],
                                                       "target": "t.py", "creation_task": False}))
d = run_pre(wt, "--refimpl-cmd", "python3 refimpl.py")
ch = {c["check"]: c for c in d["checks"]}
check("wiring: a grep-only verify draws a trivial-solution WARN", ch.get("trivial-solution", {}).get("status"), "WARN")
check("wiring: ...as a WARN only (never changes the verdict by itself)", all(c["status"] != "FAIL" or c["check"] != "trivial-solution" for c in d["checks"]), True)
check("wiring: the tree is left at baseline", (wt / "t.py").read_text(), "def f():\n    return 0\n")
d2 = run_pre(wt, "--refimpl-cmd", "python3 refimpl.py", "--no-audit")
check("wiring: --no-audit emits none of the audit rows",
      [c["check"] for c in d2["checks"] if c["check"] in ("trivial-solution", "determinism", "spec-traceability", "flake-lint", "prose-constants")], [])
check("wiring: --json carries the task_class", (d.get("task_class") or {}).get("class") in ("modify", "create", "port", "ui_dom", "cross_file"), True)
if d["verdict"] == "GO":
    st = json.loads((wt / ".preflight-state.json").read_text())
    check("wiring: a GO verdict stores the freshness snapshot", bool((st.get("freshness") or {}).get("head")), True)

# an unreadable fixture must not crash the audit nor silence the draft-confirmed review gate
fxp = wt / "test_fixture.py"
fxp.write_text("DRAFT_UNCONFIRMED = True\nassert True\n")
fxp.chmod(0o000)
try:
    d3 = run_pre(wt, "--refimpl-cmd", "python3 refimpl.py")
finally:
    fxp.chmod(0o644)
check("wiring: an unreadable fixture neither crashes the gate nor changes its verdict", d3.get("verdict"), d.get("verdict"))
fxp.unlink()

print()
print(f"FAILED {FAILS}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
