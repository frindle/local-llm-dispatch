#!/usr/bin/env python3
"""`ollama-dispatch-slice <label> --cancel` accepts a BARE plan label (2026-10-08 dashboard
triage: a label gave a FileNotFoundError traceback; only a plan-file path worked).

End-to-end through the REAL slicer CLI with HOME pointed at a temp dir (the plan store, the
cancel marker and the handoff-emit stub are all sandboxed -- nothing real is cancelled).

Usage:  python3 test-slice-plan-label.py                 -> ALL PASS
        SLICE=<mutant> python3 test-slice-plan-label.py  -> FAILs
        python3 test-slice-plan-label.py --revert-check  -> each built-in mutant must FAIL
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE") or HERE / "ollama-dispatch-slice").resolve()
fails = []


def chk(name, ok, detail=""):
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  ({detail})"))
    if not ok:
        fails.append(name)


def run():
    label = "zz-label-t"
    with tempfile.TemporaryDirectory() as home:
        home = str(Path(home).resolve())
        hb = Path(home) / "bin"; hb.mkdir()
        (hb / "handoff-emit.py").write_text("#!/usr/bin/env python3\nimport sys\nif '--json' in sys.argv: print('{}')\n")
        store = Path(home) / ".ollama-dispatch" / "slice-plans"; store.mkdir(parents=True)
        plan = {"label": label, "repo": str(Path(home) / "repo"), "target": "lib/x.ts", "lang": "ts",
                "intent": "t", "slices": [{"id": "s1-a", "title": "a", "intent": "a",
                                           "depends_on": [], "must_contain": []}]}
        (store / f"{label}.slices.json").write_text(json.dumps(plan))
        env = {**os.environ, "HOME": home, "DISPATCH_VERIFY_SANDBOX": "1", "PYTHONPATH": str(SLICE.parent)}
        marker = Path(home) / ".ollama-dispatch" / "slice-runs" / f"{label}.cancelled"

        def go(arg, cwd=home):
            return subprocess.run([sys.executable, str(SLICE), arg, "--cancel", "--reason", "t"],
                                  capture_output=True, text=True, env=env, timeout=300, cwd=cwd)
        cp = go(label)
        chk("bare label resolves to slice-plans/<label>.slices.json and cancels (rc 0)",
            cp.returncode == 0 and marker.exists(), (cp.returncode, (cp.stdout + cp.stderr)[-400:]))
        chk("no traceback for a bare label", "Traceback" not in cp.stderr, cp.stderr[-300:])
        marker.unlink(missing_ok=True)
        cp = go(str(store / f"{label}.slices.json"))
        chk("an explicit plan-file PATH still works", cp.returncode == 0 and marker.exists(), cp.stderr[-300:])
        marker.unlink(missing_ok=True)
        cp = go("zz-nope")
        err = cp.stderr
        chk("unknown label: nonzero exit", cp.returncode != 0, cp.returncode)
        chk("unknown label: clear error naming BOTH candidates, no traceback",
            "Traceback" not in err and str(store / "zz-nope.slices.json") in err
            and os.path.join(home, "zz-nope.slices.json") in err, err[-500:])
        chk("unknown label: nothing cancelled", not (marker.parent / "zz-nope.cancelled").exists())
        cp = go("sub/dir/missing.json")
        chk("a missing PATH is a clean error too (no traceback)",
            cp.returncode != 0 and "Traceback" not in cp.stderr and "missing.json" in cp.stderr, cp.stderr[-300:])
    return fails


MUT = [("no label resolution", "        a.plan = resolve_plan_arg(a.plan)", "        pass"),
       ("candidate store dir", '"~/.ollama-dispatch/slice-plans"', '"~/.ollama-dispatch/slice-planz"'),
       ("error names one candidate only", "neither {cands[0]} nor {cands[1]} exists", "neither {cands[0]} exists")]

if __name__ == "__main__":
    if "--revert-check" in sys.argv:
        src = SLICE.read_text(); bad = 0
        for n, o, nw in MUT:
            assert src.count(o) == 1, f"anchor missing: {n}"
            with tempfile.NamedTemporaryFile("w", suffix="-slice", dir=HERE, delete=False) as f:
                f.write(src.replace(o, nw))
            r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE": f.name},
                               capture_output=True, text=True, timeout=900)
            os.unlink(f.name)
            print(("bites" if r.returncode else "INERT") + f": revert '{n}'")
            bad += 0 if r.returncode else 1
        print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
        sys.exit(1 if bad else 0)
    run()
    print("\nALL PASS" if not fails else f"\n{len(fails)} FAIL")
    sys.exit(1 if fails else 0)
