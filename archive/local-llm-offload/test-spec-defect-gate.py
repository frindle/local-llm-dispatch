#!/usr/bin/env python3
"""Regression: the PRE-DISPATCH CONTRADICTION GATE and its siblings (2026-10-09).

ROOT CAUSE (smoke-order-mismatch, bundle smoke-order-mismatch, base 5398b72): the task's
INTENT said `create lib/payoutMismatch.ts` but that file ALREADY EXISTED at the base commit
(in a later-evolved form). No refimpl can satisfy "create X" over a real X; the author model
spiralled (~120K-char turns) and ~3 authoring jobs burned GPU. Nothing before dispatch looked.

  A. pure gate (dispatch_harness_gates): creation target exists / stub exempt / explicit
     modify exempt / already-satisfied needs a GREEN verify / dead refimpl anchor read from the
     BASE commit (not the working copy).
  B. EXACT REPLAY of the order-mismatch situation: TASK.md, refimpl.py, manifest and the base
     blobs are copied from test-fixtures/order-mismatch (copied read-only from the live worktree) into a temp repo; the gate, preflight
     (real CLI) and auto's pre-enqueue precheck all refuse with SPEC_DEFECT and execute nothing.
  C. baseline-red classification: assertion = red; crash/import/syntax alone = NOT red;
     creation / spec-named missing symbol = expected red; unreadable output = accepted.
  D. identical-failure signature + 3-strikes state machine.
  F. deterministic base resolution (explicit --baseline-ref = no fetch; default fetch under a lock;
     3 concurrent resolutions agree), auto --baseline-ref passthrough, base_sha in the manifest +
     BASE_DRIFT gate, baseline probe.
  E. the REAL auto _preflight_loop (stubbed preflight/dispatch): the same failure 3 rounds running
     is abandoned even though the model keeps editing; round 2 gets the signature fed back; a
     spec-defect blocker stops the loop with no refine dispatch.

Run: python3 test-spec-defect-gate.py [--bin DIR]      (prints ALL PASS)
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
if "--bin" in sys.argv:
    HERE = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
sp = importlib.util.spec_from_file_location("dispatch_harness_gates", HERE / "dispatch_harness_gates.py")
hg = importlib.util.module_from_spec(sp)
sp.loader.exec_module(hg)
PRE = HERE / "ollama-dispatch-preflight"
AUTO = HERE / "ollama-dispatch-auto"
FIXT = HERE / "test-fixtures" / "order-mismatch"   # copied read-only from the live replay worktree
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def make_repo(root, name, files):
    repo = root / f"repo-{name}"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    for rel, txt in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(txt)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def codes(fs):
    return sorted(f["code"] for f in fs)


REAL_IMPL = ("export function payoutMismatch(o: any): boolean {\n"
             "  if (o.salePrice == null) return false;\n"
             "  return Math.abs(o.salePrice - (o.expected ?? 0)) > 0.01;\n}\n")


def part_a(root):
    repo = make_repo(root, "a", {"lib/x.ts": REAL_IMPL, "lib/stub.ts": "// Stub for lib/stub.ts -- implement per TASK.md.\nexport {};\n"})
    # creation target exists as a real implementation -> defect
    f = hg.creation_target_findings(repo, "lib/x.ts", True, "create lib/x.ts exporting payoutMismatch")
    check("A creation target that exists at base = CREATION_TARGET_EXISTS", codes(f), ["CREATION_TARGET_EXISTS"])
    check("A ... structured reason + path", (f[0]["reason"], f[0]["path"]), ("creation target exists at base", "lib/x.ts"))
    check("A ... SPEC_DEFECT line", hg.format_spec_defects(f).startswith("SPEC_DEFECT: creation target exists at base: lib/x.ts"), True)
    # a seeded stub is a baseline, not a defect
    check("A a stub at base is NOT a defect",
          hg.creation_target_findings(repo, "lib/stub.ts", True, "create lib/stub.ts exporting f"), [])
    # a path absent at base is fine
    check("A a missing creation target is fine",
          hg.creation_target_findings(repo, "lib/new.ts", True, "create lib/new.ts exporting f"), [])
    # explicit modify/extend exempts
    for txt in ("modify lib/x.ts so it also exports foo", "extend the existing lib/x.ts with foo",
                "lib/x.ts already exists; update it to add foo"):
        check(f"A explicit modify wording exempts: {txt[:30]!r}",
              hg.creation_target_findings(repo, "lib/x.ts", True, txt), [])
    # an edit task (not creation, intent does not say create) is never a creation defect
    check("A edit task over an existing file is fine",
          hg.creation_target_findings(repo, "lib/x.ts", False, "make payoutMismatch use cents"), [])
    # a path the intent says to create, even when --target differs
    f = hg.creation_target_findings(repo, "app/page.tsx", False, "create lib/x.ts and wire it into app/page.tsx")
    check("A intent-named created path is checked even when target differs", [x["path"] for x in f], ["lib/x.ts"])
    check("A `create a test for lib/x.ts` does not name x.ts as created",
          hg.creation_target_findings(repo, "app/page.tsx", False, "create a test for lib/x.ts"), [])
    # reads the BASE commit, not the working copy
    (repo / "lib/x.ts").write_text("// Stub for lib/x.ts -- implement per TASK.md.\nexport {};\n")
    check("A the check reads the BASE commit, not the working copy",
          codes(hg.creation_target_findings(repo, "lib/x.ts", True, "create lib/x.ts")), ["CREATION_TARGET_EXISTS"])
    git(repo, "checkout", "--", "lib/x.ts")

    # already satisfied: needs ALL literals present AND a green verify
    task = "## Required change\n\nadd it\n\n## Must contain\n\n- `export function payoutMismatch(`\n- `> 0.01`\n\n## Scope\n"
    check("A literals all present but verify NOT measured green = no finding",
          hg.already_satisfied_findings(repo, task, "lib/x.ts", None), [])
    check("A literals all present but verify red = no finding",
          hg.already_satisfied_findings(repo, task, "lib/x.ts", False), [])
    f = hg.already_satisfied_findings(repo, task, "lib/x.ts", True)
    check("A all literals present + verify green = ALREADY_SATISFIED", codes(f), ["ALREADY_SATISFIED"])
    check("A ... reason", f[0]["reason"], "already satisfied at base")
    task2 = task.replace("`> 0.01`", "`NOT_IN_BASE_TOKEN`")
    check("A one literal absent at base = not already satisfied",
          hg.already_satisfied_findings(repo, task2, "lib/x.ts", True), [])

    # refimpl anchor vs BASE content
    ref = 'OLD = """function nothingLikeThis() {}"""\n'
    f = hg.refimpl_base_anchor_findings(repo, "lib/x.ts", ref)
    check("A refimpl OLD anchor matching nothing at base = REFIMPL_ANCHOR_UNMATCHED", codes(f), ["REFIMPL_ANCHOR_UNMATCHED"])
    ok_ref = 'OLD = """export function payoutMismatch(o: any): boolean {"""\n'
    check("A a matching anchor is fine", hg.refimpl_base_anchor_findings(repo, "lib/x.ts", ok_ref), [])

    # unsealed creation stub: target absent at HEAD, pristine stub in the worktree (job 5be45b107cea)
    import tempfile as _tf
    r2 = make_repo(Path(_tf.mkdtemp()), "stubcase", {"README.md": "x\n"})
    stub = '"""Stub for lib/new.py -- implement per TASK.md."""\n'
    (r2 / "lib").mkdir(exist_ok=True)
    (r2 / "lib/new.py").write_text(stub)
    sref = "OLD = '''" + stub.strip() + "'''\n"
    check("A anchor on the UNSEALED creation stub is not a spec defect",
          hg.refimpl_base_anchor_findings(r2, "lib/new.py", sref), [])
    (r2 / "lib/new.py").write_text("def real():\n    return 1\n" * 40)
    check("A ... but a real implementation in the tree does not vouch for the anchor",
          codes(hg.refimpl_base_anchor_findings(r2, "lib/new.py", sref)), ["REFIMPL_ANCHOR_UNMATCHED"])


def part_b(root):
    # base blobs -> temp repo (committed = the BASE commit); harness files untracked, as in the replay
    files = {str(p.relative_to(FIXT / "base")): p.read_text() for p in (FIXT / "base").rglob("*") if p.is_file()}
    repo = make_repo(root, "om", files)
    for n in ("TASK.md", "refimpl.py", "verify.sh", ".dispatch-harness.json"):
        (repo / n).write_text((FIXT / n).read_text())
    check("B replay: the creation target really is a real implementation at base",
          hg.is_stub_text(files["lib/payoutMismatch.ts"]), False)
    fs = hg.spec_defects(repo)
    check("B replay: gate reports CREATION_TARGET_EXISTS for lib/payoutMismatch.ts",
          [(f["code"], f["path"]) for f in fs if f["code"] == "CREATION_TARGET_EXISTS"],
          [("CREATION_TARGET_EXISTS", "lib/payoutMismatch.ts")])
    check("B replay: ... and the refimpl OLD anchor matches nothing at base",
          "REFIMPL_ANCHOR_UNMATCHED" in codes(fs), True)

    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), OLLAMA_DISPATCH_HOME=str(home / ".ollama-dispatch"))
    p = subprocess.run([sys.executable, str(PRE), str(repo), "--json", "--no-relevance",
                        "--refimpl-cmd", "python3 refimpl.py"], capture_output=True, text=True, env=env, timeout=300)
    d = json.loads(p.stdout)
    rows = {c["check"]: c for c in d["checks"]}
    check("B preflight CLI: NO-GO", d["verdict"], "NO-GO")
    check("B ... blocked by spec-defect", "spec-defect" in [b["check"] for b in d["blockers"]], True)
    check("B ... SPEC_DEFECT message names the path", "SPEC_DEFECT: creation target exists at base: lib/payoutMismatch.ts"
          in rows["spec-defect"]["message"], True)
    check("B ... NOTHING executed (baseline-fails UNPROVEN)", rows["baseline-fails"]["status"], "UNPR")
    check("B ... NOTHING executed (refimpl-passes UNPROVEN)", rows["refimpl-passes"]["status"], "UNPR")
    check("B ... the contract is not claimed", rows["fail-before-pass-after"]["status"], "UNPR")

    # auto's pre-enqueue precheck (the call site before any authoring job)
    ld = SourceFileLoader("ods_auto_t", str(AUTO))
    asp = importlib.util.spec_from_loader("ods_auto_t", ld)
    am = importlib.util.module_from_spec(asp)
    ld.exec_module(am)
    class A:  # the fields the precheck reads
        intent = ("MUST HOLD: create lib/payoutMismatch.ts exporting pure payoutMismatch(o): boolean "
                  "with these rules in order")
        interface = ""
    msg = am.spec_defect_precheck(A, repo, "lib/payoutMismatch.ts")
    check("B auto precheck: SPEC_DEFECT before any authoring job",
          msg.startswith("SPEC_DEFECT: creation target exists at base: lib/payoutMismatch.ts"), True)
    A.intent = "modify lib/payoutMismatch.ts so a partial payment is not a mismatch"
    check("B auto precheck: a modify intent over the same file passes", am.spec_defect_precheck(A, repo, "lib/payoutMismatch.ts"), "")
    # the preflight NO-GO shape auto reads (no refine round on a spec defect)
    check("B auto reads the preflight spec-defect blocker",
          am.spec_defect_from_preflight(d).startswith("SPEC_DEFECT: creation target exists at base"), True)
    check("B ... a GO / other blocker is not a spec defect",
          am.spec_defect_from_preflight({"blockers": [{"check": "baseline-clean"}]}), "")

    # END TO END through the real auto CLI: refuses before authoring, exit 1, no job
    arepo = make_repo(root, "om2", files)
    ahome = root / "ahome"
    (ahome / ".ollama-dispatch").mkdir(parents=True, exist_ok=True)
    aenv = dict(os.environ, HOME=str(ahome), OLLAMA_DISPATCH_HOME=str(ahome / ".ollama-dispatch"))
    try:
        ap = subprocess.run([sys.executable, str(AUTO), "--repo", str(arepo), "--lang", "ts", "--symbol", "payoutMismatch",
                             "--target", "lib/payoutMismatch.ts", "--label", "om-e2e", "--no-auto-slice", "--no-park", "--drafter-cmd", "false",
                             "--intent", "create lib/payoutMismatch.ts exporting pure payoutMismatch(o): boolean",
                             "--dest", str(root / "wt-om2")],
                            capture_output=True, text=True, env=aenv, timeout=120)
        out, rc = ap.stdout + ap.stderr, ap.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = ((e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")), 124
    check("B auto CLI: exit 1 with the SPEC_DEFECT reason", (rc, "SPEC_DEFECT: creation target exists at base" in out), (1, True))
    check("B auto CLI: no authoring was started", "authoring the harness" in out, False)


def part_c():
    cl = hg.classify_baseline_red
    r = cl("AssertionError [ERR_ASSERTION]: Expected values to be strictly equal\n", False, "")
    check("C assertion failure = red", (r["ok"], r["kind"]), (True, "assertion"))
    r = cl("=== run ===\n  FAIL: f() returns 1\n--- 1 failed ---\n", False, "")
    check("C verify.sh `FAIL:` line = red (assertion)", (r["ok"], r["kind"]), (True, "assertion"))
    r = cl("  FAIL: npm ci failed\n", False, "")
    check("C an env-bootstrap FAIL line is NOT assertion evidence", r["kind"] != "assertion", True)
    r = cl("SyntaxError: Unexpected token '}'\n    at wrapSafe\n", False, "")
    check("C SyntaxError alone = NOT red", (r["ok"], r["kind"]), (False, "crash"))
    r = cl("src/a.ts(3,1): error TS1005: ';' expected.\n", True, "")
    check("C TS compile error = NOT red even for a creation task", (r["ok"], r["kind"]), (False, "crash"))
    r = cl("ReferenceError: fooBar is not defined\n", False, "fooBar")
    check("C ReferenceError = NOT red", r["ok"], False)
    r = cl("TypeError: Cannot read properties of undefined (reading 'x')\n", True, "")
    check("C a runtime TypeError = NOT red", r["ok"], False)
    msg = "SyntaxError: The requested module './t.ts' does not provide an export named 'payoutMismatch'\n"
    r = cl(msg, False, "must export payoutMismatch")
    check("C missing export NAMED in the spec = expected red", (r["ok"], r["kind"]), (True, "expected-missing"))
    r = cl(msg, False, "something else entirely")
    check("C missing export the spec never names (edit task) = NOT red", r["ok"], False)
    r = cl(msg, True, "")
    check("C missing export on a CREATION task = expected red", (r["ok"], r["kind"]), (True, "expected-missing"))
    r = cl("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/w/lib/payoutMismatch.ts'\n", True, "")
    check("C missing module on a creation target = expected red (the stub is absent)", (r["ok"], r["kind"]), (True, "expected-missing"))
    r = cl("ModuleNotFoundError: No module named 'utils'\n", False, "")
    check("C missing module on an edit task, unnamed = NOT red", r["ok"], False)
    r = cl("TypeError: foo is not a function\n", False, "make foo return 1")
    check("C `foo is not a function` where the spec names foo = expected red", (r["ok"], r["kind"]), (True, "expected-missing"))
    r = cl("--- 1 failed ---\n", False, "")
    check("C output with no signal is accepted (never fail closed on unreadable text)", (r["ok"], r["kind"]), (True, "unclassified"))
    r = cl("TypeError: x.y is not a function\nAssertionError: boom\n", False, "")
    check("C a crash AND an assertion = red (at least one assertion case)", r["ok"], True)


def part_d():
    t1 = "not ok 1 - handles empty\n  AssertionError [ERR_ASSERTION]: Expected 3 got 4 at /tmp/a/b/c.ts:12:5\n"
    t2 = "not ok 1 - handles empty\n  AssertionError [ERR_ASSERTION]: Expected 3 got 4 at /var/x/y/z.ts:99:1\n"
    t3 = "not ok 1 - handles other\n  AssertionError [ERR_ASSERTION]: Expected 3 got 4\n"
    s1, s2, s3 = hg.failure_signature(t1), hg.failure_signature(t2), hg.failure_signature(t3)
    check("D signature is stable across path noise", s1 == s2 and bool(s1), True)
    check("D ... different failing case = different signature", s1 != s3, True)
    check("D no failure evidence = empty signature", hg.failure_signature("all good\n"), "")
    st, n = hg.repeat_failure_state(None, s1, 0)
    check("D round 1: continue", (st, n), ("continue", 1))
    st, n = hg.repeat_failure_state(s1, s1, n)
    check("D round 2 identical: hint (signature fed back)", (st, n), ("hint", 2))
    st, n = hg.repeat_failure_state(s1, s1, n)
    check("D round 3 identical: abandon", (st, n), ("abandon", 3))
    st, n = hg.repeat_failure_state(s1, s3, n)
    check("D a different failure resets the count", (st, n), ("continue", 1))
    check("D empty signature never counts", hg.repeat_failure_state("", "", 5), ("continue", 0))
    ld = SourceFileLoader("ods_auto_t2", str(AUTO))
    asp = importlib.util.spec_from_loader("ods_auto_t2", ld)
    am = importlib.util.module_from_spec(asp)
    ld.exec_module(am)
    b1 = [{"check": "refimpl-tests", "message": "the reference impl FAILS 1 of the fixture's own test(s)", "detail": "handles empty"},
          {"check": "refimpl-passes", "message": "x (exit 1)", "detail": t1}]
    b2 = [dict(b1[0]), dict(b1[1], detail=t2)]
    check("D auto: identical failures across rounds share a signature",
          am.blockers_failure_signature(b1) == am.blockers_failure_signature(b2) != "", True)
    check("D contract_status: refimpl-tests FAIL breaks fail-before/pass-after",
          hg.contract_status([{"id": "baseline-fails", "status": "PASS"}, {"id": "refimpl-passes", "status": "PASS"},
                              {"id": "refimpl-tests", "status": "FAIL"}])[0], "FAIL")


def part_e():
    """Drive the REAL _preflight_loop (preflight/dispatch stubbed; no queue, no GPU)."""
    from types import SimpleNamespace
    ld = SourceFileLoader("ods_auto_t3", str(AUTO))
    asp = importlib.util.spec_from_loader("ods_auto_t3", ld)
    m = importlib.util.module_from_spec(asp)
    ld.exec_module(m)

    def drive(preflight_blockers):
        wt = Path(tempfile.mkdtemp(prefix="idfail-wt-"))
        (wt / "TASK.md").write_text("# TASK\n\n## Must contain\n\n- `x`\n")
        (wt / "refimpl.py").write_text("NEW = ''\n")
        (wt / "lib").mkdir()
        (wt / "lib/t.ts").write_text("export {};\n")
        calls, out, n = [], {}, [0]

        def preflight(wt_, req, a):
            n[0] += 1
            return 1, {"verdict": "NO-GO", "blockers": preflight_blockers(n[0])}

        def dispatch(wt_, prompt, label, verify_cmd, a, max_iters=None):
            calls.append(prompt)
            (wt / "lib/t.ts").write_text(f"export const v = {len(calls)};\n")   # the model KEEPS editing
            return True, "converged"

        m.slice_already_satisfied = lambda *x: False
        m.slice_taken_elsewhere = lambda *x: None
        m.driver_gone = lambda a: None
        m.chain_state_write = lambda *x, **k: None
        m.enforce_target_at_head = lambda *x: []
        m.run_preflight = preflight
        m.wait_out_operational_blockers = lambda wt_, req, a, rc, data: (rc, data)
        m.repair_state_only_blockers = lambda wt_, req, a, rc, data: (rc, data)
        m.maybe_restart_stale = lambda *x, **k: None
        m.survivors_of = lambda data: []
        m.relevance_verdict = lambda data: None
        m.write_refine_guard = lambda *x, **k: None
        m.clear_refine_guard = lambda *x, **k: None
        m.must_contain_from_task = lambda wt_: []
        m.dispatch_model = dispatch
        m.log_decision = lambda *x, **k: None
        m._print_diff = lambda *x, **k: None
        m._autoslice_on_failure = lambda a, t, why: out.setdefault("fail", why) and 9
        def _die(msg, code=2):
            out["die"] = msg
            raise SystemExit(code)
        m.die = _die
        a = SimpleNamespace(require=[], max_rounds=6, no_progress_rounds=2, author_max_iters=24,
                            lang="typescript", slice_plan=None, slice_id=None, label="x")
        try:
            rc = m._preflight_loop(a, wt, "lib/t.ts", "python3 auto-harness-check.py")
        except SystemExit as e:
            rc = ("exit", e.code)
        return rc, calls, out

    def same_failure(k):   # identical assertion + case, only path/line noise differs per round
        return [{"check": "refimpl-tests", "message": "the reference impl FAILS 1 of the fixture's own test(s)",
                 "detail": "handles empty"},
                {"check": "refimpl-passes", "message": "reference impl does NOT satisfy the verify (exit 1)",
                 "detail": f"not ok 1 - handles empty\n  AssertionError [ERR_ASSERTION]: Expected 3 got 4 at /tmp/r{k}/a/b.ts:{10 + k}:1\n"}]
    rc, calls, out = drive(same_failure)
    check("E the SAME failure 3 rounds running (model keeps editing) is abandoned", rc, 9)
    check("E ... after exactly 2 refine rounds (round 3 abandons)", len(calls), 2)
    check("E ... the reason names the identical failure", "IDENTICAL failure" in out.get("fail", ""), True)
    check("E round 1 prompt carries no repeat warning", "EXACT failure" in calls[0], False)
    check("E round 2 prompt feeds the repeated-failure signature back", "EXACT failure" in calls[1] and "change the approach" in calls[1], True)

    def changing_failure(k):
        f = same_failure(k)
        f[0]["detail"] = f"case number {k}"
        f[1]["detail"] = f"not ok 1 - case {k}\n  AssertionError [ERR_ASSERTION]: Expected {k}\n"
        return f
    rc, calls, out = drive(changing_failure)
    check("E a CHANGING failure is not abandoned for repetition (runs to the round cap)", ("IDENTICAL" in out.get("fail", ""), len(calls)), (False, 5))

    rc, calls, out = drive(lambda k: [{"check": "spec-defect", "message": "SPEC_DEFECT: creation target exists at base: lib/t.ts",
                                       "detail": "x"}])
    check("E a spec-defect NO-GO stops the loop with the SPEC_DEFECT reason", (rc, "SPEC_DEFECT" in out.get("die", "")), (("exit", 1), True))
    check("E ... and no refine round (no GPU) was dispatched", calls, [])


def part_f(root):
    """Deterministic base resolution + base_sha metadata + baseline probe (smoke defect 2)."""
    def mk_origin(name):
        bare = root / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        w = root / f"{name}-w"
        subprocess.run(["git", "clone", "-q", str(bare), str(w)], check=True, capture_output=True)
        for c in (["config", "user.email", "t@t"], ["config", "user.name", "t"], ["checkout", "-q", "-b", "main"]):
            git(w, *c)
        (w / "t.py").write_text("def f():\n    return 0\n")
        git(w, "add", "-A"); git(w, "commit", "-qm", "c1"); git(w, "push", "-q", "origin", "main")
        c1 = git(w, "rev-parse", "HEAD").stdout.strip()
        repo = root / f"{name}-repo"
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True, capture_output=True)
        git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
        (w / "t.py").write_text("def f():\n    return 1\n")
        git(w, "commit", "-qam", "c2"); git(w, "push", "-q", "origin", "main")
        c2 = git(w, "rev-parse", "HEAD").stdout.strip()
        return repo, c1, c2

    repo, c1, c2 = mk_origin("o1")
    sha, note = hg.resolve_base_sha(repo, c1)
    check("F explicit --baseline-ref resolves to that commit", sha, c1)
    check("F ... and makes NO network fetch (origin/main still at the old tip)",
          git(repo, "rev-parse", "origin/main").stdout.strip(), c1)
    sha, note = hg.resolve_base_sha(repo)
    check("F default: HEAD strictly behind origin -> the origin tip (deterministic)", sha, c2)
    try:
        hg.resolve_base_sha(repo, "no-such-ref")
        bad = False
    except ValueError:
        bad = True
    check("F an explicit ref that is not a commit is refused", bad, True)
    git(repo, "commit", "-q", "--allow-empty", "-m", "local-only")
    sha, note = hg.resolve_base_sha(repo)
    check("F default: HEAD ahead/diverged -> keeps HEAD", sha, git(repo, "rev-parse", "HEAD").stdout.strip())

    # 3 concurrent resolutions must agree and none may fall back on a fetch collision
    repo2, d1, d2 = mk_origin("o2")
    code = ("import sys,importlib.util;sp=importlib.util.spec_from_file_location('g',%r);g=importlib.util.module_from_spec(sp);"
            "sp.loader.exec_module(g);print(*g.resolve_base_sha(sys.argv[1]),sep='|')" % str(HERE / "dispatch_harness_gates.py"))
    procs = [subprocess.Popen([sys.executable, "-c", code, str(repo2)], stdout=subprocess.PIPE, text=True) for _ in range(3)]
    outs = [p.communicate()[0].strip() for p in procs]
    check("F 3 concurrent resolutions agree on the origin tip", sorted({o.split("|")[0] for o in outs}), [d2])
    check("F ... none fell back to HEAD on a fetch collision", any("failed" in o for o in outs), False)

    # base_sha metadata + drift
    wt = root / "drift"
    git(repo, "worktree", "add", "-q", "-b", "dispatch/drift", str(wt), c2)
    hg.record_base_sha(wt, c2, "note")
    check("F base_sha is recorded in the harness manifest", json.loads((wt / ".dispatch-harness.json").read_text())["base_sha"], c2)
    check("F HEAD == recorded base: no drift", hg.base_drift_findings(wt), [])
    (wt / "x").write_text("x"); git(wt, "add", "x"); git(wt, "commit", "-qm", "seal")
    check("F a seal commit on top of the base is not drift", hg.base_drift_findings(wt), [])
    git(wt, "reset", "-q", "--hard", c1)
    check("F a re-base/reset away from the recorded base IS drift (BASE_DRIFT)", codes(hg.base_drift_findings(wt)), ["BASE_DRIFT"])
    check("F ... and spec_defects carries it", "BASE_DRIFT" in codes(hg.spec_defects(wt, target="t.py", creation=False)), True)

    # baseline probe
    pw = root / "probe"; pw.mkdir()
    check("F probe without verify.sh does not run", hg.baseline_probe(pw)["ran"], False)
    (pw / "verify.sh").write_text("echo VERIFY_OK\n")
    check("F probe green", hg.baseline_probe(pw)["green"], True)
    (pw / "verify.sh").write_text("echo nope; exit 1\n")
    r = hg.baseline_probe(pw)
    check("F probe red", (r["green"], r["rc"]), (False, 1))


    # auto's pre-author baseline probe: green at base + every --require literal already present
    ld = SourceFileLoader("ods_auto_t4", str(AUTO)); asp = importlib.util.spec_from_loader("ods_auto_t4", ld)
    am = importlib.util.module_from_spec(asp); ld.exec_module(am)
    pw2 = root / "probe2"; git(root, "init", "-q", str(pw2)); git(pw2, "config", "user.email", "t@t"); git(pw2, "config", "user.name", "t")
    (pw2 / "t.py").write_text("def f():\n    return 1\n"); (pw2 / "verify.sh").write_text("echo VERIFY_OK\n")
    git(pw2, "add", "-A"); git(pw2, "commit", "-qm", "i")
    (pw2 / ".dispatch-harness.json").write_text(json.dumps({"target": "t.py"}))
    class PA:
        drafter_cmd = None; _base_sha = None; require = ["return 1"]
    os.environ.pop("ODS_NO_BASELINE_PROBE", None)
    check("F baseline probe green + every --require literal already at base = SPEC_DEFECT already satisfied",
          am.baseline_probe_precheck(PA, pw2, "t.py").startswith("SPEC_DEFECT: already satisfied at base"), True)
    check("F ... the probe result is recorded in the manifest",
          json.loads((pw2 / ".dispatch-harness.json").read_text()).get("baseline_probe", {}).get("green"), True)
    PA.require = ["return 2"]
    check("F ... a literal the base lacks is not already satisfied", am.baseline_probe_precheck(PA, pw2, "t.py"), "")
    (pw2 / "verify.sh").write_text("exit 1\n"); PA.require = ["return 1"]
    check("F ... a RED probe is never already satisfied", am.baseline_probe_precheck(PA, pw2, "t.py"), "")

    # auto end to end: --baseline-ref honoured, wt cut from it, base_sha recorded
    ahome = root / "ahome2"; (ahome / ".ollama-dispatch").mkdir(parents=True)
    aenv = dict(os.environ, HOME=str(ahome), OLLAMA_DISPATCH_HOME=str(ahome / ".ollama-dispatch"))
    def run_auto(repo_, label, *extra):
        try:
            ap = subprocess.run([sys.executable, str(AUTO), "--repo", str(repo_), "--lang", "python", "--target", "t.py",
                                 "--label", label, "--no-auto-slice", "--no-park", "--drafter-cmd", "false",
                                 "--intent", "make f() return 1", "--dest", str(root / f"wt-{label}"), *extra],
                                capture_output=True, text=True, env=aenv, timeout=240)
            return ap.returncode, ap.stdout + ap.stderr
        except subprocess.TimeoutExpired:
            return 124, ""
    rc, out = run_auto(repo2, "pin", "--baseline-ref", d1)
    wtp = root / "wt-pin"
    check("F auto --baseline-ref: worktree cut from the pinned commit (not HEAD/origin)",
          git(wtp, "rev-parse", "HEAD").stdout.strip(), d1)
    check("F ... base_sha recorded for later gates", json.loads((wtp / ".dispatch-harness.json").read_text()).get("base_sha"), d1)
    check("F ... no fetch was made for the pinned base", "no fetch" in out, True)
    rc, out = run_auto(repo2, "dflt")
    wtd = root / "wt-dflt"
    check("F auto default: deterministic base = origin tip when HEAD is behind", git(wtd, "rev-parse", "HEAD").stdout.strip(), d2)
    check("F ... recorded", json.loads((wtd / ".dispatch-harness.json").read_text()).get("base_sha"), d2)
    rc, out = run_auto(repo2, "badref", "--baseline-ref", "nope-ref")
    check("F auto: a bad --baseline-ref is refused before any scaffold", (rc, "REFUSING" in out, (root / "wt-badref").exists()), (2, True, False))


def main():
    root = Path(tempfile.mkdtemp(prefix="spec-defect-"))
    part_a(root)
    part_b(root)
    part_c()
    part_d()
    part_e()
    part_f(root)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
