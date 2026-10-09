#!/usr/bin/env python3
"""Guards 2026-10-02 idle-test-tsslug (62dcd980447d, -c1 f5144683e2a0, -c2 7d6055130d95).

A fresh --lang ts project has no package.json. verify's env bootstrap ran
`npm install` (rc 254, but it leaves an empty package-lock.json), then `npm ci`
on every later run (rc 254): one permanent verify failure no harness edit could
clear. The self-check's 1500-char tail cut the FAIL line off, so neither the model
nor the continuation chain could see it, and two more full GPU runs burned.

Pins: (a) scaffold TS bootstrap skips npm when there is no package.json;
(b) auto-harness-check carries env-bootstrap FAIL lines past the tail truncation;
(c) harness_env_failure detects them; (d) the continuation chain does NOT run
another round on one, routes it to die/park (not the slicer), and the
escalation round is skipped.

Run: python3 test-auto-harness-env-park.py [--revert-check]
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parent
SCAF = Path(os.environ.get("SCAF_SRC") or BIN / "ollama-dispatch-scaffold")
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


def bootstrap_case(scaf):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fb = td / "fakebin"
        fb.mkdir()
        log = td / "npm-calls"
        for tool in ("npm", "npx"):
            (fb / tool).write_text(f"#!/bin/sh\necho {tool} \"$@\" >> {log}\nexit 254\n")
            (fb / tool).chmod(0o755)
        proj = td / "p"
        proj.mkdir()
        script = "fails=0\n" + scaf.TS_BOOTSTRAP + "\necho FAILS=$fails\n"
        r = subprocess.run(["bash", "-c", script], cwd=proj, capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{fb}:{os.environ['PATH']}"}, timeout=60)
        check("TS bootstrap without package.json: npm never invoked", log.exists(), False)
        check("TS bootstrap without package.json: no failure counted", "FAILS=0" in r.stdout, True)
        (proj / "package.json").write_text("{}")
        r = subprocess.run(["bash", "-c", script], cwd=proj, capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{fb}:{os.environ['PATH']}"}, timeout=60)
        check("TS bootstrap WITH package.json still installs (control)", log.exists(), True)


def selfcheck_case(auto):
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        filler = "\\n".join(f"line {i} of noisy verify output" for i in range(200))
        (wt / "verify.sh").write_text(
            "echo '=== env bootstrap ==='\necho '  FAIL: npm ci failed'\n"
            f"printf '{filler}\\n'\necho '=== spec literals ==='\necho '--- 1 failed ---'\nexit 1\n")
        (wt / "refimpl.py").write_text("print('noop')\n")
        (wt / "TASK.md").write_text("# T\n## Must contain\n- `x`\nOnly edit `t.py`\n")
        (wt / "test_fixture.py").write_text("")
        (wt / "t.py").write_text("x = 1\n")
        g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True)
        g("init", "-q")
        g("add", "t.py")
        g("-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "b")
        auto.write_harness_check(wt, "python")
        r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt, capture_output=True,
                           text=True, timeout=120)
        out = r.stdout + r.stderr
        check("self-check tail keeps the env-bootstrap FAIL line past truncation",
              auto.harness_env_failure(out), "FAIL: npm ci failed")


def chain_case(auto):
    env_check = "=== env bootstrap ===\nFAIL: npm ci failed\n=== spec literals ===\n  ok"
    lit_check = "=== env bootstrap ===\n  ok: npm ci\n=== spec literals ===\n  FAIL: a Must-contain literal is missing"
    check("detector: env FAIL found", auto.harness_env_failure(env_check), "FAIL: npm ci failed")
    check("detector: FAIL outside env section ignored", auto.harness_env_failure(lit_check), None)
    check("detector: no section -> None", auto.harness_env_failure("FAIL: whatever"), None)
    calls = []
    auto.dispatch_model = lambda *a, **k: (calls.append(a[2]) or (False, "did not converge"))
    auto._harness_check_output = lambda wt, v, t, b=None: (env_check, None)
    auto.record_attempt = lambda *a, **k: None
    auto.chain_state_write = lambda *a, **k: None
    auto._harness_signature = lambda *a, **k: "sig"
    a = SimpleNamespace(intent="x", interface="", lang="ts", label="L", author_continue_rounds=2,
                        author_max_iters=24, drafter_cmd=None)
    auto.load_attempts = lambda a, runs_dir=None: []
    ok, why = auto._author_with_continuations(a, Path("/nonexistent"), "src/x.ts", "v")
    check("chain: no continuation round dispatched on an env failure", calls, ["auto-author-L"])
    check("chain: reason carries the HARNESS-ENV tag", why.startswith(auto.HARNESS_ENV_PREFIX), True)
    check("route: HARNESS-ENV goes to die/park, not the slicer", auto._failure_route(why), "die")
    check("route: plain failure still auto-slices (control)", auto._failure_route("did not converge"), "autoslice")
    calls.clear()
    ok2, why2 = auto._author_escalate(a, Path("/nonexistent"), "src/x.ts", "v", why)
    check("escalation round skipped for HARNESS-ENV", (ok2, calls), (False, []))
    check("classifier: harness-env class", auto.classify_author_failure(why, env_check), "harness-env")
    # control: a non-env failure still gets its continuation round
    calls.clear()
    auto._harness_check_output = lambda wt, v, t, b=None: (lit_check, None)
    auto._author_with_continuations(a, Path("/nonexistent"), "src/x.ts", "v")
    check("chain control: a literal failure still gets continuation round(s)",
          "auto-author-L-c1" in calls, True)


def main():
    scaf = load(SCAF, "scaf_env")
    auto = load(AUTO, "auto_env")
    bootstrap_case(scaf)
    selfcheck_case(auto)
    chain_case(auto)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def mutate(src_path, old, new):
    s = Path(src_path).read_text()
    assert s.count(old) == 1, f"anchor missing/ambiguous: {old[:60]!r}"
    f = tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False)
    f.write(s.replace(old, new))
    f.close()
    return f.name


MUTANTS = [
    ("SCAF_SRC", SCAF, "bootstrap no-package.json guard removed",
     'if [ ! -f package.json ]; then\n  echo "  ok: no package.json -- no npm deps to install (tsx/tsc from PATH)"\nelif',
     "if"),
    ("AUTO_SRC", AUTO, "tail no longer carries env FAIL lines", "    if _envf:\n        tail = (", "    if False:\n        tail = ("),
    ("AUTO_SRC", AUTO, "continuation ignores env failure", "        if _env:\n            return False, (",
     "        if False:\n            return False, ("),
    ("AUTO_SRC", AUTO, "harness-env routed to the slicer",
     "startswith((CONTAMINATED_PREFIX, HARNESS_ENV_PREFIX))\n            else",
     "startswith((CONTAMINATED_PREFIX,))\n            else"),
]


def revert_check():
    bad = 0
    for var, path, name, old, new in MUTANTS:
        mp = mutate(path, old, new)
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: mp},
                           capture_output=True, text=True, timeout=300)
        os.unlink(mp)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
