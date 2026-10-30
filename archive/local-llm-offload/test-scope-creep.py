#!/usr/bin/env python3
"""scope_creep guard (incident plex-automation 4a454d6 / job 463676923c97, 2026-10-12): a verify-green `qagent land` pushed a
121-line change that added a 30s scheduler thread + env var to a 7000-line file; land ignored the gate record (concerns,
signoff pending).  Proves: the exact diff shape is flagged; legit refactors/helpers/creation tasks/test files pass; shadow
is log-only; ENFORCE refuses at qagent land; DISABLE restores exact prior behaviour (land_guard byte-identical on every
recorded qagent fix meta; gate-on-complete hook is a pure additive block).   --bin DIR to test a sandbox copy."""
import ast, difflib, importlib.machinery, importlib.util, json, os, pwd, sys, tempfile
from pathlib import Path

HERE = Path(sys.argv[sys.argv.index("--bin") + 1]) if "--bin" in sys.argv else Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="scopecreep-"))
os.environ["SCOPE_CREEP_LOG"] = str(TMP / "sc.jsonl")
for k in ("SCOPE_CREEP_DISABLE", "SCOPE_CREEP_ENFORCE", "SCOPE_CREEP_SHADOW", "SCOPE_CREEP_MAX_UNNAMED_LINES", "SCOPE_CREEP_SURFACE_LINES"):
    os.environ.pop(k, None)
import scope_creep as sc
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok: FAILS.append(name)


def mkdiff(path, old, new, newfile=False):
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if newfile else "") + d


BASE = "import os, requests\n\ndef fetch(x):\n    return requests.get(x).text\n\ndef main():\n    print(fetch('u'))\n\nif __name__ == '__main__':\n    main()\n"
BIG = "".join(f"    s{i} = {i}\n" for i in range(110))
SCHED = ("import threading, time\nSLOTS = int(os.environ.get('UNLIMITED_SLOTS', '2'))\n\n"
         "def keep_slots():\n" + BIG + "\ndef slot_scheduler():\n    while True:\n        keep_slots()\n        time.sleep(30)\n\n")
CHANGED_FETCH = BASE.replace("return requests.get(x).text", "return requests.get(x, timeout=5).text")
INCIDENT_NEW = CHANGED_FETCH.replace("def main():", SCHED + "def main():").replace(
    "    main()", "    threading.Thread(target=slot_scheduler, daemon=True).start()\n    main()")

