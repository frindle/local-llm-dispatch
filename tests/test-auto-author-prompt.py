#!/usr/bin/env python3
"""The AUTO authoring prompt must not hand the model an unfollowable loop (2026-10-02).

Live: bfmr-superseded-reservations-v2 auto-author job bc15112d643a hit
output_cap_loop after 14 iterations. Three prompt or harness defects, each pinned here:
  1. "Done when" never named the self-check command. The model ran `bash verify.sh`
     bare, got "no frozen literals -- run: ollama-dispatch-scaffold --freeze-literals",
     a step it cannot take (and check_literals.py is not its to edit), and looped.
     The prompt now names `python3 auto-harness-check.py`, which freezes the
     literals itself. In an auto worktree check_literals.py now points there too.
  2. The prompt forced a NextRequest INTEGRATION fixture onto a PURE lib function
     because the intent said "route wiring is a later slice". A PURE intent is never
     web, and a route-shaped TARGET still is.
  3. The fixture sits at the worktree root, but the model copied the repo test's
     `./bfmrVanished.ts` import and got MODULE_NOT_FOUND. The prompt now gives the
     root-relative path.
--revert-check mutates each fix. AUTO_SRC / SCAFFOLD_SRC override the files under test."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
SCAF = Path(os.environ.get("SCAFFOLD_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def main():
    m = load(AUTO, "auto_ap")
    m.load_attempts = lambda a: []
    tmp = Path(tempfile.mkdtemp(prefix="aap-"))

    def prompt(intent, target, lang="typescript", interface=""):
        a = SimpleNamespace(intent=intent, interface=interface, lang=lang, label="x",
                            repo=str(tmp), new_project=None, dest=None, target=target)
        return m.author_prompt(a, target)

    pure = prompt("Add an exported PURE function f(x) to lib/x.ts. No live calls; "
                  "route wiring is a later slice.", "lib/x.ts")
    chk("PURE intent mentioning 'route' -> NOT forced into a NextRequest integration test",
        "MUST be an INTEGRATION" in pure, False)
    route = prompt("Add a POST handler that returns 400 on bad input", "app/api/x/route.ts")
    chk("route-shaped target -> integration fixture still required", "MUST be an INTEGRATION" in route, True)
    pure_route = prompt("Make the PURE helper used by this route reject empty input",
                        "app/api/x/route.ts")
    chk("route TARGET wins even when the intent says pure", "MUST be an INTEGRATION" in pure_route, True)
    chk("TS prompt gives the ROOT-relative import of the target, with extension",
        "`from './lib/x.ts'`" in pure, True)
    py = prompt("Add a pure function f", "app/x.py", lang="python")
    chk("python prompt carries no TS import rule", "from './" in py, False)
    chk("'Done when' names the self-check command", "python3 auto-harness-check.py" in pure, True)
    chk("...and says not to run verify.sh bare", "NOT `bash verify.sh` on its own" in pure, True)

    # check_literals.py with no frozen literals, in an AUTO worktree vs a hand one
    sc = load(SCAF, "scaf_ap")
    for auto, want in ((True, "python3 auto-harness-check.py"), (False, "--freeze-literals")):
        d = Path(tempfile.mkdtemp(prefix="cl-", dir=tmp))
        (d / "check_literals.py").write_text(sc.CHECK_LITERALS.format(target="lib/x.ts"))
        (d / "TASK.md").write_text("# T\n\n## Must contain\n\n- `f`\n")
        if auto:
            (d / "auto-harness-check.py").write_text("# stub\n")
        r = subprocess.run([sys.executable, "check_literals.py"], cwd=d, capture_output=True, text=True)
        chk(f"empty LITERALS ({'auto' if auto else 'hand'} worktree) still FAILS", r.returncode != 0, True)
        chk(f"empty LITERALS ({'auto' if auto else 'hand'} worktree) names the step THIS author can take",
            want in r.stdout, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    (AUTO, "AUTO_SRC", "pure not exempt", '''    if re.search(r"\\bpure\\b", a.intent + " " + interface, re.I):
        web = False
''', ""),
    (AUTO, "AUTO_SRC", "route target not forced", '''    if re.search(r"(^|/)(app|pages)/api/|(^|/)route\\.[cm]?[jt]sx?$", target or ""):
        web = True
''', ""),
    (AUTO, "AUTO_SRC", "no import rule", "BENIGN or empty implementation certifies nothing.{integ}{import_rule}",
     "BENIGN or empty implementation certifies nothing.{integ}"),
    (AUTO, "AUTO_SRC", "self-check unnamed", "`python3 refimpl.py`. The self-check enforces exactly this: run\n`python3 auto-harness-check.py`",
     "`python3 refimpl.py`. The self-check enforces exactly this: run\n`it`"),
    (SCAF, "SCAFFOLD_SRC", "literal msg not auto-aware", '    if pathlib.Path("auto-harness-check.py").is_file():\n        # an AUTO',
     '    if False:\n        # an AUTO'),
]


def revert_check():
    bad = 0
    for path, env, name, old, new in MUTANTS:
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-" + path.name, delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
