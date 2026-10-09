#!/usr/bin/env python3
"""Behavioural tests for the 2026-10-05 rt-bg-commitments-fix gaps.

  (A) auto-runs/<bundle>.json is SHARED by every dispatch-auto run in a bundle. The
      sync-guard run's `ended` record overwrote the getcommitments run's record. Now
      each run owns runs[<label>] under a lock, and the top-level fields (the only
      ones the queue daemon reads) mirror the most active run. Attempt histories are
      per run.
  (B) A converged harness had no entry point back into preflight: the driver had
      exited at the park, and a re-run re-scaffolds. Now --resume-harness skips
      scaffold and authoring, re-proves VERIFY_OK, and continues at the preflight
      loop. The driver records its argv so that self-heal can relaunch it.
  (C) self-heal --job-sweep: when a continuation passes, it resolves the row AND
      relaunches the driver with --resume-harness. Idempotent and capped; it never
      launches under DISPATCH_VERIFY_SANDBOX, and never while a driver is alive.
  (D) A GO-paused run was invisible. It now leaves ONE open READY-TO-LAND row, which
      the janitor closes when the coding dispatch for that label is enqueued or the
      worktree is gone.

Nothing real is enqueued or launched: every launcher/resolve is a stub, and all
paths are temp dirs. Revert check: point AUTO_SRC / HEAL_SRC / JANITOR_SRC at the
.bak files; the checks for the reverted file must FAIL.
"""
import importlib.util
import json
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
HEAL = Path(os.environ.get("HEAL_SRC") or HERE / "dispatch-self-heal.py")
JAN = Path(os.environ.get("JANITOR_SRC") or HERE / "escalation_index_janitor.py")
QUEUE = HERE / "ollama-queue.py"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"

FAILS, N = [], [0]