# 1. the task asks to change ONE function; the model also adds an unrelated scheduler thread + env var + big function
SPEC1 = "Make fetch() pass timeout=5 to requests.get. Only edit app.py."
r = sc.evaluate(mkdiff("app.py", BASE, INCIDENT_NEW), SPEC1, None)
chk("1 unrelated scheduler thread + env var + 110-line fn flagged (spec abstains on signoff)", r["flag"], True)
chk("1 reasons", set(r["reasons"]) >= {"UNREQUESTED_SURFACE", "UNREQUESTED_BLOCK"}, True)
# 2. exact incident shape: spec NAMES it all, change is big, unsigned gate -> UNSIGNED_SURFACE only
SPEC2 = SPEC1 + " Add keep_slots(), slot_scheduler(), UNLIMITED_SLOTS and a thread started in main."
r = sc.evaluate(mkdiff("app.py", BASE, INCIDENT_NEW), SPEC2, True)
chk("2 spec-named but unsigned + large -> UNSIGNED_SURFACE", r["reasons"], ["UNSIGNED_SURFACE"])
chk("2b same diff, clean signed gate -> pass", sc.evaluate(mkdiff("app.py", BASE, INCIDENT_NEW), SPEC2, False)["flag"], False)
chk("2c same diff, gate unknown -> pass (no guess)", sc.evaluate(mkdiff("app.py", BASE, INCIDENT_NEW), SPEC2, None)["flag"], False)
small = BASE.replace("def main():", "def tick():\n    pass\n\ndef main():").replace("    main()", "    threading.Thread(target=tick, daemon=True).start()\n    main()")
chk("2d small (<80 lines) named thread, unsigned -> pass", sc.evaluate(mkdiff("app.py", BASE, small), "add tick thread", True)["flag"], False)
# 3. legit things that must still pass
chk("3a one-function edit", sc.evaluate(mkdiff("app.py", BASE, CHANGED_FETCH), SPEC1, True)["flag"], False)
helper = BASE.replace("def main():", "def _norm(u):\n" + "    u = u.strip()\n" * 12 + "    return u\n\ndef main():")
chk("3b small unnamed helper (refactor) passes", sc.evaluate(mkdiff("app.py", BASE, helper), SPEC1, True)["flag"], False)
bighelper = BASE.replace("def main():", "def parse_rows(t):\n" + BIG + "    return 1\n\ndef main():")
chk("3c big helper NAMED by the task passes", sc.evaluate(mkdiff("app.py", BASE, bighelper), "add parse_rows() to parse rows", True)["flag"], False)
chk("3d big helper NOT named flagged", sc.evaluate(mkdiff("app.py", BASE, bighelper), SPEC1, None)["reasons"], ["UNREQUESTED_BLOCK"])
moved_old = BASE + "\ndef tick():\n" + BIG + "\nt = threading.Thread(target=tick)\nE = os.environ.get('X')\n"
moved_new = "import os\n" + moved_old.replace(BASE, BASE.replace("def main", "def main"))
moved_new = moved_new.replace("import threading", "import threading")
refactor_new = "# reordered\n" + moved_old  # identical defs re-added (invariant-guard refactor / re-added literal): removed+added same names
chk("3e refactor that only reorders (same def/thread/env on removed side) passes",
    sc.evaluate(mkdiff("app.py", moved_old, refactor_new), SPEC1, True)["flag"], False)
chk("3f new env var NAMED by task passes", sc.evaluate(mkdiff("app.py", BASE, BASE + "T = os.environ.get('FETCH_TIMEOUT')\n"), "read FETCH_TIMEOUT from env", True)["flag"], False)
chk("3g test files are ignored (thread+env in test_app.py)", sc.evaluate(mkdiff("test_app.py", "", SCHED, True) + mkdiff("tests/x.js", "", "setInterval(f, 5)\nprocess.env.Q\n"), SPEC1, True)["flag"], False)
chk("3h creation task: brand-new file with thread+env passes", sc.evaluate(mkdiff("loginCooldown.js", "", "const a = process.env.DATA_DIR\nsetInterval(() => 1, 5)\n" + "x\n" * 150, True), "Create loginCooldown.js", True)["flag"], False)
chk("3i bulk snapshot (>3000 lines) abstains even with a thread+env", sc.evaluate(mkdiff("a.py", "x()\n", "x()\nimport threading\nthreading.Thread(target=zz).start()\nK = os.environ.get('Q')\n" + "x = 1\n" * 3500), "fix x", True)["flag"], False)
chk("3j no spec text: UNREQUESTED rules abstain", sc.evaluate(mkdiff("app.py", BASE, INCIDENT_NEW), "", None)["flag"], False)
chk("3k anonymous setInterval with a task that talks about a heartbeat passes",
    sc.evaluate(mkdiff("lib.js", "a();\n", "a();\nsetInterval(() => beat(), 5000)\n"), "keep the lock heartbeat alive after restart", None)["flag"], False)
chk("3l same anonymous setInterval with unrelated task flagged",
    sc.evaluate(mkdiff("lib.js", "a();\n", "a();\nsetInterval(() => beat(), 5000)\n"), "fix typo in error message", None)["reasons"], ["UNREQUESTED_SURFACE"])
