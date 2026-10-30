#!/usr/bin/env python3
"""Both-ways proof for `ollama-dispatch-scaffold --kind expression` (gap #1).

The expression harness does not import a symbol; it reads the target as TEXT,
finds a unique anchor, captures the enclosing `{ ... }` and evals it via
`new Function`. So the things that can go wrong are specific to it, and this
proves each one on the GENERATED fixture (run under node, no tsc/repo needed):

  * extraction: a unique anchor yields the right expression body
  * baseline RED / fixed GREEN: authored cases discriminate the make-aware fix
  * a BENIGN edit (touching the line but not the property) does NOT pass
  * a MISSING anchor is a hard FAIL (never a silent skip)
  * a NON-UNIQUE anchor is a hard FAIL (ambiguous extraction)

Run: test-scaffold-expression.py [-v]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
SCAFFOLD = BIN / "ollama-dispatch-scaffold"

# A target whose changed line carries TWO brace groups, like the real exemplar.
TARGET_BASELINE = """\
export function Caption(v, canEditLimit) {
  return (
    <span>
      {v.ctrl === 'full' ? 'CHARGE LIMIT' : 'CHARGE LIMIT VIA SCHEDULE'} - {canEditLimit ? 'TAP DIAL TO SET' : 'SET VIA RIVIAN APP'} - SOURCE
    </span>
  );
}
"""
# The make-aware fix: the else-branch becomes make-aware.
FIX_OLD = "{canEditLimit ? 'TAP DIAL TO SET' : 'SET VIA RIVIAN APP'}"
FIX_NEW = ("{canEditLimit ? 'TAP DIAL TO SET' : "
           "(v.ctrl === 'full' ? 'SET IN TESLA APP' : 'SET VIA RIVIAN APP')}")
# A BENIGN edit: rewrites the line (touches it) but does not make it make-aware.
# A relevant verify must still fail on this.
BENIGN_NEW = "{canEditLimit ? 'TAP DIAL TO SET' : 'SET VIA RIVIAN APP' /* x */}"

CASES = """
chk('tesla non-interactive -> tesla app',
    evalExpr({ ctrl: 'full' }, false) === 'SET IN TESLA APP');
chk('rivian non-interactive -> rivian app (over-trigger guard)',
    evalExpr({ ctrl: 'schedule' }, false) === 'SET VIA RIVIAN APP');
chk('interactive -> dial prompt',
    evalExpr({ ctrl: 'full' }, true) === 'TAP DIAL TO SET');