def check(name, got, want=True):
    N[0] += 1
    ok = got == want
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def A(m, **kw):
    a = SimpleNamespace(label="rt-x-getc", bundle="rt-x", lang="ts", intent="i", interface=None,
                        model="m", host="studio", require=[], drafter_cmd=None, dest=None,
                        slice_plan=None, slice_id=None, no_park=False, new_project=None,
                        repo="/nonexistent", target="lib/x.ts", resume_harness=False,
                        num_ctx=65536, no_auto_slice=True, auto_slice=False,
                        allow_multi_module=True, kind="symbol", anchor=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_chain_record(m, q, tmp):
    print("-- (A) shared bundle chain record")
    d = tmp / "runs-a"
    me = os.getpid()
    alive = lambda pid: pid == me
    getc = A(m, label="rt-x-getc")
    sync = A(m, label="rt-x-sync")
    try:
        m.chain_state_write(getc, "advancing", step="preflight r1", runs_dir=d,
                            now="2026-10-05T21:30:00Z", alive=alive)
        m.chain_state_write(sync, "ended", outcome="exit 0", runs_dir=d,
                            now="2026-10-05T21:35:00Z", alive=alive)
    except TypeError:
        m.chain_state_write(getc, "advancing", step="preflight r1", runs_dir=d,
                            now="2026-10-05T21:30:00Z")
        m.chain_state_write(sync, "ended", outcome="exit 0", runs_dir=d,
                            now="2026-10-05T21:35:00Z")
    doc = json.loads((d / "rt-x.json").read_text())
    check("a later `ended` from ANOTHER run does not erase the live run's record",
          ((doc.get("runs") or {}).get("rt-x-getc") or {}).get("phase"), "advancing")
    check("top-level mirrors the live run (what the daemon reads)",
          (doc.get("label"), doc.get("phase"), doc.get("pid")), ("rt-x-getc", "advancing", me))
    prog = q.chain_run_progress("rt-x", runs_dir=d, alive=alive,
                                now=q._parse_iso_ts("2026-10-05T21:36:00Z"))
    check("the UNCHANGED queue reader still sees the bundle's driver as live",
          prog.get("driver_live"), True)
    try:
        m.chain_state_write(getc, "ended", outcome="exit 0", runs_dir=d,
                            now="2026-10-05T21:40:00Z", alive=alive)
    except TypeError:
        m.chain_state_write(getc, "ended", outcome="exit 0", runs_dir=d,
                            now="2026-10-05T21:40:00Z")
    doc = json.loads((d / "rt-x.json").read_text())
    check("once every run ended, top-level is the newest ended run",
          (doc.get("phase"), doc.get("label")), ("ended", "rt-x-getc"))
    check("both runs' rounds are kept",
          sorted((doc.get("runs") or {}).keys()), ["rt-x-getc", "rt-x-sync"])
    m.AUTO_RUNS_DIR = d
    check("attempt histories are per run inside a bundle",
          m.attempts_path(getc) != m.attempts_path(sync), True)


def test_resume_check(m, tmp):
    print("-- (B) --resume-harness precondition + entry point")
    wt = tmp / "wt"
    ok, why = m.resume_harness_check(wt, "lib/x.ts", "ts", check=lambda: (0, "VERIFY_OK"),
                                     reset=lambda w, t: [])
    check("refused: no worktree", ok, False)
    wt.mkdir()
    (wt / "auto-harness-check.py").write_text("# stub\n")
    ok, why = m.resume_harness_check(wt, "lib/x.ts", "ts", check=lambda: (0, "VERIFY_OK"),
                                     reset=lambda w, t: [])
    check("refused: no authored harness", (ok, "missing" in why), (False, True))
    for f in ("TASK.md", "refimpl.py", "verify.sh"):
        (wt / f).write_text("x\n")
    resets = []
    ok, why = m.resume_harness_check(wt, "lib/x.ts", "ts", check=lambda: (1, "FAIL: case 5"),
                                     reset=lambda w, t: resets.append(t) or [])
    check("refused: harness does not self-check", ok, False)
    ok, why = m.resume_harness_check(wt, "lib/x.ts", "ts", check=lambda: (0, "x\nVERIFY_OK: y"),
                                     reset=lambda w, t: resets.append(t) or [])
    check("accepted: VERIFY_OK", ok, True)
    check("the target is reset to HEAD before the self-check", resets, ["lib/x.ts", "lib/x.ts"])

    # do_auto --resume-harness: never scaffolds/authors, enters the preflight loop
    calls = []
    m.do_scaffold = lambda *x, **k: calls.append("scaffold")
    m._author_with_continuations = lambda *x, **k: calls.append("author") or (False, "x")
    m.resume_harness_check = lambda *x, **k: (True, "harness self-check VERIFY_OK")
    m._preflight_loop = lambda a, w, t, v: calls.append(("preflight", str(w), t, v)) or 7
    m.chain_state_write = lambda *x, **k: None
    m.record_argv = lambda *x, **k: calls.append("argv")
    m.AUTO_RUNS_DIR = tmp / "runs-b"
    a = A(m, resume_harness=True, dest=str(wt), repo=str(tmp), lang="ts")
    try:
        rc = m.do_auto(a)
    except SystemExit as e:
        rc = ("exit", e.code)
    check("do_auto --resume-harness returns the preflight loop's result", rc, 7)
    check("... without scaffolding or authoring",
          [c for c in calls if c in ("scaffold", "author")], [])
    check("... entering preflight on the existing worktree with the auto self-check",
          [c for c in calls if isinstance(c, tuple)],
          [("preflight", str(wt.resolve()), "lib/x.ts", "python3 auto-harness-check.py")])


def test_argv(tmp):
    print("-- (B) argv record")
    m = load(AUTO, "oda_argv")
    d = tmp / "runs-c"
    a = A(m, label="rt-x-getc")
    fp = m.record_argv(a, "/w", argv=["--repo", "/r", "--label", "rt-x-getc",
                                      "--resume-harness"], runs_dir=d)
    rec = json.loads(Path(fp).read_text())
    check("argv is recorded per label without --resume-harness",
          rec["argv"], ["--repo", "/r", "--label", "rt-x-getc"])


def test_unmark(m, tmp):
    print("-- (B) resume strips ONLY a previous GO's marker stamp")
    import subprocess as sp
    wt = tmp / "wt-mark"
    wt.mkdir()
    body = "import { test } from 'node:test';\ntest('a', () => {});\n"
    (wt / "verify.test.ts").write_text(body)
    for c in (["git", "init", "-q"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
              ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal"]):
        sp.run(c, cwd=wt, capture_output=True)
    (wt / "verify.test.ts").write_text(
        "import { test } from 'node:test';\n// Model-drafted; NOT yet read by a human.\n"
        "const DRAFT_UNCONFIRMED = true;\n\ntest('a', () => {});\n")
    out = m.unmark_go_stamp(wt)
    check("a marker-only diff is restored to the sealed HEAD",
          (len(out), (wt / "verify.test.ts").read_text()), (1, body))
    (wt / "verify.test.ts").write_text(
        "import { test } from 'node:test';\n// Model-drafted; NOT yet read by a human.\n"
        "const DRAFT_UNCONFIRMED = true;\ntest('a', () => {});\ntest('b', () => {});\n")
    out = m.unmark_go_stamp(wt)
    check("a marker + REAL edit is left alone",
          (out, "test('b'" in (wt / "verify.test.ts").read_text()), ([], True))


def test_go_surface(m, jan, tmp):
    print("-- (D) GO is surfaced and closed by the janitor")
    md = tmp / "READY.md"
    wt = tmp / "wt-go"
    wt.mkdir()
    a = A(m, label="rt-x-sync")
    rel = {"killed": 1, "evidence_mutants": 1, "score": 1.0}
    r1 = m.surface_go(a, wt, rel, ready_md=md, now="2026-10-05T21:35:37Z")
    r2 = m.surface_go(a, wt, rel, ready_md=md, now="2026-10-05T21:40:00Z")
    rows = [l for l in md.read_text().splitlines() if l.startswith("- [ ] ")]
    check("one open row, deduped", (bool(r1), r2, len(rows)), (True, None, 1))
    check("slicer/no-park runs do not surface",
          m.surface_go(A(m, label="z", no_park=True), wt, rel, ready_md=md), None)
    qs = tmp / "qstate.json"
    qs.write_text(json.dumps({"jobs": [{"id": "aaaaaaaaaaaa", "label": "other",
                                        "status": "running"}]}))
    f = jan.Facts(runs_dir=tmp / "nr", plans_dir=tmp / "np", queue_state=qs,
                  cancelled=lambda p: False)
    closed = jan.run([md], facts=f, apply=True, log_path=tmp / "j.log", out=lambda *x: None)
    check("janitor keeps it open while no coding dispatch exists", len(closed), 0)
    qs.write_text(json.dumps({"jobs": [{"id": "bbbbbbbbbbbb", "label": "rt-x-sync",
                                        "status": "pending"}]}))
    f = jan.Facts(runs_dir=tmp / "nr", plans_dir=tmp / "np", queue_state=qs,
                  cancelled=lambda p: False)
    closed = jan.run([md], facts=f, apply=True, log_path=tmp / "j.log", out=lambda *x: None)
    check("janitor closes it once --label rt-x-sync is enqueued", len(closed), 1)
    md2 = tmp / "READY2.md"
    m.surface_go(A(m, label="gone"), tmp / "wt-gone", rel, ready_md=md2)
    f = jan.Facts(runs_dir=tmp / "nr", plans_dir=tmp / "np", queue_state=qs,
                  cancelled=lambda p: False)
    closed = jan.run([md2], facts=f, apply=True, log_path=tmp / "j.log", out=lambda *x: None)
    check("janitor closes it when the worktree is gone", len(closed), 1)


def test_heal_resume(h, tmp):
    print("-- (C) self-heal relaunches the driver on a passed continuation")
    runs = tmp / "runs-h"
    (runs / "argv").mkdir(parents=True)
    (runs / "argv" / "rt-x-getc.json").write_text(json.dumps(
        {"label": "rt-x-getc", "argv": ["--repo", "/r", "--label", "rt-x-getc"],
         "cwd": str(tmp)}))
    h.AUTO_RUNS = runs
    led = tmp / "self-heal.json"
    dec = tmp / "decisions.jsonl"
    stuck = {"id": "c1c1c1c1c1c1", "label": "auto-author-rt-x-getc-c1", "status": "needs_opus",
             "bundle": "rt-x", "cwd": str(tmp)}
    passed = {"id": "c2c2c2c2c2c2", "label": "auto-author-rt-x-getc-c2", "status": "done",
              "exit_code": 0, "continues": "c1c1c1c1c1c1", "bundle": "rt-x", "cwd": str(tmp)}
    launched = []
    launch = lambda cmd, cwd, log: launched.append(cmd) or 4242
    resolved = []

    def resume(j, cid, **kw):
        kw.pop("slice_runs", None)
        return h.resume_auto_driver(j, cid, runs_dir=runs, launch=launch, alive=lambda p: False,
                                    slice_runs=tmp / "nosr", notifier=lambda *x, **k: None, **kw)
    try:
        out = h.job_heal_sweep(jobs=[stuck, passed], ledger_path=led, slice_runs=tmp / "nosr",
                               resolve=lambda i: resolved.append(i) or True, log_dir=tmp / "nolog",
                               decisions=dec, sidecars=[], resume=resume)
    except TypeError:
        out = h.job_heal_sweep(jobs=[stuck, passed], ledger_path=led, slice_runs=tmp / "nosr",
                               resolve=lambda i: resolved.append(i) or True, log_dir=tmp / "nolog",
                               decisions=dec, sidecars=[])
    check("the needs_opus row is resolved", resolved, ["c1c1c1c1c1c1"])
    check("the driver is relaunched once with the recorded argv + --resume-harness",
          [c[2:] for c in launched], [["--repo", "/r", "--label", "rt-x-getc", "--resume-harness"]])
    if not hasattr(h, "resume_auto_driver"):
        for n in ("idempotent", "live driver", "cap", "sandbox", "no argv"):
            check("resume: " + n, False, True)
        return
    act = resume(stuck, "c2c2c2c2c2c2", ledger_path=led, decisions=dec)
    check("idempotent per passed continuation", (act.startswith("skip:already"), len(launched)),
          (True, 1))
    (runs / "rt-x.json").write_text(json.dumps({"runs": {"rt-x-getc": {
        "label": "rt-x-getc", "phase": "advancing", "pid": 99}}}))
    act = h.resume_auto_driver(stuck, "c3", runs_dir=runs, launch=launch, ledger_path=led,
                               decisions=dec, alive=lambda p: p == 99, slice_runs=tmp / "nosr")
    check("never while a driver for that run is alive", act.startswith("skip:driver"), True)
    (runs / "rt-x.json").unlink()
    act = h.resume_auto_driver(stuck, "c4", runs_dir=runs, launch=launch, ledger_path=led,
                               decisions=dec, alive=lambda p: False, slice_runs=tmp / "nosr")
    check("second resume today is allowed (cap 2/label/day)", act.startswith("resumed:"), True)
    alerts = []
    act = h.resume_auto_driver(stuck, "c5", runs_dir=runs, launch=launch, ledger_path=led,
                               decisions=dec, alive=lambda p: False, slice_runs=tmp / "nosr",
                               notifier=lambda *x, **k: alerts.append(k.get("dedupe_key")))
    check("the third is capped, with ONE deduped alert",
          (act.startswith("park:"), alerts, len(launched)), (True, ["resume-cap:rt-x-getc"], 2))
    act = h.resume_auto_driver(dict(stuck, label="auto-author-rt-y-c1"), "c9", runs_dir=runs,
                               ledger_path=led, decisions=dec, alive=lambda p: False,
                               slice_runs=tmp / "nosr")
    check("no recorded argv -> nothing launched", act.startswith("skip:no recorded argv"), True)
    (runs / "argv" / "rt-y.json").write_text(json.dumps({"label": "rt-y", "argv": ["--x"]}))
    act = h.resume_auto_driver(dict(stuck, label="auto-author-rt-y-c1"), "c9", runs_dir=runs,
                               ledger_path=led, decisions=dec, alive=lambda p: False,
                               slice_runs=tmp / "nosr")
    check("DISPATCH_VERIFY_SANDBOX=1 never launches a real driver",
          act.startswith("skip:DISPATCH_VERIFY_SANDBOX"), True)
    check("no claude/opus anywhere in the resume path",
          any(w in (Path(HEAL).read_text().split("def resume_auto_driver")[1]
                    .split("def job_heal_sweep")[0]).lower().replace("needs_opus", "")
              for w in ("claude", "opus")), False)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="auto-resume-"))
    m = load(AUTO, "oda_resume")
    q = load(QUEUE, "oq_resume")
    test_chain_record(m, q, tmp)
    test_argv(tmp)
    m2 = load(AUTO, "oda_resume2")
    try:
        test_go_surface(m2, load(JAN, "jan_resume"), tmp)
    except AttributeError as e:
        check("GO surfacing exists: %s" % e, False, True)
    try:
        test_unmark(load(AUTO, "oda_unmark"), tmp)
    except AttributeError as e:
        check("marker strip exists: %s" % e, False, True)
    try:
        test_resume_check(m, tmp)
    except AttributeError as e:
        check("resume entry point exists: %s" % e, False, True)
    h = load(HEAL, "heal_resume")
    h.HEAL_LEDGER = tmp / "self-heal.json"
    h.DECISIONS = tmp / "decisions.jsonl"
    test_heal_resume(h, tmp)
    print(f"\n{N[0] - len(FAILS)}/{N[0]} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