chk("3m Flask route unnamed flagged", sc.evaluate(mkdiff("app.py", BASE, BASE + "@app.route('/admin/purge')\ndef p():\n    pass\n"), SPEC1, None)["reasons"], ["UNREQUESTED_SURFACE"])
chk("3n JS env unnamed flagged", sc.evaluate(mkdiff("a.js", "x()\n", "x()\nconst k = process.env.SECRET_MODE\n"), "fix x", None)["flag"], True)
# 4. flags
chk("4a gate_unsigned: signoff pending", sc.gate_unsigned({"verdict": "pass", "signoff_required": True, "signoff_verdict": None}), True)
chk("4b gate_unsigned: concerns", sc.gate_unsigned({"verdict": "concerns"}), True)
chk("4c gate_unsigned: clean pass", sc.gate_unsigned({"verdict": "pass"}), False)
chk("4d gate_unsigned: no record", sc.gate_unsigned(None), None)
INC = mkdiff("app.py", BASE, INCIDENT_NEW)
GATE_BAD = {"verdict": "concerns", "signoff_required": True, "signoff_verdict": None}
log = Path(os.environ["SCOPE_CREEP_LOG"])
w, r = sc.check(INC, SPEC2, GATE_BAD, {"job": "t"})
chk("4e shadow (default): returns no refusal, logs SCOPE-CREEP-WOULD-FLAG", (w, json.loads(log.read_text().splitlines()[-1])["event"]), (None, "SCOPE-CREEP-WOULD-FLAG"))
os.environ["SCOPE_CREEP_ENFORCE"] = "1"
w, r = sc.check(INC, SPEC2, GATE_BAD, {"job": "t"})
chk("4f enforce: refusal reason + SCOPE-CREEP-BLOCKED", (bool(w) and "SCOPE-CREEP-BLOCKED" in w, json.loads(log.read_text().splitlines()[-1])["event"]), (True, "SCOPE-CREEP-BLOCKED"))
n = len(log.read_text().splitlines())
os.environ["SCOPE_CREEP_DISABLE"] = "1"
w, r = sc.check(INC, SPEC2, GATE_BAD, {"job": "t"})
chk("4g kill switch beats enforce: (None,None), nothing logged", (w, r, len(log.read_text().splitlines())), (None, None, n))
os.environ.pop("SCOPE_CREEP_DISABLE"); os.environ.pop("SCOPE_CREEP_ENFORCE")
os.environ["SCOPE_CREEP_SHADOW"] = "0"
chk("4h shadow off + enforce off: no-op", sc.check(INC, SPEC2, GATE_BAD, {"job": "t"}), (None, None))
os.environ.pop("SCOPE_CREEP_SHADOW")

# 5. qagent land_guard: the REAL incident meta + byte-identical on every recorded fix meta when disabled / shadow
qp = HERE / "qagent"
ld = importlib.machinery.SourceFileLoader("qagent_t", str(qp)); spc = importlib.util.spec_from_loader("qagent_t", ld)
qa = importlib.util.module_from_spec(spc); ld.exec_module(qa)
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
FX = HERE / "test-fixtures-scope-creep"
corpus = []
for p in sorted((REAL_HOME / ".qagent" / "fix").glob("*.json")):
    try:
        m = json.loads(p.read_text())
        if m.get("report") and m["report"].get("files") is not None: corpus.append(m)
    except Exception: pass
# fixture meta = the real incident (diff + task recorded in test-fixtures-scope-creep/): works on any machine / in the canary sandbox
corpus = [m for m in corpus if m["job"] != "463676923c97"]
INCMETA = {"job": "463676923c97", "problem": (FX / "incident-4a454d6.task.txt").read_text(), "repo": "/nonexistent", "base": "x",
           "protected": [], "harness": [], "report": {"patch": str(FX / "incident-4a454d6.diff"), "files": ["M\tarr-webhook.py"]}}
corpus.append(INCMETA)
def guard_old(m):       # prior behaviour = land_guard with the scope-creep tail neutralised
    keep = qa._scope_creep_guard; qa._scope_creep_guard = lambda m: None
    try: return repr(qa.land_guard(m))
    except Exception as e: return "EXC " + type(e).__name__
    finally: qa._scope_creep_guard = keep
