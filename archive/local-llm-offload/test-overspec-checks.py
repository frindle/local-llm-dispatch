#!/usr/bin/env python3
"""OVER-SPECIFICATION checks (2026-10-11): cardlink s2 + arr-webhook burned model rounds on fixtures that pinned things
TASK.md never states. Static half = SPEC_TEST_PARITY extension (stubbed seams, return shapes, literal call counts,
source-text matching); dynamic half = the N-version variant check (preflight `nversion-variant`).
  test-overspec-checks.py                # run -> OVERSPEC_CHECKS_OK
  test-overspec-checks.py --revert-check # disable each rule in turn; every one must go RED
HG_SRC / PF_SRC env override the modules under test (used by --revert-check).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
HG_SRC = Path(os.environ.get("HG_SRC") or HERE / "dispatch_harness_gates.py")
PF_SRC = Path(os.environ.get("PF_SRC") or HERE / "ollama-dispatch-preflight")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:400]))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)


def mkwt(root, base_target, fixture_name, fixture, task, target="arr.py"):
    wt = Path(root)
    wt.mkdir(parents=True, exist_ok=True)
    g(wt, "init", "-q")
    (wt / target).write_text(base_target)
    g(wt, "add", target)
    g(wt, "commit", "-qm", "base")
    (wt / fixture_name).write_text(fixture)
    (wt / "TASK.md").write_text(task)
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": target}))
    return wt


BASE_PY = ("def get_all_torrents():\n    return []\n\ndef record_activity(x):\n    pass\n\n"
           "def old_entry():\n    return get_all_torrents()\n")
FX_SEAM = ("import importlib.util\nspec = importlib.util.spec_from_file_location('target', 'arr.py')\n"
           "target = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(target)\n"
           "target.get_all_torrents = lambda: [1]\ntarget.record_activity = lambda x: None\n"
           "def test_a():\n    assert target.maintain() == 2\n")
TASK_HEAD = "# TASK\n## Confirmed defect\nget_all_torrents is slow.\n"
TASK_NO = TASK_HEAD + "## Entry point\n`maintain()` -> int\n## Required change\nPromote slots.\n"
TASK_YES = TASK_NO + "## Interface details\nFetch with `get_all_torrents()` (the stubbed seam; no other route).\n"


def main():
    sp = importlib.util.spec_from_file_location("hg_t", HG_SRC)
    hg = importlib.util.module_from_spec(sp)
    sys.modules["hg_t"] = hg
    sp.loader.exec_module(hg)

    def reasons(wt, task):
        return [f["reason"] for f in hg.spec_test_parity_findings(wt, task)]

    R_SEAM, R_SHAPE = "Test stubs a seam the spec never names", "Test asserts a return shape the spec contradicts or omits"
    R_COUNT, R_SRC = "Test pins an exact call count the spec never states", "Test identifies calls by matching the callback's source text"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # ---------------- (1) stubbed seams
        wt = mkwt(root / "seam", BASE_PY, "test_fixture.py", FX_SEAM, TASK_NO)
        check("seam: data-feeding stub of a base function the NEW entry never gets told about -> flagged",
              reasons(wt, TASK_NO) == [R_SEAM], reasons(wt, TASK_NO))
        fs = hg.spec_test_parity_findings(wt, TASK_NO)
        check("seam: finding names the seam and is a SPEC_TEST_PARITY (admission path)",
              fs and fs[0]["code"] == hg.SPEC_TEST_PARITY and "`get_all_torrents`" in fs[0]["message"]
              and "record_activity" not in fs[0]["message"], fs)
        check("seam: side-effect-only stub (record_activity) is not a data route -> never flagged",
              "record_activity" not in "".join(f["message"] for f in fs))
        check("seam: naming it in an Interface section clears it", reasons(wt, TASK_YES) == [])
        check("seam: a mention only in 'Confirmed defect' (not a spec section) does NOT count",
              reasons(wt, TASK_NO) == [R_SEAM])
        check("seam: spec_defects() (preflight admission) carries it",
              any(f["code"] == hg.SPEC_TEST_PARITY for f in hg.spec_defects(wt, target="arr.py", creation=False, task_text=TASK_NO)))
        fx2 = FX_SEAM.replace("target.maintain()", "target.old_entry()")
        (wt / "test_fixture.py").write_text(fx2)
        check("seam near-miss: entry already exists at base and ITSELF calls the seam -> edit-in-place, not a route choice",
              reasons(wt, TASK_NO) == [], reasons(wt, TASK_NO))
        base2 = BASE_PY + "\ndef helper_chain():\n    return old_entry()\n"
        wt2 = mkwt(root / "seam2", base2, "test_fixture.py", FX_SEAM.replace("target.maintain()", "target.helper_chain()"), TASK_NO)
        check("seam near-miss: reached transitively (entry -> old_entry -> seam) is still derivable", reasons(wt2, TASK_NO) == [])
        fx3 = FX_SEAM.replace("target.get_all_torrents = lambda: [1]\n", "target.brand_new_helper = lambda: [1]\n")
        (wt / "test_fixture.py").write_text(fx3)
        check("seam near-miss: stubbing a name the BASE never had (a new function the task adds) is not a seam",
              reasons(wt, TASK_NO) == [])
        (wt / "test_fixture.py").write_text(FX_SEAM.replace("target.get_all_torrents = lambda: [1]\n",
                                                           "import requests\nrequests.get = lambda *a: 1\n"))
        check("seam near-miss: external library attribute (requests.get) is not a target seam", reasons(wt, TASK_NO) == [])
        rpc = ("class S:\n    def post(self, url, json=None):\n        method = json['method']\n"
               "        if method == 'core.get_torrents_status':\n            return {}\n")
        (wt / "test_fixture.py").write_text(rpc)
        check("seam: JSON-RPC method literal the fake dispatches on, absent from TASK.md -> flagged",
              reasons(wt, TASK_NO) == [R_SEAM], reasons(wt, TASK_NO))
        check("seam: ...named (full dotted or last segment) in an Interface section -> clean",
              reasons(wt, TASK_NO + "## Interface\nPOST method `core.get_torrents_status`.\n") == []
              and reasons(wt, TASK_NO + "## Interface\nuse get_torrents_status.\n") == [])
        # JS
        js_base = "export function fetchRows(page) { return page.evaluate(() => 1); }\nexport async function loadCard(x) { return x; }\n"
        js_fx = ("import { test, mock } from 'node:test';\nimport assert from 'node:assert';\n"
                 "mock.method(deps, 'loadCard', async () => 1);\ntest('a', () => { assert.ok(true); });\n")
        wtj = mkwt(root / "js", js_base, "verify.test.ts", js_fx, TASK_NO, target="m.ts")
        check("seam (JS): mock.method on a function the base defines/imports, never named -> flagged",
              reasons(wtj, TASK_NO) == [R_SEAM], reasons(wtj, TASK_NO))
        check("seam (JS): named -> clean", reasons(wtj, TASK_NO + "## Interface\n`loadCard(x)` is the seam.\n") == [])

        # ---------------- (2) return shape
        shape_fx = FX_SEAM.replace("    assert target.maintain() == 2\n", "    count, calls = target.maintain()\n    assert count == 2\n").replace(
            "target.get_all_torrents = lambda: [1]\n", "")
        wts = mkwt(root / "shape", BASE_PY, "test_fixture.py", shape_fx, TASK_NO)
        check("shape: fixture unpacks a tuple while TASK.md says `-> int` -> flagged as contradiction",
              reasons(wts, TASK_NO) == [R_SHAPE]
              and "declares a plain scalar" in hg.spec_test_parity_findings(wts, TASK_NO)[0]["message"], reasons(wts, TASK_NO))
        check("shape: 'NOT a tuple' in the spec is a contradiction too",
              reasons(wts, TASK_HEAD + "## Interface\n`maintain()` returns an int -- NOT a tuple.\n") == [R_SHAPE])
        check("shape: spec declares `-> (count, calls)` -> clean",
              reasons(wts, TASK_HEAD + "## Interface\n`maintain()` -> (count, calls) tuple\n") == [])
        check("shape: spec never names the entry function -> flagged 'never names it'",
              "never names it" in hg.spec_test_parity_findings(wts, TASK_HEAD + "## Required change\nx\n")[0]["message"])
        tup_base = BASE_PY + "\ndef handle(m, p):\n    return 200, {}, 'ok'\n"
        wtt = mkwt(root / "shape2", tup_base, "test_fixture.py",
                   "import importlib.util\nspec = importlib.util.spec_from_file_location('target','arr.py')\n"
                   "target = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(target)\n"
                   "status, headers, out = target.handle('POST', '/x')\n", TASK_NO)
        check("shape near-miss: BASE already returns that tuple (visible in the edited code) -> clean", reasons(wtt, TASK_NO) == [])

        # ---------------- (3) exact call counts
        cnt_fx = "def test_a():\n    assert len(page.calls) == 16\n    assert env.reads == 16\n"
        wtc = mkwt(root / "cnt", BASE_PY, "test_fixture.py", cnt_fx, TASK_NO)
        check("count: `len(page.calls) == 16` with no such number in the spec -> flagged once",
              reasons(wtc, TASK_NO) == [R_COUNT], reasons(wtc, TASK_NO))
        check("count: stated in the spec ('16 reads') -> clean", reasons(wtc, TASK_NO + "## Exact behaviour contract\n16 reads, one per attempt\n") == [])
        check("count: stated in Required change counts too (section scope is Interface/contract/required-change)",
              reasons(wtc, TASK_HEAD + "## Required change\nRe-read the page 16 times.\n") == [])
        check("count: a mention OUTSIDE those sections (Confirmed defect) does not justify it",
              reasons(wtc, TASK_HEAD + "It polls 16 times.\n## Required change\nx\n") == [R_COUNT])
        (wtc / "test_fixture.py").write_text("def test_a():\n    assert len(page.calls) >= 16\n    assert len(page.calls) <= 20\n")
        check("count near-miss: inequalities never fire", reasons(wtc, TASK_NO) == [])
        (wtc / "test_fixture.py").write_text("def test_a():\n    assert len(page.calls) == 2\n    assert len(calls) == 3\n    assert len(page.calls) == 1\n")
        check("count near-miss: structural counts 1..3 are exempt (first page + short page)", reasons(wtc, TASK_NO) == [])
        (wtc / "test_fixture.py").write_text("def test_a():\n    assert update_calls[0]['data']['buyerId'] == 9\n    assert update_calls[0].count == 9\n    assert result.length == 7\n    assert len(items) == 12\n")
        check("count near-miss: a FIELD of one call (calls[0].x == 9), data lengths (result/items) are not call counts",
              reasons(wtc, TASK_NO) == [])
        (wtc / "test_fixture.py").write_text("test('a', () => {\n  expect(evals).toBe(17);\n  expect(clock.now).toBe(8000);\n});\n")
        os.replace(wtc / "test_fixture.py", wtc / "verify.test.ts")
        rs = reasons(wtc, TASK_NO)
        check("count: no double report with the older quantity rule (17 and 8000 -> exactly ONE finding)",
              len(rs) == 1, rs)

        # ---------------- (4) source-text matching
        src_fx = ("import { test } from 'node:test';\nconst page = { async evaluate(fn) {\n"
                  "  if (/payment method/.test(String(fn))) { counter++; return 1; }\n  return 0; } };\ntest('a', () => {});\n")
        wtm = mkwt(root / "src", "export function f() {}\n", "verify.test.ts", src_fx, TASK_NO, target="m.ts")
        check("source match: counter keyed on /payment method/.test(String(fn)) -> flagged (cardlink s2)",
              reasons(wtm, TASK_NO) == [R_SRC], reasons(wtm, TASK_NO))
        (wtm / "verify.test.ts").write_text(src_fx.replace("/payment method/.test(String(fn))", "String(fn).includes('payment method')"))
        check("source match: String(fn).includes(...) form too", reasons(wtm, TASK_NO) == [R_SRC])
        (wtm / "verify.test.ts").write_text("import { test } from 'node:test';\nconst page = { async evaluate(fn) {\n  const src = fn.toString();\n"
                                            "  if (src.includes('payment method')) { counter++; }\n  return 0; } };\ntest('a', () => {});\n")
        check("source match: via an assigned `const src = fn.toString()` too", reasons(wtm, TASK_NO) == [R_SRC])
        (wtm / "verify.test.ts").write_text("import { test } from 'node:test';\ntest('a', () => {\n"
                                            "  assert.ok(String(body.error).includes('but 5 are stored'));\n  assert.ok(String(res).includes('ok'));\n"
                                            "  if (fn.name === 'extractDetailInBrowser') counter++;\n});\n")
        check("source match near-miss: String(<error/body>).includes(msg) and fn.name === 'x' are NOT source matching",
              reasons(wtm, TASK_NO) == [], reasons(wtm, TASK_NO))
        (wtm / "verify.test.ts").write_text(src_fx)
        check("source match: the matched string named in an Interface section clears it",
              reasons(wtm, TASK_NO + "## Interface\nMatch `payment method`.\n") == [])

        # ---------------- (ii) N-version variants (pure)
        base = "def f(s):\n    return 1\n"
        app = "def f(s):\n    data = get_all(s)\n    s.post(data)\n    return len(data)\n"
        vs = hg.nversion_variants(base, app)
        check("nversion: an added read is duplicated -> one parsable variant", len(vs) == 1 and vs[0][1].count("get_all(s)") == 2, vs)
        check("nversion: stream-consuming bare load/read (json.load(fh), fh.read()) is never duplicated",
              hg.nversion_variants(base, "def f(fh):\n    data = json.load(fh)\n    return data\n") == []
              and hg.nversion_variants(base, "def f(fh):\n    data = fh.read()\n    return data\n") == [])
        check("nversion: a read already at base (not added by the refimpl) is left alone",
              hg.nversion_variants(app, app) == [])
        check("nversion: added NON-read call (s.post) is never duplicated (not idempotent)",
              hg.nversion_variants(base, "def f(s):\n    s.post(1)\n    return 1\n") == [])
        check("nversion: unparsable / non-python text -> no variants",
              hg.nversion_variants(base, "function f() {}") == [] and hg.nversion_variants(base, "") == [])
        wtn = mkwt(root / "nv", "x = 1\n", "test_fixture.py", "", TASK_NO, target="t.py")
        (wtn / "t.py").write_text(app)
        seen = []
        fs = hg.nversion_findings(wtn, "t.py", base, app, lambda: (seen.append((wtn / "t.py").read_text()) or True, ""))
        check("nversion: variant passes -> no finding; variant text was on disk DURING the run; applied text restored",
              fs == [] and seen and seen[0].count("get_all(s)") == 2 and (wtn / "t.py").read_text() == app, (fs, seen))
        fs = hg.nversion_findings(wtn, "t.py", base, app, lambda: (False, "AssertionError: reads == 16"))
        check("nversion: variant FAILS the fixture -> NVERSION_VARIANT_FAILS naming the variant and first failure; restored",
              len(fs) == 1 and fs[0]["code"] == hg.NVERSION_VARIANT_FAILS and "get_all(...) called twice" in fs[0]["message"]
              and "AssertionError" in fs[0]["message"] and (wtn / "t.py").read_text() == app, fs)

        def boom():
            raise RuntimeError("verify exploded")
        try:
            hg.nversion_findings(wtn, "t.py", base, app, boom)
        except RuntimeError:
            pass
        check("nversion: even when the verify run raises, the applied text is restored", (wtn / "t.py").read_text() == app)

    # ---------------- (ii) end to end through the real preflight
    pl = SourceFileLoader("pf_t", str(PF_SRC))
    spf = importlib.util.spec_from_loader("pf_t", pl)
    pm = importlib.util.module_from_spec(spf)
    pl.exec_module(pm)

    def e2e(count_assert):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"
            wt.mkdir()
            g(wt, "init", "-q")
            (wt / "t.py").write_text("LOG = []\n\ndef get_items():\n    LOG.append(1)\n    return [1, 2]\n\ndef f():\n    return 0\n")
            g(wt, "add", "t.py")
            g(wt, "commit", "-qm", "base")
            (wt / "TASK.md").write_text("# Task\nMake f() return the item count in `t.py`.\n\nOnly edit `t.py`.\n"
                                        "Run `bash verify.sh` after every edit until it prints VERIFY_OK.\n\n## Must contain\n\n- `len(items)`\n")
            (wt / "test_fixture.py").write_text(
                "import t\nr = t.f()\nassert r == 2, r\n" + count_assert + "\n")
            (wt / "verify.sh").write_text("python3 test_fixture.py && echo VERIFY_OK || { echo FAIL; exit 1; }\n")
            (wt / "refimpl.py").write_text("from pathlib import Path\np = Path('t.py')\ns = p.read_text()\n"
                                           "p.write_text(s.replace('def f():\\n    return 0\\n', 'def f():\\n    items = get_items()\\n    return len(items)\\n'))\n")
            r = subprocess.run([sys.executable, str(PF_SRC), str(wt), "--refimpl-cmd", "python3 refimpl.py", "--no-relevance",
                                "--target", "t.py", "--json"], capture_output=True, text=True, timeout=300,
                               env={**os.environ, "HOME": d})
            try:
                dd = json.loads(r.stdout)
            except ValueError:
                return None, r.stderr[-500:], (wt / "t.py").read_text() if (wt / "t.py").exists() else ""
            rows = {c.get("id") or c.get("check"): c for c in (dd.get("checks") or dd.get("results") or [])}
            return rows.get("nversion-variant"), r.stdout[-400:], (wt / "t.py").read_text()

    row, tail, tgt_after = e2e("assert len(t.LOG) == 1, t.LOG")
    check("e2e: a fixture pinning the read count (== 1 read) FAILS the extra-read variant -> preflight WARN nversion-variant",
          row is not None and row.get("status") == "WARN", (row, tail))
    check("e2e: the target is back to base after preflight (refimpl reverted, variant gone)",
          "get_items()\n    return len" not in tgt_after, tgt_after)
    row, tail, _t = e2e("assert len(t.LOG) >= 1, t.LOG")
    check("e2e: an inequality fixture passes the variant -> PASS (healthy harness not flagged)",
          row is not None and row.get("status") == "PASS", (row, tail))
    print("OVERSPEC_CHECKS_OK" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTATIONS = [
    ("hg", "seam check never reports", "        if miss:\n            out.append(_finding(", "        if False:\n            out.append(_finding("),
    ("hg", "seam side-effect filter dropped", "                if _seam_returns_data(s) and not _seam_named(s, section)", "                if not _seam_named(s, section)"),
    ("hg", "seam edit-in-place exemption dropped", "                and not _base_entry_reaches(base_text, _entry_calls(fx, text), s, fx.endswith('.py'))]", "                ]"),
    ("hg", "seam: any mention counts (section scoping ignored)", "        if not _OVERSPEC_SECTION_RE.search(title):\n            continue\n        end = next((n2 for n2, l2, _t in heads[k + 1:] if l2 <= lvl), len(lines))\n        out.append(",
     "        end = next((n2 for n2, l2, _t in heads[k + 1:] if l2 <= lvl), len(lines))\n        out.append("),
    ("hg", "return shape never checked", "        for fn, (shape, ex) in sorted(_entry_shapes(fx, text).items()):", "        for fn, (shape, ex) in []:"),
    ("hg", "base-tuple exemption dropped", " or (fx.endswith(\".py\") and _base_returns_tuple(base_text, fn))", ""),
    ("hg", "exact counts never checked", "        if bad:\n            out.append(_finding(", "        if False:\n            out.append(_finding("),
    ("hg", "count minimum dropped to 1", "OVERSPEC_MIN_COUNT = 4", "OVERSPEC_MIN_COUNT = 1"),
    ("hg", "n-version: bare load/read duplicated too", "(?!(?:load|read)$)", ""),
    ("hg", "count: single-entry field accepted as a count", "    if re.search(r\"\\]\\s*[.\\[]\", a) or re.fullmatch(r\"[\\d\\s]+\", a):\n        return False\n", ""),
    ("hg", "count dedupe against older rule dropped", "    skip = set(re.findall(r\"\\b\\d+\\b\", \" \".join(f[\"message\"] for f in old)))", "    skip = set()"),
    ("hg", "source match never reports", "            if hits:\n                out.append(_finding(", "            if False:\n                out.append(_finding("),
    ("hg", "source match: any String(x) accepted", "_CB = r\"(?:fn|func|f|cb|callback|script|code|expr|expression|pageFunction|handler|source|src|evalFn|pageFn)\\b\"", "_CB = r\"[\\w$.]+\\b\""),
    ("hg", "not wired into the parity entry point", "    return old + overspec_findings(wt, task_text, skip_quantities=skip)", "    return old"),
    ("hg", "n-version: no variants generated", "            out.append((f\"{nm}(...) called twice", "            continue\n            out.append((f\"{nm}(...) called twice"),
    ("hg", "n-version: applied text not restored", "        finally:\n            p.write_text(applied_text)", "        finally:\n            pass"),
    ("hg", "n-version: failing variant ignored", "        if not ok:\n            first = next(", "        if False:\n            first = next("),
    ("hg", "n-version: non-read call duplicated too", "_NV_READ_RE = re.compile(r\"^_*(?!(?:load|read)$)(?:get|fetch|list|load|read|query|find|search|lookup)(?:_|[A-Z0-9]|$)\")", "_NV_READ_RE = re.compile(r\".\")"),
    ("pf", "preflight never runs the n-version check", "            if green:\n                self.check_nversion()\n", ""),
    ("pf", "preflight n-version failure is silent", "        if fs:\n            self.add(\"nversion-variant\", WARN,", "        if False:\n            self.add(\"nversion-variant\", WARN,"),
]


def revert_check():
    bad = 0
    texts = {"hg": HG_SRC.read_text(), "pf": PF_SRC.read_text()}
    for which, name, old, new in MUTATIONS:
        src = texts[which]
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False, dir=str(HERE)) as fh:
            fh.write(src.replace(old, new))
        env = {**os.environ, "HG_SRC": fh.name} if which == "hg" else {**os.environ, "PF_SRC": fh.name}
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=900)
        os.unlink(fh.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
