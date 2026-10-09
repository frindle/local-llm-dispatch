#!/usr/bin/env python3
"""Three auto-harness-check gaps (2026-10-06, rt-bg-commitments-hide-expired
3b1c246e9113 / c7d74b0feebd):

 a) tsx/esbuild `Error: Transform failed` + `file:l:c: ERROR: ...` lines print ABOVE
    ~12 stack frames, so the 1500-char tail dropped them; and top-level await under
    tsx's CJS output got the fake-environment hint. -> carried + TLA hint.
 b) node:test `Cannot mock 'X'. The module is already mocked.` -> mock-once hint.
 c) a long-lived driver re-enqueued with the auto-harness-check.py it wrote at start
    (stale template). -> re-rendered from the ON-DISK template at every enqueue.

Behavioural: real worktrees driven by the REAL HARNESS_CHECK template (verify.sh
replays the exact tsx/node output captured on Node 26 / tsx). --revert-check removes
each block in turn and requires the suite to go RED for every one."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []

FRAMES = "".join("#     at frame%d (/Users/x/.npm/_npx/f/node_modules/esbuild/lib/main.js:%d:7)\n"
                 % (i, 800 + i) for i in range(14))
TLA_OUT = (
    "=== behavioural tests (npx --yes tsx --test) ===\nTAP version 13\n"
    "# node:internal/modules/run_main:107\n#     triggerUncaughtException(\n#     ^\n"
    "# Error: Transform failed with 1 error:\n"
    "# /wt/verify.test.ts:2:10: ERROR: Top-level await is currently not supported with "
    "the \"cjs\" output format\n" + FRAMES +
    "#   name: 'TransformError'\n# }\n# Node.js v26.7.0\n# Subtest: verify.test.ts\n"
    "not ok 1 - verify.test.ts\n  ---\n  duration_ms: 95.6\n  failureType: 'testCodeFailure'\n"
    "  exitCode: 1\n  error: 'test failed'\n  code: 'ERR_TEST_FAILURE'\n  ...\n1..1\n"
    "# tests 1\n# suites 0\n# pass 0\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n"
    "# duration_ms 99.6\n  FAIL: new test file(s) failed (exit 1)\n")
MOCK_OUT = (
    "=== behavioural tests (npx --yes tsx --test) ===\nTAP version 13\n"
    "# Subtest: b\nnot ok 2 - b\n  ---\n  location: '/wt/verify.test.ts:4:1'\n"
    "  failureType: 'testCodeFailure'\n"
    "  error: \"Invalid state: Cannot mock '@/lib/auth'. The module is already mocked.\"\n"
    "  code: 'ERR_INVALID_STATE'\n  stack: |-\n"
    "    MockTracker.module (node:internal/test_runner/mock/mock:676:13)\n"
    + "".join("    frame%d (/wt/x.ts:%d:1)\n" % (i, i) for i in range(40)) +
    "  ...\n1..2\n# tests 2\n# pass 1\n# fail 1\n")
ASSERT_OUT = ("=== behavioural tests ===\nnot ok 1 - x\n  error: 'AssertionError: 0 !== 2'\n"
              "# tests 1\n# fail 1\n")


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[-700:]))
    if not ok:
        FAILS.append(name)


def load(path=AUTO, name="oda_tla"):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def mk_wt(m, out_text):
    wt = Path(tempfile.mkdtemp(prefix="tla-")) / "wt"
    (wt / "lib").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "lib" / "x.ts").write_text("export const x = 1;\n")
    g("add", "."); g("commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `lib/x.ts`\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / "out.txt").write_text(out_text)
    (wt / "verify.sh").write_text(
        'grep -q MARK lib/x.ts || { echo "FAIL - no MARK"; exit 1; }\n'
        'cat out.txt; exit 1\n')
    (wt / "refimpl.py").write_text("open('lib/x.ts','a').write('// MARK\\n')\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/x.ts"}))
    m.write_harness_check(wt, "ts")
    return wt


def run(wt):
    return subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True, timeout=120).stdout


def main():
    os.environ.setdefault("OLLAMA_DISPATCH_HOME", tempfile.mkdtemp(prefix="tla-home-"))
    m = load()

    # ---- (a) transform error carried past the tail + top-level-await hint
    o = run(mk_wt(m, TLA_OUT))
    check("a: Transform failed line carried above the tail",
          "Error: Transform failed with 1 error:" in o.split("=== verify output tail ===")[0], o)
    check("a: ERROR: line (file:line:col) carried",
          "/wt/verify.test.ts:2:10: ERROR: Top-level await" in o, o)
    check("a: top-level-await hint", "It is TOP-LEVEL AWAIT" in o
          and "before(async () =>" in o and "STATIC import" in o, o)
    check("a: says no case ran / compile error", "NO test case ran" in o
          and "compile error" in o, o)
    check("a: no fake-environment blame", "suspect the FAKE ENVIRONMENT" not in o, o)
    gen = TLA_OUT.replace("Top-level await is currently not supported with the \"cjs\" "
                          "output format", "Expected \";\" but found \"x\"")
    o_g = run(mk_wt(m, gen))
    check("a: non-TLA transform error gets the generic compile hint, not TLA",
          "compile error" in o_g and "TOP-LEVEL AWAIT" not in o_g, o_g)

    # ---- (b) already mocked
    o2 = run(mk_wt(m, MOCK_OUT))
    check("b: already-mocked hint names the module",
          "Cannot mock '@/lib/auth'. The module is already mocked` -- " in o2, o2)
    check("b: says mock ONCE / .restore()", "ONCE at the top of the" in o2
          and ".restore()" in o2, o2)
    check("b: no fake-environment blame", "suspect the FAKE ENVIRONMENT" not in o2, o2)

    # control
    o3 = run(mk_wt(m, ASSERT_OUT))
    check("control: assertion failure keeps the fake hint",
          "suspect the FAKE ENVIRONMENT" in o3, o3)
    check("control: no compile / mock hint", "compile error" not in o3
          and "already mocked" not in o3, o3)

    # continuation note agrees with the hints
    mark = m.REFIMPL_OWN_TESTS_MARK
    n1 = m.refimpl_fails_own_tests_note(mark + "\n" + TLA_OUT, "ts")
    check("note: compile signal, no fake blame", "did not compile" in n1
          and "fake environment is the usual" not in n1, n1)
    n2 = m.refimpl_fails_own_tests_note(mark + "\n" + MOCK_OUT, "ts")
    check("note: mocked-twice signal", "mocked twice" in n2, n2)
    n3 = m.refimpl_fails_own_tests_note(mark + "\n" + ASSERT_OUT, "ts")
    check("note control: fake signal kept", "fake environment is the usual" in n3, n3)

    # ---- (c) refresh from the ON-DISK template
    td = Path(tempfile.mkdtemp(prefix="tla-src-"))
    src = td / "ollama-dispatch-auto"
    shutil.copy(AUTO, src)
    ms = load(src, "oda_tla_src")
    wt = mk_wt(ms, ASSERT_OUT)
    hc = wt / "auto-harness-check.py"
    check("c: freshly written check is current", ms.refresh_harness_check(wt, "ts") == "", "")
    # the driver keeps running; the script on disk is edited under it
    txt = src.read_text()
    edited = txt.replace('print("VERIFY_OK: harness discriminates and is satisfiable")',
                         'print("VERIFY_OK: harness discriminates and is satisfiable")\n'
                         '# ONDISK_TEMPLATE_MARKER', 1)
    assert edited != txt
    src.write_text(edited)
    check("c: in-memory template does NOT have the marker (precondition)",
          "ONDISK_TEMPLATE_MARKER" not in ms.HARNESS_CHECK, "")
    r = ms.refresh_harness_check(wt, "ts")
    check("c: edited on-disk template -> 'stale' + rewritten", r == "stale"
          and "ONDISK_TEMPLATE_MARKER" in hc.read_text(), r)
    check("c: second refresh is a no-op", ms.refresh_harness_check(wt, "ts") == "", "")
    hc.unlink()
    check("c: missing -> regenerated", ms.refresh_harness_check(wt, "ts") == "missing"
          and hc.is_file(), "")
    # a disk template this driver cannot fill -> in-memory fallback, never a broken check
    src.write_text(txt.replace("_REQUIRED = __REQUIRED_LITERALS__",
                               "_REQUIRED = __REQUIRED_LITERALS__\n_NEWTHING = __NEW_PLACEHOLDER__", 1))
    ms.refresh_harness_check(wt, "ts")
    check("c: unknown placeholder on disk -> in-memory template",
          "__NEW_PLACEHOLDER__" not in hc.read_text()
          and hc.read_text() == ms.render_harness_check("ts", (), ms.HARNESS_CHECK), "")
    src.write_text(txt[: len(txt) // 2])           # mid-edit, does not parse
    ms.refresh_harness_check(wt, "ts")
    check("c: unparseable source on disk -> in-memory template",
          hc.read_text() == ms.render_harness_check("ts", (), ms.HARNESS_CHECK), "")
    src.write_text(edited)

    # dispatch_model refreshes a STALE check before enqueueing (fake queue, no real enqueue)
    hc.write_text("# stale check from an older driver\nprint('VERIFY_OK')\n")
    fq = td / "fake-queue.py"
    fq.write_text("import sys\nsys.exit(1)\n")
    ms.QUEUE = fq
    a = SimpleNamespace(drafter_cmd=None, lang="ts", require=(), model="m", host="h",
                        num_ctx=1024, author_max_iters=1, bundle="b", max_tokens=None,
                        timeout=1, label="tla-test", slice_plan=None, slice_id=None,
                        repo=str(wt), new_project=None, intent="x")
    try:
        ok, why = ms.dispatch_model(wt, "prompt", "tla-test", "python3 auto-harness-check.py", a)
    except SystemExit as e:  # noqa
        ok, why = False, f"SystemExit {e}"
    check("c: dispatch_model rewrote the stale check before enqueue",
          "ONDISK_TEMPLATE_MARKER" in hc.read_text(), why)
    check("c: fake queue enqueue failed cleanly (no job)", ok is False, why)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


CUTS = [
    ("a-carry", "    # TRANSFORM ERRORS ABOVE THE TAIL", "    if any(l.strip() not in tail for l in _secf):"),
    ("a-hint", "    # TRANSFORM FAILED / TOP-LEVEL AWAIT UNDER CJS",
     "    # `Cannot mock 'X'. The module is already mocked.`"),
    ("b-hint", "    # `Cannot mock 'X'. The module is already mocked.`",
     "    # REPEATED CASE -> SUSPECT THE FIXTURE"),
]


def revert_check():
    src = AUTO.read_text()
    variants = []
    for tag, s, e in CUTS:
        a = src.index(s)
        b = src.index(e, a + len(s))
        v = src[:a] + src[b:]
        if tag == "a-hint":   # keep the name the b block reads defined
            v = v.replace("    # `Cannot mock 'X'.", "    _vout = (r.stdout or '') + '\\n' + "
                          "(r.stderr or ''); _tfm = None\n    # `Cannot mock 'X'.", 1)
        variants.append((tag, v))
    # c: dispatch_model back to missing-only repair
    a = src.index("    if \"auto-harness-check.py\" in verify_cmd:\n        try:\n"
                  "            _hc = refresh_harness_check(")
    b = src.index("    if \"auto-harness-check.py\" in verify_cmd and not", a)
    variants.append(("c-enqueue", src[:a] + "    if \"auto-harness-check.py\" in verify_cmd "
                     "and not (wt / \"auto-harness-check.py\").is_file():\n"
                     "        write_harness_check(wt, a.lang, getattr(a, 'require', ()))\n" + src[b:]))
    # c: render from in-memory template only
    variants.append(("c-ondisk", src.replace(
        "    t = template if template is not None else harness_check_template_on_disk()",
        "    t = template if template is not None else HARNESS_CHECK", 1)))
    allred = True
    for tag, v in variants:
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(v)
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        allred &= red
        print(("bites" if red else "INERT") + f": revert {tag} -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if allred else "REVERT-CHECK FAILED")
    return 0 if allred else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