"""


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def scaffold_expr(repo: Path, dest: Path, anchor: str):
    return run([sys.executable, str(SCAFFOLD), "--label", "exprtest",
                "--repo", str(repo), "--lang", "ts", "--kind", "expression",
                "--target", "cap.tsx", "--anchor", anchor,
                "--dest", str(dest), "--force"])


def author(fixture: Path, params="['v', 'canEditLimit']", cases=CASES):
    t = fixture.read_text()
    t = t.replace("const PARAMS = [/* e.g. 'v', 'canEditLimit' */];",
                  f"const PARAMS = {params};")
    t = t.replace("const CASES_AUTHORED = false;",
                  cases + "\nconst CASES_AUTHORED = true;")
    fixture.write_text(t)


def node(fixture: Path):
    return run(["node", str(fixture)], cwd=fixture.parent)


def make_repo(tmp: Path, body: str) -> Path:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    (repo / "cap.tsx").write_text(body)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "t@t"], cwd=repo)
    run(["git", "config", "user.name", "t"], cwd=repo)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-qm", "baseline"], cwd=repo)
    return repo


def case_extraction_and_both_ways(tmp):
    repo = make_repo(tmp / "a", TARGET_BASELINE)
    wt = tmp / "a" / "wt"
    r = scaffold_expr(repo, wt, "TAP DIAL TO SET")
    if r.returncode != 0:
        return False, f"scaffold failed: {r.stderr.strip()[-200:]}"
    fx = wt / "verify_impl.mjs"
    # unauthored -> SCAFFOLD_INCOMPLETE, and it prints the extracted expression
    r0 = node(fx)
    extracted_ok = ("SET VIA RIVIAN APP" in r0.stdout
                    and "canEditLimit" in r0.stdout and r0.returncode == 1)
    author(fx)
    # baseline: RED (fix not applied)
    rb = node(fx)
    red = rb.returncode != 0 and "FAIL - tesla non-interactive" in rb.stdout
    # apply the fix, GREEN
    tgt = wt / "cap.tsx"
    tgt.write_text(tgt.read_text().replace(FIX_OLD, FIX_NEW, 1))
    rg = node(fx)
    green = rg.returncode == 0 and "0 failed" in rg.stdout
    return (extracted_ok and red and green,
            f"extracted={extracted_ok} baseline_red={red} fixed_green={green}")


def case_benign_does_not_pass(tmp):
    repo = make_repo(tmp / "b", TARGET_BASELINE)
    wt = tmp / "b" / "wt"
    scaffold_expr(repo, wt, "TAP DIAL TO SET")
    fx = wt / "verify_impl.mjs"
    author(fx)
    tgt = wt / "cap.tsx"
    tgt.write_text(tgt.read_text().replace(FIX_OLD, BENIGN_NEW, 1))
    r = node(fx)
    # a benign edit leaves the property broken -> the verify MUST stay red
    return r.returncode != 0, f"benign_still_red={r.returncode != 0}"


def case_missing_anchor_fails(tmp):
    repo = make_repo(tmp / "c", TARGET_BASELINE)
    wt = tmp / "c" / "wt"
    scaffold_expr(repo, wt, "TAP DIAL TO SET")
    fx = wt / "verify_impl.mjs"
    author(fx)
    # move the anchor out of the target: extraction must FAIL, not skip
    tgt = wt / "cap.tsx"
    tgt.write_text(tgt.read_text().replace("TAP DIAL TO SET", "PUSH THE BUTTON"))
    r = node(fx)
    ok = r.returncode != 0 and "extraction" in r.stdout and "not found" in r.stdout
    return ok, f"rc={r.returncode} said_extraction={'extraction' in r.stdout}"


def case_nonunique_anchor_fails(tmp):
    # anchor appears twice -> ambiguous -> the fixture must refuse to guess.
    body = TARGET_BASELINE.replace("SOURCE", "SET VIA RIVIAN APP SOURCE")
    repo = make_repo(tmp / "d", body)
    wt = tmp / "d" / "wt"
    scaffold_expr(repo, wt, "SET VIA RIVIAN APP")
    fx = wt / "verify_impl.mjs"
    author(fx)
    r = node(fx)
    ok = r.returncode != 0 and "not unique" in r.stdout
    return ok, f"rc={r.returncode} said_nonunique={'not unique' in r.stdout}"


CASES_LIST = [
    ("extraction-and-both-ways", case_extraction_and_both_ways),
    ("benign-does-not-pass",     case_benign_does_not_pass),
    ("missing-anchor-fails",     case_missing_anchor_fails),
    ("nonunique-anchor-fails",   case_nonunique_anchor_fails),
]


def main():
    verbose = "-v" in sys.argv
    if not run(["node", "--version"]).returncode == 0:
        print("node not on PATH -- cannot run the expression-mode tests")
        return 2
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, (name, fn) in enumerate(CASES_LIST):
            ok, detail = fn(tmp / str(i))
            print(f"  {'ok  ' if ok else 'FAIL'} {name:<26} {detail}")
            if not ok:
                fails += 1
    print(f"\n--- {fails} of {len(CASES_LIST)} failed ---")
    if fails == 0:
        print("EXPRESSION_SCAFFOLD_OK: extraction, both-ways, benign-reject, "
              "and anchor guards all hold")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
