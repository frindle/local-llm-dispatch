#!/usr/bin/env python3
"""Unattended-readiness (2e): an IN-FLIGHT self-heal retry must not clobber a
hand-repaired harness, and an explicit hand-lock (.hand-harness) stops every
automatic retry/clear/re-author.

Replays chat-fixes3 s2 (2026-10-04): the slice FAILED, a detached advance began the
auto-retry holding status FAILED in memory, Main hand-wrote the harness and
--regate'd it (slice -> ENQUEUED on disk) -- and the driver cleared + re-authored.

  slicer  execute() FAILED branch: slice moved on disk mid-retry -> no clear
          bound_stale_worktree_retry: re-checks disk status / live job / lock /
            hand edits immediately before the clear -> aborts, nothing cleared
          PENDING branch: re-checks immediately before launching AUTO (the author
            job enqueue) -> nothing launched when the slice moved or is locked
          lock appearing while AUTO runs -> slice left PENDING, not FAILED
          hand-locked FAILED/PENDING slices are skipped entirely
          --regate moves past the lock (lock kept, git-ignored);
          --retry-slice discards it -- but NOT when launched by self-heal
  auto    dispatch_model refuses to enqueue an author/refine job into a locked or
          taken-elsewhere tree (exit 5, tree untouched)
  heal    dispatch-self-heal skips a hand-locked escalated slice; its detached
          launches carry DISPATCH_SELF_HEAL

SLICE_SRC / AUTO_SRC / HEAL_SRC=<path> for the revert-test.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice")
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
HEAL = Path(os.environ.get("HEAL_SRC") or BIN / "dispatch-self-heal.py")
FAILS = []
LOCK = ".hand-harness"


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    sys.path.insert(0, str(path.parent))
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    loader.exec_module(m)
    return m


def mkwt(root, name):
    wt = Path(root) / name
    wt.mkdir()
    (wt / "target.py").write_text("def f():\n    return 1\n")
    (wt / "test_fixture.py").write_text("assert True\n")
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, check=True)
    g("init", "-q")
    g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal")
    return str(wt)


class Died(Exception):
    pass


class Stop(Exception):
    pass


def slicer_tests(root):
    m = load(SLICE, "slice_hl")
    disk = {}
    removed, autos, lands, saved = [], [], [], []
    live = {}

    def _die(msg):
        raise Died(msg)
    m.die = _die
    m.read_state = lambda label: json.loads(json.dumps(disk)) if disk else None
    m.save_state = lambda st, *a, **k: saved.append(1)
    m.remove_worktree = lambda cwt, wt: removed.append(wt)
    m.slice_job_inflight = lambda st, sid: live.get(sid)
    m.slice_job_awaiting_gate = lambda *a, **k: None
    m.refresh_author_job_ids = lambda st, sid, s: 0
    m.ensure_chain_worktree = lambda st: os.path.join(root, "cwt")
    m.harvest_ready_slices = lambda st, cwt: []
    m.auto_land_passed = lambda st, cwt: None
    m.refresh_state = lambda st: st
    m._plan_cancelled = lambda label: None
    m.vacuous_gate_reason = lambda *a, **k: None
    m.any_dep_failed = lambda st, sid: False
    m.deps_done = lambda st, sid: True
    m.retry_guard = getattr(m, "retry_guard", None) and (lambda *a, **k: (True, None, []))
    m._land_green_slice = lambda *a, **k: (lands.append(1) or (False, "red"))
    m.request_advance = lambda label: None

    def _run_tee(cmd, *a, **k):
        autos.append(cmd)
        raise Stop()
    m.run_tee = _run_tee

    def mkst(status, wt, sid="s2"):
        s = {"status": status, "worktree": wt, "job_id": "jobold000001", "depends_on": [],
             "intent": "x", "title": "t", "must_contain": []}
        st = {"label": "hltest", "order": [sid], "slices": {sid: s}, "repo": root,
              "target": "target.py", "lang": "python", "dag_published": True}
        return st, s

    def execute(st):
        removed.clear(), autos.clear(), lands.clear()
        try:
            m.execute(st, "model", "host", 20, 600, "auto", None)
        except Stop:
            pass
        return (list(removed), len(autos))

    # A. THE EVIDENCE: FAILED in this process's memory, ENQUEUED (regated) on disk
    wt = mkwt(root, "a")
    st, s = mkst(m.FAILED, wt)
    disk.clear(); disk.update({"label": "hltest", "slices": {"s2": {"status": "enqueued",
                                                                     "job_id": "0a79390ccb8a"}}})
    chk("A: regated on disk mid-retry -> the in-flight driver clears NOTHING and "
        "launches no AUTO", execute(st), ([], 0))
    chk("A: ...the hand harness is still there", Path(wt, "test_fixture.py").exists(), True)

    # B. bound_stale_worktree_retry re-checks just before the clear
    for label, prep in (
            ("slice moved on disk", lambda: disk.update(
                {"slices": {"s2": {"status": "confirmed"}}})),
            ("live job", lambda: live.update({"s2": ("j9", "running", "auto-author-x")})),
            ("hand-lock", lambda: Path(wtb, LOCK).write_text("x"))):
        wtb = mkwt(root, "b-" + label.replace(" ", "-"))
        st, s = mkst(m.FAILED, wtb)
        disk.clear(); disk.update({"slices": {"s2": {"status": "failed"}}}); live.clear()
        prep()
        removed.clear()
        r = m.bound_stale_worktree_retry(st, "s2", s, wtb, "/cwt", "test")
        chk(f"B: {label} -> bound_stale_worktree_retry does not clear (returns False)",
            (r, removed), (False, []))
    live.clear()
    wtb = mkwt(root, "b-control")
    st, s = mkst(m.FAILED, wtb)
    disk.clear(); disk.update({"slices": {"s2": {"status": "failed"}}})
    removed.clear()
    chk("B: control (nothing changed) -> it still clears", (m.bound_stale_worktree_retry(
        st, "s2", s, wtb, "/cwt", "test"), removed), (True, [wtb]))

    # C. PENDING branch re-checks immediately before launching AUTO
    st, s = mkst(m.PENDING, os.path.join(root, "c-none"))
    s["worktree"] = None
    m.slice_worktree = lambda st, sid: os.path.join(root, "c-none")
    disk.clear(); disk.update({"slices": {"s2": {"status": "confirmed"}}})
    chk("C: slice taken on disk -> AUTO (author-job enqueue) NOT launched",
        execute(st), ([], 0))
    disk.clear(); disk.update({"slices": {"s2": {"status": "pending"}}})
    st, s = mkst(m.PENDING, None)
    chk("C: control -> AUTO launched", execute(st)[1], 1)

    # D. hand-locked FAILED / PENDING slices are skipped outright
    for status in (m.FAILED, m.PENDING):
        wtd = mkwt(root, "d-" + status)
        Path(wtd, LOCK).write_text("x")
        m.slice_worktree = lambda st, sid, _w=wtd: _w
        st, s = mkst(status, wtd)
        disk.clear(); disk.update({"slices": {"s2": {"status": status}}})
        got = execute(st)
        chk(f"D: hand-locked {status} slice -> no land, no clear, no AUTO",
            (got, len(lands)), (([], 0), 0))

    # E. lock placed WHILE AUTO runs -> PENDING, not FAILED
    wte = os.path.join(root, "e")
    m.slice_worktree = lambda st, sid: wte
    st, s = mkst(m.PENDING, None)
    disk.clear(); disk.update({"slices": {"s2": {"status": "pending"}}})

    def _run_tee_lock(cmd, *a, **k):
        os.makedirs(wte, exist_ok=True)
        Path(wte, LOCK).write_text("x")
        return types.SimpleNamespace(returncode=5, stdout="[auto] NOT enqueuing: HAND-LOCKED")
    m.run_tee = _run_tee_lock
    try:
        m.execute(st, "model", "host", 20, 600, "auto", None)
    except Stop:
        pass
    chk("E: lock appeared mid-authoring -> slice left PENDING, not FAILED/ESCALATED",
        s["status"], m.PENDING)

    # F. --retry-slice: human discards; self-heal may not
    wtf = mkwt(root, "f")
    Path(wtf, LOCK).write_text("x")
    st, s = mkst(m.FAILED, wtf)
    os.environ["DISPATCH_SELF_HEAL"] = "1"
    removed.clear()
    try:
        m.retry_slice(st, "s2")
        r = "retried"
    except Died:
        r = "refused"
    chk("F: automated (self-heal) --retry-slice of a hand-locked tree -> refused, kept",
        (r, removed), ("refused", []))
    del os.environ["DISPATCH_SELF_HEAL"]
    removed.clear()
    try:
        m.retry_slice(st, "s2")
        r = "retried"
    except Died:
        r = "refused"
    chk("F: a human --retry-slice still discards it", (r, removed), ("retried", [wtf]))

    # G. --regate moves past the lock, keeps it, hides it from git
    wtg = mkwt(root, "g")
    Path(wtg, LOCK).write_text("x")
    st, s = mkst(m.FAILED, wtg)
    enq = []
    m.confirm_and_enqueue = lambda st, sid, s, wt, *a, **k: enq.append(wt)
    try:
        m.regate_slice(st, "s2", "model", "host", 20, 600, "auto", None)
    except Died as e:
        enq.append(f"died: {e}")
    porcelain = subprocess.run(["git", "-C", wtg, "status", "--porcelain"],
                               capture_output=True, text=True).stdout
    chk("G: --regate goes past the lock (enqueued), the lock stays, git does not see it",
        (enq, Path(wtg, LOCK).exists(), LOCK in porcelain), ([wtg], True, False))


def auto_tests(root):
    a_mod = load(AUTO, "auto_hl")
    calls = []
    class Enqueued(Exception):
        pass

    def _capture(cmd, *x, **k):        # record the enqueue and stop (never poll a job)
        calls.append(cmd)
        raise Enqueued()
    a_mod.capture = _capture
    wt = Path(mkwt(root, "auto"))
    a = types.SimpleNamespace(drafter_cmd=None, slice_plan=None, slice_id=None, model="m",
                              host="h", num_ctx=1, author_max_iters=1, max_tokens=None,
                              lang="python", bundle=None, timeout=1)
    (wt / LOCK).write_text("x")
    try:
        a_mod.dispatch_model(wt, "prompt", "auto-author-x", "true", a)
        got = "returned"
    except SystemExit as e:
        got = e.code
    except Enqueued:
        got = "ENQUEUED"
    chk("auto: hand-locked tree -> refuses to enqueue the author job (exit 5)",
        (got, calls), (5, []))
    (wt / LOCK).unlink()
    calls.clear()
    for _f in ("AUTO-TASK.md",):
        (wt / _f).unlink(missing_ok=True)
    a_mod.slice_taken_elsewhere = lambda plan, sid: "enqueued"
    a.slice_plan, a.slice_id = "p", "s2"
    try:
        a_mod.dispatch_model(wt, "prompt", "auto-refine-x-r2", "true", a)
        got = "returned"
    except SystemExit as e:
        got = e.code
    except Enqueued:
        got = "ENQUEUED"
    calls_n = len(calls)
    chk("auto: slice taken elsewhere (e.g. --regate) -> refuses to enqueue (exit 5)",
        (got, calls_n), (5, 0))
    chk("auto: ...and left the tree untouched (no AUTO-TASK.md written)",
        (wt / "AUTO-TASK.md").exists(), False)


def heal_tests(root):
    h = load(HEAL, "heal_hl")
    runs = Path(root) / "runs"
    runs.mkdir()
    wt = mkwt(root, "heal")
    Path(wt, LOCK).write_text("x")
    (runs / "hp.json").write_text(json.dumps({"label": "hp", "slices": {"s2": {
        "status": "escalated", "worktree": wt,
        "escalation_reason": "authoring failed DETERMINISTICALLY (AUTO rc=1)"}}}))
    launched = []
    got = h.heal("hp", "s2", review_text="VERDICT: (c) harness defect", slice_runs=str(runs),
                 ledger_path=str(Path(root) / "ledger.json"),
                 launch=lambda *a, **k: launched.append(a))
    chk("heal: a hand-locked escalated slice is skipped, nothing launched",
        (str(got), launched), ("skip:hand-locked", []))
    envs = []

    class FakePopen:
        def __init__(self, *a, **k):
            envs.append((k.get("env") or {}).get("DISPATCH_SELF_HEAL"))
            self.pid = 1
    _real = h.subprocess.Popen
    h.subprocess.Popen = FakePopen          # the shared module: restore it after
    try:
        h._slicer_detached(Path(root) / "plan.json", ["--retry-slice", "s2"],
                           str(Path(root) / "heal.log"))
    finally:
        h.subprocess.Popen = _real
    chk("heal: detached slicer launches carry DISPATCH_SELF_HEAL=1", envs, ["1"])


def main():
    root = tempfile.mkdtemp(prefix="hl-test-")
    try:
        slicer_tests(root)
        auto_tests(root)
        heal_tests(root)
    finally:
        subprocess.run(["rm", "-rf", root])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
