#!/usr/bin/env python3
"""Behavioural tests for the gate <-> authoring-orchestrator worktree race.

2026-10-05, rt-egift-link-s1-s0 d25ff267b7c9: gate-on-complete's in-place
verify-relevance (`git add -N` on the creation target, refimpl applied, mutants)
ran in the SAME worktree, at the same time, as ollama-dispatch-auto's continuation
self-check. The intent-to-add landed between the self-check's before/after
porcelain snapshots, the continuation was refused as "unattributable" and the
slice FAILED with two continuation rounds unused.

  A  await_gate_tree_release waits for a live gate hook until <id>.gate.json lands
  B  ... returns promptly when no gate is coming
  C  the gate's _tree_lock_acquire and auto's tree_lock exclude each other
  D  _harness_check_output under a concurrent gate-style `git add -N`: CLEAN with
     the lock, CONTAMINATED with the lock disabled (revert test: the guard bites)
  E  the plan's verify_shape reaches the author prompt (slicer arg + prompt section)
  F  the TS fixture template imports `mock` from node:test

Hermetic: temp git repos + temp log dirs, no queue, no enqueue.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
FAILS = []


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


AUTO = load("_auto_race", Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto"))
GATE = load("_gate_race", BIN / "gate-on-complete.py")
SLICE = load("_slice_race", BIN / "ollama-dispatch-slice")


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def mk_repo(tmp):
    wt = Path(tmp) / "wt"
    wt.mkdir()
    git(wt, "init", "-q")
    (wt / "a.txt").write_text("a\n")
    git(wt, "add", "a.txt")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (wt / "lib").mkdir()
    (wt / "lib" / "store.ts").write_text("export {};\n")   # untracked creation stub
    return wt


def fake_gate(tmp, job, body, sleep_s):
    """A process whose argv reads `.../gate-on-complete.py --job-id <job>` (what
    _gate_hook_alive matches), running `body` after `sleep_s`."""
    d = Path(tmp) / "fakebin"
    d.mkdir(exist_ok=True)
    sc = d / "gate-on-complete.py"
    sc.write_text(textwrap.dedent(f"""
        import sys, time, pathlib
        time.sleep({sleep_s})
        {body}
    """))
    return subprocess.Popen([sys.executable, str(sc), "--job-id", job])


# --- A / B --------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    wt = mk_repo(tmp)
    ld = Path(tmp) / "logs"
    ld.mkdir()
    job = "aaaabbbbcccc"
    p = fake_gate(tmp, job, f"pathlib.Path({str(ld / (job + '.gate.json'))!r}).write_text('{{}}')", 3)
    t0 = time.time()
    st = AUTO.await_gate_tree_release(job, wt, grace=1, ceiling=60, log_dir=ld)
    el = time.time() - t0
    p.wait()
    check("A: waits for a live gate hook until gate.json lands", st == "gate-landed" and el >= 2.5,
          f"status={st} elapsed={el:.1f}s")

    t0 = time.time()
    st = AUTO.await_gate_tree_release("ddddeeeeffff", wt, grace=1, ceiling=60, log_dir=ld)
    el = time.time() - t0
    check("B: no gate coming -> returns after the spawn grace", st == "no-gate" and el < 10,
          f"status={st} elapsed={el:.1f}s")

# --- C --------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    wt = mk_repo(tmp)
    fh = GATE._tree_lock_acquire(wt, timeout=5)
    check("C1: gate takes the tree lock", bool(fh))
    with AUTO.tree_lock(wt, timeout=1) as got:
        check("C2: auto cannot take it while the gate holds it", got is False)
    GATE._tree_lock_release(fh)
    with AUTO.tree_lock(wt, timeout=1) as got:
        check("C3: auto takes it once the gate released it", got is True)
        fh2 = GATE._tree_lock_acquire(wt, timeout=1)
        check("C4: the gate abstains (False) while auto holds it", fh2 is False)
    lp = AUTO.tree_lock_path(wt)
    check("C5: the lock lives in the git dir, never in `git status`",
          lp is not None and ".git" in str(lp) and git(wt, "status", "--porcelain").stdout.count("lock") == 0)


# --- D --------------------------------------------------------------------------
def race_once(use_lock):
    with tempfile.TemporaryDirectory() as tmp:
        wt = mk_repo(tmp)
        # a gate-style mutator: takes the gate's tree lock (when the fix is live),
        # stages the creation target intent-to-add, holds it, then resets.
        script = Path(tmp) / "mut.py"
        script.write_text(textwrap.dedent(f"""
            import importlib.util, subprocess, sys, time
            from importlib.machinery import SourceFileLoader
            ld = SourceFileLoader("g", {str(BIN / 'gate-on-complete.py')!r})
            g = importlib.util.module_from_spec(importlib.util.spec_from_loader("g", ld)); ld.exec_module(g)
            wt = {str(wt)!r}
            fh = g._tree_lock_acquire(wt, timeout=30) if {use_lock!r} else None
            time.sleep(1.0)
            subprocess.run(["git", "-C", wt, "add", "-N", "--", "lib/store.ts"])
            time.sleep(3.0)
            subprocess.run(["git", "-C", wt, "reset", "-q", "--", "lib/store.ts"])
            g._tree_lock_release(fh)
        """))
        mp = subprocess.Popen([sys.executable, str(script)])
        time.sleep(0.4 if use_lock else 1.5)   # with the lock: let the mutator take it first
        real_lock = AUTO.tree_lock
        if not use_lock:
            class _nolock:
                def __init__(self, *a, **k): pass
                def __enter__(self): return True
                def __exit__(self, *e): return False
            AUTO.tree_lock = _nolock
        try:
            out, reason = AUTO._harness_check_output(wt, "sleep 3", "lib/store.ts")
        finally:
            AUTO.tree_lock = real_lock
        mp.wait()
        return reason


r_fixed = race_once(True)
check("D1: self-check under a concurrent gate mutation is CLEAN with the tree lock", r_fixed is None,
      f"reason={r_fixed}")
r_broken = race_once(False)
check("D2 (revert test): with the lock disabled the same race is CONTAMINATED",
      r_broken is not None and "different state" in r_broken, f"reason={r_broken}")

# --- E --------------------------------------------------------------------------
check("E1: slicer forwards the plan's verify_shape",
      SLICE._verify_shape_args({"verify_shape": "real sqlite db"}) == ["--verify-shape=real sqlite db"])
check("E2: no verify_shape -> no arg", SLICE._verify_shape_args({"verify_shape": ""}) == [])


class _A:
    lang = "ts"
    intent = "add a model"
    interface = ""
    verify_shape = "behavioral: throwaway SQLite DB from replayed migrations"
    edit_file = []
    require = []


try:
    pr = AUTO.author_prompt(_A(), "lib/x.ts")
    check("E3: author prompt carries the plan's verify design", "throwaway SQLite DB" in pr)
except Exception as e:  # author_prompt may need more attributes on some paths
    check("E3: author prompt carries the plan's verify design", False, f"{type(e).__name__}: {e}")
_A.verify_shape = ""
check("E4: empty verify_shape adds no section", AUTO.verify_shape_section(_A()) == "")

# --- F --------------------------------------------------------------------------
check("F: TS fixture template imports mock",
      "import {{ test, mock }} from 'node:test';" in (BIN / "ollama-dispatch-scaffold").read_text())

# --- G: Jest-API hint (5d52fd8a8d5f) ---------------------------------------------
def _jest_wt(msg):
    wt = Path(tempfile.mkdtemp(prefix="jestapi-")) / "wt"
    wt.mkdir(parents=True)
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(wt, *a)
    (wt / "target.txt").write_text("stub\n")
    git(wt, "add", "."); git(wt, "commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\n\n## Scope\nOnly edit `target.txt`; "
                                "do not edit `verify.sh` or `TASK.md`.\n")
    (wt / "verify.sh").write_text("grep -q MARK target.txt || { echo 'FAIL: no MARK'; exit 1; }\n"
                                  "echo \"error: '%s'\"; echo 'FAIL: new test file(s) failed'; exit 1\n" % msg)
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    AUTO.write_harness_check(wt, "ts")
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


_rc, _out = _jest_wt("import_node_test.mock.fn(...).mockResolvedValue is not a function")
check("G1: Jest mockResolvedValue failure -> node:test API hint",
      _rc != 0 and "is JEST's API" in _out and "mock.fn(async () => value)" in _out, _out[-900:])
_rc, _out = _jest_wt("x.mockImplementationOnce is not a function")
check("G2: Jest mockImplementationOnce also matched", "`.mockImplementationOnce` is JEST's API" in _out, _out[-900:])
_rc, _out = _jest_wt("expected 1 row, got 2")
check("G3: control -- unrelated failure gets no Jest hint", _rc != 0 and "JEST's API" not in _out, _out[-900:])

# --- H: --regate accepts a HAND-LOCKED pending slice -----------------------------
SL = load("_slice_regate", Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice"))


def _regate(status, locked):
    wt = Path(tempfile.mkdtemp(prefix="regate-"))
    if locked:
        (wt / SL.HAND_LOCK).write_text("x")
    st = {"order": ["s0"], "label": "t", "slices": {"s0": {"status": status, "worktree": str(wt)}}}
    called = []
    SL.save_state = lambda *_a, **_k: None
    SL.slice_job_inflight = lambda *_a, **_k: None
    SL._ignore_hand_lock = lambda *_a, **_k: None
    SL.confirm_and_enqueue = lambda *a, **k: called.append(a[1])
    try:
        SL.regate_slice(st, "s0", "m", "h", 1, 1, "auto", 0)
    except SystemExit:
        return False, st["slices"]["s0"]["status"]
    return bool(called), st["slices"]["s0"]["status"]


_ok, _stt = _regate(SL.PENDING, True)
check("H1: hand-locked PENDING slice is re-gated (CONFIRMED + gate run)", _ok and _stt == SL.CONFIRMED, _stt)
_ok, _stt = _regate(SL.PENDING, False)
check("H2: unlocked PENDING slice is still refused", not _ok and _stt == SL.PENDING, _stt)
_ok, _stt = _regate(SL.FAILED, False)
check("H3: FAILED slice still re-gated", _ok, _stt)

# --- I: declared-scope NEW non-source files reach the relevance diff -------------
def _scope_wt():
    wt = Path(tempfile.mkdtemp(prefix="scopesql-")) / "wt"
    wt.mkdir(parents=True)
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(wt, *a)
    (wt / "lib").mkdir()
    (wt / "lib/store.ts").write_text("export {};\n")
    git(wt, "add", "."); git(wt, "commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Scope\nOnly edit `lib/store.ts`, `db/m.sql`; do not edit `verify.sh`.\n")
    (wt / "verify.sh").write_text("grep -q CASCADE db/m.sql && grep -q UNIQUE db/m.sql && echo VERIFY_OK || exit 1\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/store.ts", "creation_task": True}))
    (wt / "db").mkdir()
    (wt / "db/m.sql").write_text("CREATE TABLE t (\n  a INT UNIQUE,\n  b INT REFERENCES o ON DELETE CASCADE\n);\n")
    (wt / "stray.sql").write_text("SELECT 1;\n")   # undeclared: must NOT be pulled in
    return wt


PF = load("_pf_scope", Path(os.environ.get("PF_SRC") or BIN / "ollama-dispatch-preflight"))


class _PA:
    target = None
    task_file = "TASK.md"


_wt = _scope_wt()
_pa = _PA(); _pa.worktree = str(_wt)
_pf = PF.Preflight(_pa)
_pf.task_text = (_wt / "TASK.md").read_text()
_pf._pre_refimpl_untracked = set()
_d = _pf.untracked_refimpl_diff()
check("I1: preflight relevance diff includes the declared NEW .sql file", "b/db/m.sql" in _d, _d[-600:])
check("I2: undeclared new .sql by-product still excluded", "stray.sql" not in _d, _d[-600:])

_wt = _scope_wt()
_vr = os.environ.get("VR_SRC") or str(BIN / "verify-relevance.py")
_r = subprocess.run([sys.executable, _vr, str(_wt), "--applied", "--json"],
                    capture_output=True, text=True, timeout=300)
try:
    _rec = json.loads(_r.stdout)
except ValueError:
    _rec = {}
_files = ((_rec.get("generation") or {}).get("files") or {})
check("I3: gate (--applied) mutates the declared NEW .sql file", "db/m.sql" in _files,
      (_r.stdout + _r.stderr)[-600:])
check("I4: gate (--applied) leaves undeclared stray.sql alone", "stray.sql" not in _files, _files)
check("I5: --applied leaves the tree untracked as it was",
      "?? db/m.sql" in git(_wt, "status", "--porcelain", "--untracked-files=all").stdout)

# --- J: check_literals names the NEAR-MISS token it found (0ea47b5f09f8) ---------
SC = load("_scaf_near", Path(os.environ.get("SCAF_SRC") or BIN / "ollama-dispatch-scaffold"))


def _lit_run(body):
    d = Path(tempfile.mkdtemp(prefix="nearmiss-"))
    (d / "lib").mkdir()
    (d / "TASK.md").write_text("## Must contain\n- in schema.txt: `model OrderEgiftLink {`\n"
                               "- `upsertOrderEgiftLink`\n\n## Scope\nOnly edit `lib/s.ts`.\n")
    (d / "check_literals.py").write_text(SC.CHECK_LITERALS.format(target="lib/s.ts"))
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        SC.freeze_literals(d)
    (d / "schema.txt").write_text(body[0])
    (d / "lib/s.ts").write_text(body[1])
    r = subprocess.run([sys.executable, "check_literals.py"], cwd=d, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


_rc, _o = _lit_run(("model OrderEgmtLink {\n", "export async function upsertOrderEgmtLink() {}\n"))
check("J1: misspelled token is named as a NEAR MISS with the rename",
      _rc == 1 and "has `OrderEgmtLink` where the literal needs `OrderEgiftLink`" in _o
      and "`upsertOrderEgmtLink` where the literal needs `upsertOrderEgiftLink`" in _o, _o)
_rc, _o = _lit_run(("model OrderEgiftLink {\n", "export async function upsertOrderEgiftLink() {}\n"))
check("J2: control -- correct spelling passes with no near-miss line", _rc == 0 and "NEAR MISS" not in _o, _o)
_rc, _o = _lit_run(("model Order {\n", "export {};\n"))
check("J3: control -- genuinely absent literal gets no near-miss line", _rc == 1 and "NEAR MISS" not in _o, _o)

# --- K: gate auto-fix feedback names the near-miss spelling (0ea47b5f09f8) -------
GT = load("_gate_near", Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py"))
_k = Path(tempfile.mkdtemp(prefix="gatenear-")) / "wt"
_k.mkdir(parents=True)
for _a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
    git(_k, *_a)
(_k / "schema.prisma").write_text("model Order {\n  id Int\n}\n")
git(_k, "add", "."); git(_k, "commit", "-qm", "b")
(_k / "schema.prisma").write_text("model Order {\n  id Int\n  egiftLink OrderEgmtLink?\n}\n")
(_k / "m.sql").write_text('CREATE TABLE "OrderEgmtLink" ();\n')
(_k / "TASK.md").write_text("mentions OrderEgiftLinkTypoBait only in harness\n")
_iss = [{"severity": "high", "source": "completeness",
         "what": "spec names `model OrderEgiftLink {` but it is absent from the diff"},
        {"severity": "high", "source": "completeness",
         "what": "spec names `CREATE TABLE \"OrderEgiftLink\"` but it is absent from the diff"}]
_fb = getattr(GT, "_near_miss_feedback", lambda *a: [])(str(_k), _iss)
check("K1: gate feedback names `OrderEgmtLink` vs `OrderEgiftLink`",
      any("`OrderEgmtLink` where the spec spelling is `OrderEgiftLink`" in l for l in _fb), _fb)
(_k / "store.ts").write_text("prisma.orderEgmtLink.upsert()\\n")
_fb2 = getattr(GT, "_near_miss_feedback", lambda *a: [])(str(_k), _iss)
check("K1b: lowercase accessor keeps its case in the rename",
      any("rename EVERY `orderEgmtLink` to `orderEgiftLink`" in l for l in _fb2), _fb2)
(_k / "store.ts").unlink()
check("K2: one line per (have, want) pair, not per occurrence", len(_fb) == 1, _fb)
_fb = getattr(GT, "_near_miss_feedback", lambda *a: [])(str(_k), [{"what": "spec names `ZebraQuux` but it is absent from the diff"}])
check("K3: control -- no near-identical token, no feedback", _fb == [], _fb)
check("K4: builder wires the helper into the auto-fix task",
      "_near_miss_feedback(cwd, payload.get(\"issues\")" in Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py").read_text())

# --- L: slicer follows the gate's auto-fix successor (0ea47b5f09f8 -> 01fdda651fba) --
SL2 = load("_slice_afs", Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice"))
_qs = Path(tempfile.mkdtemp(prefix="afs-")) / "state.json"


def _afs(jobs):
    _qs.write_text(json.dumps({"jobs": jobs}))
    f = getattr(SL2, "autofix_successor", None)
    return f("a0", state_path=str(_qs)) if f else None


_base = {"id": "a0", "status": "failed", "auto_fix_root": "a0", "auto_fix_round": 0, "cwd": "/w"}
check("L1: pending auto-fix r1 in the same worktree is the successor",
      _afs([_base, {"id": "a1", "status": "pending", "auto_fix_root": "a0", "auto_fix_round": 1, "cwd": "/w"}])
      == ("a1", "pending", 1))
check("L2: newest live round wins",
      (_afs([_base, {"id": "a1", "status": "failed", "auto_fix_root": "a0", "auto_fix_round": 1, "cwd": "/w"},
             {"id": "a2", "status": "running", "auto_fix_root": "a0", "auto_fix_round": 2, "cwd": "/w"}]) or [None])[0] == "a2")
check("L3: control -- failed round / other worktree / no round -> None (slice FAILS as before)",
      _afs([_base, {"id": "a1", "status": "failed", "auto_fix_root": "a0", "auto_fix_round": 1, "cwd": "/w"},
            {"id": "b1", "status": "pending", "auto_fix_root": "a0", "auto_fix_round": 1, "cwd": "/other"}]) is None)


# poll branch: a failed coding job with a live successor stays ENQUEUED on the successor
_src = Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice").read_text()
check("L4: ENQUEUED failure branch consults autofix_successor before FAILED",
      _src.find("succ = autofix_successor(s[\"job_id\"])") != -1
      and _src.find("succ = autofix_successor(s[\"job_id\"])") < _src.find("dispatch {js} -- FAILED; dependents") and _src.find("succ = autofix_successor") > _src.find("js = job_status(s[\"job_id\"])"))

# --- M: auto-fix rounds are judged CUMULATIVELY from the chain root (01fdda651fba) --
GT2 = load("_gate_chain", Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py"))
_m = Path(tempfile.mkdtemp(prefix="chainbase-")) / "wt"
_m.mkdir(parents=True)
for _a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
    git(_m, *_a)
(_m / "t.txt").write_text("stub\n")
(_m / "verify.sh").write_text("grep -q 'CREATE TABLE \"Good\"' m.sql && grep -q 'id INT' m.sql && grep -q 'name TEXT' m.sql && echo VERIFY_OK || exit 1\n")
(_m / "TASK.md").write_text("## Scope\nOnly edit `t.txt`, `m.sql`; do not edit `verify.sh`.\n")
git(_m, "add", "."); git(_m, "commit", "-qm", "harness seal")
_root_head = git(_m, "rev-parse", "HEAD").stdout.strip()
(_m / "m.sql").write_text('CREATE TABLE "Gdoo" (\n  id INT,\n  name TEXT\n);\n')
git(_m, "add", "."); git(_m, "commit", "-qm", "auto: seal round baseline")
(_m / "m.sql").write_text('CREATE TABLE "Good" (\n  id INT,\n  name TEXT\n);\n')
_st = Path(tempfile.mkdtemp(prefix="chainstate-")) / "q.json"
_st.write_text(json.dumps({"jobs": [
    {"id": "r0", "auto_fix_round": 0, "auto_fix_root": "r0", "launch_baseline": {"head": _root_head}},
    {"id": "r1", "auto_fix_round": 1, "auto_fix_root": "r0"},
    {"id": "rx", "auto_fix_round": 1, "auto_fix_root": "r0x"}]}))
_cb = getattr(GT2, "autofix_chain_base", lambda *a, **k: "")
check("M1: auto-fix round resolves to the ROOT job's launch baseline",
      _cb("r1", _m, state_path=str(_st)) == _root_head)
check("M2: control -- the root job itself (round 0) gets no chain base", _cb("r0", _m, state_path=str(_st)) == "")
check("M3: control -- unknown root -> no chain base", _cb("rx", _m, state_path=str(_st)) == "")
_vr2 = os.environ.get("VR_SRC") or str(BIN / "verify-relevance.py")
_rb = subprocess.run([sys.executable, _vr2, str(_m), "--applied", "--json", "--base", _root_head],
                     capture_output=True, text=True, timeout=300)
try:
    _rbj = json.loads(_rb.stdout)
except ValueError:
    _rbj = {}
_gf = ((_rbj.get("generation") or {}).get("files") or {}).get("m.sql") or {}
check("M4: verify-relevance --base measures the CUMULATIVE file (all 4 added lines)",
      _gf.get("added_lines") == 4, (_rb.stdout + _rb.stderr)[-500:])
_rh = subprocess.run([sys.executable, _vr2, str(_m), "--applied", "--json"],
                     capture_output=True, text=True, timeout=300)
try:
    _rhj = json.loads(_rh.stdout)
except ValueError:
    _rhj = {}
check("M5: control -- without --base only the round delta (1 line) is seen",
      (((_rhj.get("generation") or {}).get("files") or {}).get("m.sql") or {}).get("added_lines") == 1,
      (_rh.stdout + _rh.stderr)[-500:])
check("M6: --base leaves the working tree as it was",
      (_m / "m.sql").read_text().startswith('CREATE TABLE "Good"')
      and git(_m, "status", "--porcelain").stdout.strip() == "M m.sql")
_gsrc = Path(os.environ.get("GATE_SRC") or BIN / "gate-on-complete.py").read_text()
check("M7: gate passes the chain base to the diff, relevance and review",
      "base=_chain_base)" in _gsrc and "a.task_file, base=_chain_base)" in _gsrc
      and _gsrc.count('payload.get("diff_base") or') == 3)

print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
