#!/usr/bin/env python3
"""Test-file targets: verify-relevance is N/A for auto's LOCKED runner fixture, so
auto never dispatches an unwinnable refine (2026-10-03, idle-test-testfile-py
f02404b2cdba: 12 survivors in refimpl's NEW test code, refine told the model to
add cases to a locked test_fixture.py -> output_cap_loop -> persistent-nogo).

Asserted:
  1. preflight on a python test-file target with the locked fixture: relevance row
     is UNPR "N/A for a TEST-FILE target" (no survivors, never FAIL/LOW)
  2. SPOOF: a fixture carrying the locked marker comment but different bytes is
     still MEASURED (cannot opt out by copying a comment)
  3. gate _locked_test_target_fixture: True for exact bytes; False for edited
     bytes, for a non-test target, and with no manifest
  4. gate measure_relevance short-circuits to not_applicable for the locked fixture

PF_SRC / GATE_SRC override the preflight / gate under test (revert-check: the .baks).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
PF = Path(os.environ.get("PF_SRC") or BIN / "ollama-dispatch-preflight")
GATE = Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py")
FAILS = []


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    ld.exec_module(m)
    return m


AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
oda = load("oda_tt", AUTO)


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


PRICING = ('def apply_discount(price, pct):\n    if pct < 0 or pct > 100:\n'
           '        raise ValueError("pct out of range")\n'
           '    return round(price * (100 - pct) / 100, 2)\n')
TEST_BAD = ('import pricing\n\n\ndef test_basic():\n    assert pricing.apply_discount(100, 10) == 90\n\n\n'
            'def test_fifteen():\n    assert pricing.apply_discount(100, 15) == 80\n')
TEST_GOOD = TEST_BAD.replace("== 80", "== 85") + (
    '\n\ndef test_invalid_pct():\n    try:\n        pricing.apply_discount(100, 101)\n'
    '        assert False, "expected ValueError"\n    except ValueError:\n        pass\n')


def g(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def build(wt: Path, spoof=False, target="test_pricing.py"):
    wt.mkdir(parents=True)
    g(wt, "init", "-q")
    (wt / "pricing.py").write_text(PRICING)
    (wt / "test_pricing.py").write_text(TEST_BAD)
    g(wt, "add", "-A")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (wt / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": ["TASK.md", "verify.sh", "test_fixture.py", "refimpl.py"],
         "target": target, "fixture": "test_fixture.py"}))
    fx = oda.test_target_fixture(target)
    if spoof:
        fx = fx + "\n# tweaked by the author\n"
    (wt / "test_fixture.py").write_text(fx)
    (wt / "TASK.md").write_text(
        "# TASK\nFix test_fifteen in `test_pricing.py` (85, not 80) and add "
        "test_invalid_pct.\n\n## Must contain\n\n- `def test_invalid_pct`\n\nOnly edit `test_pricing.py`.\nRun `bash verify.sh` "
        "after every edit until it prints VERIFY_OK.\n")
    (wt / "verify.sh").write_text(
        "export PYTHONDONTWRITEBYTECODE=1\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n")
    (wt / "refimpl.py").write_text(
        f"from pathlib import Path\nPath('test_pricing.py').write_text({TEST_GOOD!r})\n")
    return wt


def preflight_row(wt, home):
    r = subprocess.run([sys.executable, str(PF), str(wt), "--refimpl-cmd",
                        "python3 refimpl.py", "--target", "test_pricing.py", "--json",
                        "--relevance-max-mutants", "20"],
                       capture_output=True, text=True, timeout=600,
                       env={**os.environ, "HOME": str(home)})
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    for c in d.get("checks") or []:
        if c.get("check") == "verify-relevance":
            return c
    return {}


def main():
    with tempfile.TemporaryDirectory() as td:
        T = Path(td)
        home = T / "home"
        home.mkdir()

        wt = build(T / "locked")
        row = preflight_row(wt, home)
        check("locked test-target fixture: relevance UNPR", row.get("status"), "UNPR")
        check("locked test-target fixture: says N/A test-file target",
              "N/A for a TEST-FILE target" in (row.get("message") or ""), True)

        wt = build(T / "spoof", spoof=True)
        row = preflight_row(wt, home)
        check("spoofed fixture (marker, different bytes) is still measured",
              "N/A for a TEST-FILE target" in (row.get("message") or ""), False)

        # 5. the locked fixture is BYTE-DETERMINISTIC across processes (a set repr
        #    varied with PYTHONHASHSEED, so every round "restored" a churned file)
        outs = set()
        for seed in ("1", "2", "3", "4"):
            r = subprocess.run([sys.executable, "-c",
                "import importlib.util,sys\nfrom importlib.machinery import SourceFileLoader as S\n"
                "l=S('a',sys.argv[1]);s=importlib.util.spec_from_loader('a',l);"
                "m=importlib.util.module_from_spec(s);l.exec_module(m);"
                "sys.stdout.write(m.test_target_fixture('test_pricing.py'))", str(AUTO)],
                capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed})
            outs.add(r.stdout)
        check("locked fixture identical across PYTHONHASHSEED", len(outs), 1)

        # 6. the REAL human sequence (2026-10-03 idle-test-testfile-py2): auto writes
        #    the locked fixture -> auto.mark_unconfirmed stamps it -> the human runs
        #    `ollama-dispatch-draft <wt> --confirm` (leaves a stray leading '\n').
        #    The detector must be True pre- AND post-confirm, and False once a code
        #    line is edited, in BOTH preflight and gate.
        pf = load("pf_tt", PF)
        gate0 = load("goc_tt0", GATE)
        for edit in (False, True):
            wt = build(T / ("seq-edit" if edit else "seq"))
            oda._SCAFFOLD_FIXTURE = "test_fixture.py"
            ok, why = oda.mark_unconfirmed(wt, "python")
            body = (wt / "test_fixture.py").read_text()
            check(f"seq{'-edit' if edit else ''}: auto stamped the marker",
                  ok and "DRAFT_UNCONFIRMED = True" in body, True)
            if not edit:
                check("seq: marked (pre-confirm) -> preflight detector True",
                      pf.locked_test_target_fixture(wt), True)
            r = subprocess.run([sys.executable, str(BIN / "ollama-dispatch-draft"), str(wt),
                                "--confirm"], capture_output=True, text=True, timeout=120)
            body = (wt / "test_fixture.py").read_text()
            check(f"seq{'-edit' if edit else ''}: draft --confirm removed the marker",
                  r.returncode == 0 and "DRAFT_UNCONFIRMED" not in body, True)
            if not edit:
                check("seq: confirm leaves the real residue (file differs byte-wise)",
                      body != oda.test_target_fixture("test_pricing.py"), True)
            else:
                (wt / "test_fixture.py").write_text(body.replace("timeout=600", "timeout=6"))
            want = not edit
            check(f"seq{'-edit' if edit else ''}: post-confirm preflight detector {want}",
                  pf.locked_test_target_fixture(wt), want)
            check(f"seq{'-edit' if edit else ''}: post-confirm gate detector {want}",
                  gate0._locked_test_target_fixture(wt), want)
            if not edit:
                row = preflight_row(wt, home)
                check("seq: post-confirm preflight relevance row says N/A test-file target",
                      "N/A for a TEST-FILE target" in (row.get("message") or ""), True)

        gate = load("goc_tt", GATE)
        fn = getattr(gate, "_locked_test_target_fixture", None)
        check("gate has _locked_test_target_fixture", callable(fn), True)
        if callable(fn):
            check("gate: exact locked fixture -> True", fn(T / "locked"), True)
            check("gate: edited fixture -> False", fn(T / "spoof"), False)
            wt = build(T / "nontest", target="pricing.py")
            (wt / "test_fixture.py").write_text(oda.test_target_fixture("pricing.py"))
            check("gate: non-test target -> False", fn(wt), False)
            (T / "locked" / ".dispatch-harness.json").rename(T / "locked" / "m.json")
            check("gate: no manifest -> False", fn(T / "locked"), False)
            (T / "locked" / "m.json").rename(T / "locked" / ".dispatch-harness.json")
            payload = {}
            gate.measure_relevance(payload, T / "locked", "bash verify.sh")
            check("gate measure_relevance: not_applicable test-file-target",
                  (payload.get("verify_relevance") or {}).get("not_applicable"),
                  "test-file-target")

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
