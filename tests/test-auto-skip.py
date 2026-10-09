#!/usr/bin/env python3
"""Regression test: verified auto-skip from an escalation review (the owner, 2026-10-02).

A review VERDICT (a) / "already satisfied" is a CLAIM. It may retire a slice ONLY
after the deterministic proof at the chain tip (real git worktrees here, real
`bash verify.sh`):
  1. verify passes at tip AND fails with the target at chain base, tree clean
     -> --skip-slice launched with "auto: verify passed at <sha>", rows retired,
        ESCALATIONS row written [x];
  2. verify FAILS at tip -> no skip, reason recorded;
  3. model says (a) but the tip is unsatisfied (property never landed) -> no skip;
  4. leaky verify (passes at base too) -> no skip;
  5. dirty chain tree -> no skip;
  plus: VERDICT parsing, label -> plan/slice mapping, retire sweep leaves
  awaiting_signoff / pending rows alone, watcher routes source D, [x] row.
Run: python3 test-auto-skip.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEAL = Path(os.environ.get("HEAL_SRC") or HERE / "dispatch-self-heal.py")
WATCH = Path(os.environ.get("WATCH_SRC") or HERE / "dispatch-escalation-watcher.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def sh(cwd, *a):
    subprocess.run(list(a), cwd=str(cwd), check=True, capture_output=True)


VERIFY = """#!/bin/bash
grep -q PROPERTY_PRESENT {target} || {{ echo "FAIL: property missing"; exit 1; }}
echo VERIFY_OK
"""
LEAKY = "#!/bin/bash\necho VERIFY_OK\n"


def fixture(root, land_property=True, verify=VERIFY, dirty=False):
    """repo (base commit) + chain worktree (one commit on top) + slice worktree
    holding the frozen harness. Returns (runs_dir, plan, sid, chain)."""
    repo = root / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@t")
    sh(repo, "git", "config", "user.name", "t")
    (repo / "app.ts").write_text("export const x = 1;\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "base")
    chain = root / "chain"
    sh(repo, "git", "worktree", "add", "-q", "-b", "chain", str(chain))
    (chain / "app.ts").write_text("export const x = 1;\n" +
                                  ("// PROPERTY_PRESENT\n" if land_property else "// other s0 work\n"))
    sh(chain, "git", "commit", "-qam", "s0")
    if dirty:
        (chain / "stray.ts").write_text("uncommitted\n")
    swt = root / "slice-wt"
    swt.mkdir()
    (swt / "verify.sh").write_text(verify.format(target="app.ts"))
    (swt / "TASK.md").write_text("add PROPERTY_PRESENT\n")
    (swt / "refimpl.py").write_text("# must never be copied\n")
    runs = root / "runs"
    runs.mkdir()
    plan_file = root / "p.slices.json"
    plan_file.write_text("{}")
    st = {"chain_worktree": str(chain), "repo": str(repo), "plan_path": str(plan_file),
          "slices": {"s1-x": {"status": "escalated", "worktree": str(swt), "target": "app.ts"}}}
    (runs / "plan-a.json").write_text(json.dumps(st))
    return runs, "plan-a", "s1-x", chain


REVIEW_A = "blah\nVERDICT: (a) -- already satisfied at the chain tip\n"
REVIEW_B = "VERDICT: (b) -- spec under-specified\n"


def run_case(h, name, review, **fx):
    root = Path(tempfile.mkdtemp(prefix="autoskip-test-"))
    runs, plan, sid, chain = fixture(root, **fx)
    launched, resolved = [], []
    jobs = [{"id": "J1", "label": "auto-author-plan-a-s1-x-r2", "status": "needs_opus"},
            {"id": "J2", "label": "plan-a-s1-x", "status": "failed", "awaiting_signoff": True},
            {"id": "J3", "label": "plan-a-s1-x-c1", "status": "pending"},
            {"id": "J4", "label": "plan-b-s1-x", "status": "needs_opus"}]
    h.heal._launch = lambda pp, args, log: launched.append(args) or 0
    h.heal._resolve = lambda jid: resolved.append(jid) or True
    h.heal._jobs = jobs
    h.heal._review_path = "/r/review.md"
    out = h.heal(plan, sid, None, review, slice_runs=runs, ledger_path=root / "led.json")
    left = subprocess.run(["git", "-C", str(chain), "worktree", "list"],
                          capture_output=True, text=True).stdout.count("\n")
    return out, launched, resolved, left


def heal_tests(h):
    # ISOLATION (2026-10-05): the (b) case reaches _retry_with_notes, which writes a
    # review-notes file into ESC_DIR and a decisions.jsonl line -- this test leaked 39
    # "plan-a/s1-x retry+notes launched" lines into the LIVE decisions log (what
    # `qctl status` reads) and 5 notes files into the live escalations dir.
    _iso = Path(tempfile.mkdtemp(prefix="autoskip-iso-"))
    h.DECISIONS, h.ESC_DIR = _iso / "decisions.jsonl", _iso / "esc"
    check("VERDICT (a) parsed", h.review_says_satisfied(REVIEW_A), True)
    check("VERDICT: a -- parsed", h.review_says_satisfied("VERDICT: a -- done"), True)
    check("VERDICT (b) not a skip", h.review_says_satisfied(REVIEW_B), False)
    check("prose 'already satisfied' outside VERDICT ignored",
          h.review_says_satisfied("the model said already satisfied\nVERDICT: (c) harness"), False)
    check("no VERDICT line -> not a skip", h.review_says_satisfied("already satisfied"), False)
    check("label_base strips decorations", h.label_base("auto-refine-plan-a-s1-x-r3-c2"), "plan-a-s1-x")

    out, launched, resolved, wts = run_case(h, "pass", REVIEW_A)
    check("1 verify passes at tip, fails at base -> skip-verified", out.startswith("skip-verified:"), True)
    check("1 --skip-slice launched with auto reason naming the tip",
          bool(launched) and launched[0][:2] == ["--skip-slice", "s1-x"]
          and launched[0][3].startswith("auto: verify passed at ") and "/r/review.md" in launched[0][3], True)
    check("1 only the slice's needs_opus row retired (not signoff/pending/other plan)", resolved, ["J1"])
    check("1 temp tip worktree cleaned up", wts, 2)

    out, launched, resolved, _ = run_case(h, "tip-unsat", REVIEW_A, land_property=False)
    check("3 model says (a) but tip unsatisfied -> no skip", (out.startswith("skip:not verified"), launched, resolved),
          (True, [], []))

    out, launched, _, _ = run_case(h, "verify-fails", REVIEW_A,
                                   verify="#!/bin/bash\necho broken; exit 3\n")
    check("2 verify fails -> no skip", (out.startswith("skip:not verified"), launched), (True, []))

    out, launched, _, _ = run_case(h, "leaky", REVIEW_A, verify=LEAKY)
    check("4 leaky verify (passes at base) -> no skip", (out.startswith("skip:not verified"), "discriminate" in out, launched),
          (True, True, []))

    out, launched, _, _ = run_case(h, "dirty", REVIEW_A, dirty=True)
    check("5 dirty chain tree -> no skip", ("DIRTY" in out, launched), (True, []))

    out, launched, _, _ = run_case(h, "b", REVIEW_B)
    check("non-(a) verdict never reaches the skip path", out.startswith("skip-verified") or bool(launched), False)

    # tip proof by EXTRACTION (git archive), never a registered worktree (2026-10-02:
    # registering one blocked 106s on iCloud-evicted .git metadata -> Rivian s1 timeout)
    root = Path(tempfile.mkdtemp(prefix="autoskip-x-"))
    runs, plan, sid, chain = fixture(root)
    st = json.loads((runs / "plan-a.json").read_text())
    s1 = st["slices"]["s1-x"]
    (Path(s1["worktree"]) / "tsconfig.tsbuildinfo").write_text("CACHE")
    gits, seen = [], []
    real_git = h._git
    h._git = lambda cwd, *a, timeout=60: (gits.append(a) or real_git(cwd, *a, timeout=timeout))

    def spy(tree):
        seen.append(((tree / "tsconfig.tsbuildinfo").exists(),
                     "PROPERTY_PRESENT" in (tree / "app.ts").read_text()))
        return h._run_verify(tree)
    try:
        ok, det, _tip = h.verify_satisfied_at_tip(st, s1, runner=spy)
    finally:
        h._git = real_git
    check("x tip proof passes on the extracted tree", ok, True)
    check("x the proof never registers a worktree",
          [a for a in gits if a and a[0] == "worktree"], [])
    check("x the incremental tsc cache is carried into the tip tree", [v[0] for v in seen], [True, True])
    check("x tip run sees the landed property, base run sees it reverted",
          [v[1] for v in seen], [True, False])
    # a target that did NOT exist at the chain base is removed for the base run
    root = Path(tempfile.mkdtemp(prefix="autoskip-n-"))
    runs, plan, sid, chain = fixture(root)
    (chain / "new.ts").write_text("// PROPERTY_PRESENT\n")
    sh(chain, "git", "add", "new.ts")
    sh(chain, "git", "commit", "-qm", "new file")
    st = json.loads((runs / "plan-a.json").read_text())
    s1 = st["slices"]["s1-x"]
    s1["target"] = "new.ts"
    (Path(s1["worktree"]) / "verify.sh").write_text(VERIFY.format(target="new.ts"))
    ok, det, _tip = h.verify_satisfied_at_tip(st, s1)
    check("n target absent at base -> removed for the base run -> skip proven", ok, True)

    # retire sweep: rows of a slice that is now DONE are superseded
    root = Path(tempfile.mkdtemp())
    (root / "plan-a.json").write_text(json.dumps({"slices": {"s1-x": {"status": "done"},
                                                             "s2-y": {"status": "escalated"}}}))
    res = []
    jobs = [{"id": "A", "label": "auto-author-plan-a-s1-x-r1", "status": "needs_opus"},
            {"id": "B", "label": "auto-author-plan-a-s2-y", "status": "needs_opus"},
            {"id": "C", "label": "plan-a-s1-x", "status": "done"},
            {"id": "D", "label": "plan-a-s1-x-r2", "status": "failed", "awaiting_signoff": True}]
    h.retire_sweep(slice_runs=root, jobs=jobs, resolve=lambda j: res.append(j) or True)
    check("retire sweep: only done-slice needs_opus row resolved", res, ["A"])
    check("label -> plan/slice", h.plan_slice_for_label("auto-refine-plan-a-s2-y-r4", root), ("plan-a", "s2-y"))


def watcher_tests(w):
    seen = []

    class R:
        returncode = 0
        stdout = "skip-verified:abc123\n"
        stderr = ""

    fake = lambda cmd, **k: seen.append(cmd) or R()
    out = w.self_heal({"source": "D", "kind": "job", "label": "auto-author-plan-a-s1-x"}, "/c", "/r",
                      run=fake, tool=__file__)
    check("watcher routes source D through --job-label", (out, "--job-label" in seen[-1]), ("skip-verified:abc123", True))
    idx = Path(tempfile.mkdtemp()) / "E.md"
    w.append_index({"plan": "p", "slice_id": "s", "source": "A"}, "/c.md", "VERDICT: a [self-heal: skip-verified:abc]",
                   idx, checked=True)
    w.append_index({"plan": "p", "slice_id": "s", "source": "A"}, "/c.md", "VERDICT: b", idx)
    rows = idx.read_text().splitlines()
    check("verified skip writes a [x] row; others stay open", (rows[0][:5], rows[1][:5]), ("- [x]", "- [ ]"))
    check("open_escalations ignores the [x] row", len(w.open_escalations(idx)), 1)
    check("skip-verified heal -> row checked", w.heal_retired_it("skip-verified:abc"), True)
    check("refused skip -> row stays open", w.heal_retired_it("skip:not verified (x)"), False)


def main():
    heal_tests(load(HEAL, "heal_t"))
    watcher_tests(load(WATCH, "watch_t"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("HEAL_SRC", "skip on the model's word (no proof)",
     "    ok, detail, tip = (verifier or verify_satisfied_at_tip)(st, s)\n",
     "    ok, detail, tip = True, 'trusted', 'deadbeef'\n"),
    ("HEAL_SRC", "no discrimination check at base",
     "        if ok2:\n            return False, (\"the verify ALSO",
     "        if False:\n            return False, (\"the verify ALSO"),
    ("HEAL_SRC", "dirty tree not checked",
     "    if dirty:\n        return False, \"chain worktree is DIRTY",
     "    if False:\n        return False, \"chain worktree is DIRTY"),
    ("HEAL_SRC", "retire ignores awaiting_signoff",
     "        if j.get(\"awaiting_signoff\"):\n            continue\n", ""),
    ("HEAL_SRC", "any VERDICT line counts",
     "            return bool(_SATISFIED_RE.search(line))", "            return True"),
    ("HEAL_SRC", "tip tree via a registered worktree again",
     "        ok_x, why_x = _extract_tree(chain, tip, tree)\n",
     "        tree.rmdir(); _git(chain, 'worktree', 'add', '--detach', str(tree), tip, timeout=120); ok_x, why_x = True, ''\n"),
    ("HEAL_SRC", "tsc cache not carried",
     '    for cache in ("tsconfig.tsbuildinfo",):', '    for cache in ():'),
    ("HEAL_SRC", "absent-at-base target left in place",
     "        try:\n            Path(dest).unlink()\n        except FileNotFoundError:\n            pass\n        return True, \"\"",
     "        return True, \"\""),
    ("WATCH_SRC", "verified skip row left open",
     "    return str(heal or \"\").startswith(\"skip-verified:\")",
     "    return False"),
    ("WATCH_SRC", "source D not routed",
     "    elif e.get(\"source\") == \"D\" and e.get(\"label\"):",
     "    elif False:"),
]


def revert_check():
    bad = 0
    srcs = {"HEAL_SRC": HEAL, "WATCH_SRC": WATCH}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