def guard_new(m):
    try: return repr(qa.land_guard(m))
    except Exception as e: return "EXC " + type(e).__name__
old = {m["job"]: guard_old(m) for m in corpus}
os.environ["SCOPE_CREEP_DISABLE"] = "1"
chk(f"5a DISABLE: land_guard identical to prior on {len(corpus)} recorded fix metas", {m["job"]: guard_new(m) for m in corpus}, old)
os.environ.pop("SCOPE_CREEP_DISABLE")
chk("5b shadow (enforce off): land_guard identical to prior on every recorded meta", {m["job"]: guard_new(m) for m in corpus}, old)
inc = INCMETA
os.environ["SCOPE_CREEP_ENFORCE"] = "1"
g = guard_new(inc)
chk("5c ENFORCE: the real incident job is REFUSED by land_guard", "SCOPE-CREEP-BLOCKED" in g, True)
os.environ["SCOPE_CREEP_DISABLE"] = "1"
chk("5d ENFORCE+DISABLE: the incident job is not refused (prior behaviour)", guard_new(inc), guard_old(inc))
os.environ.pop("SCOPE_CREEP_DISABLE")
chk("5d2 SHADOW only (default): incident still lands per prior behaviour but WOULD-FLAG is logged",
    (lambda: (os.environ.pop("SCOPE_CREEP_ENFORCE"), guard_new(inc), "SCOPE-CREEP-WOULD-FLAG" in log.read_text())[-1])(), True)
clean = [m for m in corpus if m["job"] != "463676923c97" and m.get("landed") and old[m["job"]] == "None"]
os.environ["SCOPE_CREEP_ENFORCE"] = "1"
bad = [m["job"] for m in clean if guard_new(m) != "None"]
chk(f"5e ENFORCE does not refuse any of {len(clean)} other recorded LANDED fixes (read from the real ~/.qagent)", bad, [])
os.environ.pop("SCOPE_CREEP_ENFORCE")

# 6. gate-on-complete hook is a purely additive block: removing it reproduces the pre-change file byte-for-byte; it never writes payload
gp = HERE / "gate-on-complete.py"; bak = HERE / "gate-on-complete.py.bak-20261013-scopecreep"
src = gp.read_text()
chk("6a hook present and SCOPE_CREEP_DISABLE-guarded", "_sc.check(" in src and 'os.environ.get("SCOPE_CREEP_DISABLE"' in src, True)
i = src.index("        # SCOPE-CREEP SHADOW"); j = src.index("        except Exception:\n            pass\n", i) + len("        except Exception:\n            pass\n")
if bak.exists():
    chk("6b file minus the hook == pre-change backup (byte-identical)", src[:i] + src[j:] == bak.read_text(), True)
blk = ast.parse("def _f():\n" + "\n".join(l[4:] if l.startswith("    ") else l for l in src[i:j].splitlines()) + "\n")
stores = [n for n in ast.walk(blk) if isinstance(n, (ast.Assign, ast.AugAssign, ast.Subscript)) and any(
    isinstance(t, ast.Subscript) and getattr(t.value, "id", "") == "payload" for t in (n.targets if isinstance(n, ast.Assign) else [getattr(n, "target", None)]) if t is not None)]
chk("6c hook never assigns into payload", stores, [])
chk("6d hook sits after the final gate_json.write_text (cannot change the written record)", src.rfind("gate_json.write_text(json.dumps(payload", 0, i) > 0, True)
chk("6e hook is last-resort wrapped (try/except pass) and TEST_MODE-skipped", "not TEST_MODE" in src[i:j] and src[j - 40:j].strip().endswith("pass"), True)
print("SCOPE_CREEP_OK" if not FAILS else f"SCOPE_CREEP_FAIL {FAILS}")
sys.exit(1 if FAILS else 0)
