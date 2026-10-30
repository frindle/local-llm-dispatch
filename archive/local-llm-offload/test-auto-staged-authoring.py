#!/usr/bin/env python3
"""Wave 3 (2026-10-09): staged authoring + non-attempt accounting + task-class/lane plumbing.

Pins, in ollama-dispatch-auto:
  1. record_attempt / park_visible / should_escalate do NOT count a job whose failure_class is
     'operator' or whose terminal_reason is force_stopped / cancelled (queue dbb5fbf parity);
  2. stage_check (TASK.md / fixture / refimpl) and the `--stage-check` CLI verify command;
  3. _author_staged: stage ordering TASK -> fixture -> refimpl, ONE queue job per file, the
     no-write guard (a job that wrote nothing, or whose log shows a PROSE RUNAWAY, is re-prompted
     with a file-writing instruction), operator stops are never re-prompted, exhaustion falls back
     to the legacy ladder on the same tree;
  4. task_class_for records the class in .dispatch-harness.json; lane_enqueue_args only emits
     --mode when --lane != open AND the queue supports it.
AUTO_SRC overrides the file under test (task_class.py is read from its directory)."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
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


def mk_a(tmp, **kw):
    d = dict(label="t-stage", bundle=None, lang="python", intent="Add f(x) to app/x.py that doubles x",
             interface="", require=["def f("], drafter_cmd=None, author_max_iters=24, staged="on",
             task_class="auto", lane="open", edit_file=(), model="m", host="h", num_ctx=1, max_tokens=0,
             timeout=5, repo=str(tmp), new_project=None, dest=None, target="app/x.py")
    d.update(kw)
    return SimpleNamespace(**d)


def make_wt(tmp):
    wt = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp))
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / "TASK.md").write_text("# T\n## Must contain\n- `TODO`\nOnly edit `app/x.py`\nTODO\n")
    (wt / "test_fixture.py").write_text("def test_x():\n    assert 0, 'SCAFFOLD_INCOMPLETE'\n" + "# padding line to exceed the empty-file floor\n" * 3)
    (wt / "refimpl.py").write_text("# TODO -- the exact text to replace\n")
    (wt / "app").mkdir()
    (wt / "app" / "x.py").write_text("def g():\n    return 1\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "app/x.py", "lang": "python"}))
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
                   check=True)
    return wt


GOOD_TASK = "# T\n## Must contain\n- `def f(`\nOnly edit `app/x.py`\n"
GOOD_FX = "def test_double():\n    from app.x import f\n    assert f(2) == 4\n    assert f(0) == 0\n    assert f(-1) == -2\n"
GOOD_REF = "p='app/x.py'\nt=open(p).read()\nopen(p,'w').write(t+'\\ndef f(x):\\n    return 2*x\\n')\n"


def main():
    m = load(AUTO, "auto_staged")
    tmp = Path(tempfile.mkdtemp(prefix="staged-"))
    m.AUTO_RUNS_DIR = tmp / "runs"
    m.chain_state_write = lambda *a, **k: None

    # ---- 1. non-attempt accounting ------------------------------------------------------------
    chk("operator failure_class is not an attempt", m.is_non_attempt("operator", None, "failed"), True)
    chk("force_stopped is not an attempt", m.is_non_attempt(None, "force_stopped", "failed"), True)
    chk("cancelled terminal_reason is not an attempt", m.is_non_attempt(None, "cancelled", "failed"), True)
    chk("cancelled status is not an attempt", m.is_non_attempt(None, None, "cancelled"), True)
    chk("a model failure IS an attempt", m.is_non_attempt("model", "iteration_cap", "failed"), False)
    chk("no info IS an attempt", m.is_non_attempt(None, None, None), False)
    a = mk_a(tmp)
    a._last_job_outcome = {"job": "j1", "status": "failed", "failure_class": "operator",
                           "terminal_reason": "force_stopped"}
    chk("record_attempt skips a force_stopped job", m.record_attempt(a, "j1", "job j1 failed", "chk"), None)
    chk("...and writes nothing", m.load_attempts(a), [])
    a._last_job_outcome = {"job": "jOTHER", "status": "failed", "failure_class": "operator"}
    chk("an outcome for a DIFFERENT job does not suppress this one",
        bool(m.record_attempt(a, "j2", "job j2 failed", "chk")), True)
    a._last_job_outcome = None
    m.clear_attempts(a)
    m.record_attempt(a, "j3", "job j3 failed (exit 1)", "x")
    m.record_attempt(a, "j4", "job j4 failed (exit 1)", "x")
    chk("two real same-class failures escalate", m.should_escalate(m.load_attempts(a), pid=1), True)
    legacy = m.load_attempts(a)
    legacy[-1]["interrupted"] = True
    chk("...but an interrupted-flagged record does not count toward the pair",
        m.should_escalate(legacy, pid=1), False)
    calls = []
    a._chain_last_job = "j9"
    a._last_job_outcome = {"job": "j9", "status": "failed", "failure_class": "operator",
                           "terminal_reason": "force_stopped"}
    rec = m.park_visible(a, 1, "job j9 failed", runs_dir=tmp / "runs", _run=lambda c: calls.append(c) or (0, "", ""),
                         log="/tmp/x.log")
    chk("park_visible on an operator stop does NOT escalate to needs_opus", calls, [])
    chk("...and says why", rec.get("skipped"), "operator-stop")
    a._last_job_outcome = {"job": "j9", "status": "failed", "failure_class": "model", "terminal_reason": "x"}
    m.park_visible(a, 1, "job j9 failed", runs_dir=tmp / "runs", _run=lambda c: calls.append(c) or (0, "", ""),
                   log="/tmp/x.log")
    chk("park_visible on a real failure still escalates", bool(calls), True)

    # the REAL poller records how a job ended (dispatch_model), so record_attempt can skip operator stops
    wtp = make_wt(tmp)
    ap = mk_a(tmp, label="t-poll")
    m._stop_for_hand = lambda *x, **k: None
    m.capture = lambda cmd, **k: (0, "enqueued abc123def456\n", "")
    m.driver_gone = lambda a: None
    m.await_gate_tree_release = lambda *x: None
    m.time.sleep = lambda s: None
    for row, tag in (({"status": "failed", "exit_code": 3, "terminal_reason": "force_stopped",
                       "failure_class": "operator", "log_path": ""}, "operator"),
                     ({"status": "failed", "exit_code": 1, "terminal_reason": "iteration_cap",
                       "failure_class": "model", "log_path": ""}, "model")):
        m.job_row = lambda j, row=row: row
        okp, whyp = m.dispatch_model(wtp, "prompt", "auto-author-t-poll", "true", ap)
        out = ap._last_job_outcome or {}
        chk(f"poller records the {tag} job outcome", (okp, out.get("failure_class"), out.get("terminal_reason")),
            (False, row["failure_class"], row["terminal_reason"]))
        rp = m.record_attempt(ap, "abc123def456", whyp, "chk")
        chk(f"...so record_attempt {'skips' if tag == 'operator' else 'counts'} it", rp is None, tag == "operator")
    m.clear_attempts(ap)

    # ---- 2. stage checks ----------------------------------------------------------------------
    wt = make_wt(tmp)
    req = ["def f("]
    chk("task stage fails on scaffold TODOs", m.stage_check("task", wt, "python", "app/x.py", req)[0], False)
    (wt / "TASK.md").write_text(GOOD_TASK)
    chk("task stage passes on a complete TASK.md", m.stage_check("task", wt, "python", "app/x.py", req)[0], True)
    chk("task stage fails when a required literal is absent",
        m.stage_check("task", wt, "python", "app/x.py", ["def zzz("])[0], False)
    chk("fixture stage fails on the SCAFFOLD guard", m.stage_check("fixture", wt, "python", "app/x.py")[0], False)
    (wt / "test_fixture.py").write_text(GOOD_FX)
    chk("fixture stage passes on real cases", m.stage_check("fixture", wt, "python", "app/x.py")[0], True)
    (wt / "test_fixture.py").write_text("def test_x(:\n  pass\n" + " " * 90)
    chk("fixture stage fails on a syntax error", m.stage_check("fixture", wt, "python", "app/x.py")[0], False)
    chk("refimpl stage fails on the STUB", m.stage_check("refimpl", wt, "python", "app/x.py")[0], False)
    (wt / "refimpl.py").write_text(GOOD_REF)
    chk("refimpl stage passes on real code", m.stage_check("refimpl", wt, "python", "app/x.py")[0], True)
    chk("stage order is task, fixture, refimpl", m.STAGE_ORDER, ("task", "fixture", "refimpl"))
    chk("a test-file target skips the (locked) fixture stage",
        m.stage_plan(mk_a(tmp), "tests/test_x.py"), ["task", "refimpl"])
    chk("the last stage's verify is the FULL self-check",
        m.stage_verify_cmd("refimpl", mk_a(tmp), "app/x.py"), "python3 auto-harness-check.py")
    vc = m.stage_verify_cmd("task", mk_a(tmp), "app/x.py")
    chk("early stage verify is the --stage-check CLI", "--stage-check task" in vc and "--require=def f(" in vc.replace("'", ""), True)
    wt2 = make_wt(tmp)
    (wt2 / "TASK.md").write_text(GOOD_TASK)
    r = subprocess.run(vc, shell=True, cwd=wt2, capture_output=True, text=True,
                       env=dict(os.environ))
    # the command embeds the real ~/bin path; run the file under test instead
    r = subprocess.run([sys.executable, str(AUTO), "--stage-check", "task", "--lang", "python",
                        "--target", "app/x.py", "--require=def f("], cwd=wt2, capture_output=True, text=True)
    chk("--stage-check CLI exits 0 on a good TASK.md", (r.returncode, r.stdout.startswith("STAGE_OK")), (0, True))
    (wt2 / "TASK.md").write_text("TODO\n")
    r = subprocess.run([sys.executable, str(AUTO), "--stage-check", "task"], cwd=wt2, capture_output=True, text=True)
    chk("--stage-check CLI exits 1 on a bad TASK.md", (r.returncode, r.stdout.startswith("STAGE_FAIL")), (1, True))

    # ---- 2b. staged prompt must not carry the contradictory ORDER OF WORK (job 5be45b107cea) ---
    _ap = mk_a(tmp)
    _sp = {st: m.stage_prompt(_ap, "app/x.py", st, None) for st in ("task", "fixture", "refimpl")}
    chk("stage prompt drops the 'FIRST write refimpl.py' hard rule",
        any("FIRST write `refimpl.py`" in v for v in _sp.values()), False)
    chk("task stage prompt scopes ORDER OF WORK to TASK.md and bans the full self-check",
        ("## ORDER OF WORK (this staged job)" in _sp["task"] and "Write ONLY `TASK.md`" in _sp["task"]
         and "Do NOT run the full `auto-harness-check.py`" in _sp["task"]), True)
    chk("refimpl stage prompt tells the model to run the full self-check",
        "Run `python3 auto-harness-check.py` when done" in _sp["refimpl"], True)
    chk("the rest of the authoring body survives the ORDER OF WORK swap",
        "## 1. `refimpl.py`" in _sp["task"], True)

    # ---- 3. _author_staged --------------------------------------------------------------------
    def run_staged(script, a=None):
        """script: list of per-job callables(wt, prompt) -> (ok, why, side-effect writes)"""
        a = a or mk_a(tmp, label="t-" + str(len(script)) + str(id(script) % 997))
        wt = make_wt(tmp)
        jobs = []

        def fake_dispatch(wt_, prompt, label, verify_cmd, a_, max_iters=None):
            i = len(jobs)
            jobs.append({"label": label, "prompt": prompt, "verify": verify_cmd, "iters": max_iters})
            step = script[i] if i < len(script) else (lambda w, p: (False, "job x failed (exit 1)", None))
            ok, why, log = step(Path(wt_), prompt)
            a_._chain_last_job = f"job{i}"
            a_._last_job_outcome = (None if ok else {"job": f"job{i}", "status": "failed", "failure_class": "model",
                                                      "terminal_reason": "iteration_cap", "log_path": log})
            return ok, why
        m.dispatch_model = fake_dispatch
        res = m._author_staged(a, wt, "app/x.py", "modify")
        return res, jobs, wt

    def write(name, text):
        def f(w, p):
            (w / name).write_text(text)
            return True, "done", None
        return f

    res, jobs, _ = run_staged([write("TASK.md", GOOD_TASK), write("test_fixture.py", GOOD_FX),
                               write("refimpl.py", GOOD_REF)])
    chk("clean staged run converges", res[0], True)
    chk("exactly ONE job per file", [j["label"][-3:] for j in jobs], ["-s1", "-s2", "-s3"])
    chk("jobs run in TASK -> fixture -> refimpl order",
        ["write ONLY `TASK.md`" in jobs[0]["prompt"], "write ONLY `test_fixture.py`" in jobs[1]["prompt"],
         "write ONLY `refimpl.py`" in jobs[2]["prompt"]], [True, True, True])
    chk("stage 2 sees stage 1's result", "`TASK.md`: TASK.md complete" in jobs[1]["prompt"], True)
    chk("stage budgets come from the task class", [j["iters"] for j in jobs], [6, 14, 12])
    chk("staged prompts still carry the full authoring context", "AUTHORING TASK" in jobs[0]["prompt"], True)

    runlog = tmp / "runaway.log"
    runlog.write_text("[model] PROSE RUNAWAY (block of 500 chars repeated x3, 12047 chars, no tool call) -- aborting turn\n")
    nothing = lambda w, p: (False, "job x failed (exit 1)", str(runlog))
    res, jobs, _ = run_staged([nothing, write("TASK.md", GOOD_TASK), write("test_fixture.py", GOOD_FX),
                               write("refimpl.py", GOOD_REF)])
    chk("a no-write job is re-prompted and the run still converges", res[0], True)
    chk("...the re-prompt carries the file-writing instruction", "NO-WRITE" in jobs[1]["prompt"], True)
    chk("...and the prose-runaway evidence", "block of 500 chars repeated x3" in jobs[1]["prompt"], True)
    chk("...the first attempt had no re-prompt text", "NO-WRITE" in jobs[0]["prompt"], False)
    chk("...re-prompt job label is distinct", jobs[1]["label"].endswith("-s1-r1"), True)

    res, jobs, _ = run_staged([nothing, nothing, nothing, nothing, nothing])
    chk("a stage that never writes fails after reprompts+1 jobs (2+1)", (res[0], len(jobs)), (False, 3))
    chk("...with a reason naming the stage", "stage `task` wrote nothing" in res[1], True)

    def op_stop(w, p):
        return False, "job x cancelled", None
    a_op = mk_a(tmp, label="t-opstop")
    wt_ = make_wt(tmp)
    jobs2 = []

    def fd(wt__, prompt, label, vc, a__, max_iters=None):
        jobs2.append(label)
        a__._chain_last_job = "jopx"
        a__._last_job_outcome = {"job": "jopx", "status": "failed", "failure_class": "operator",
                                 "terminal_reason": "force_stopped", "log_path": None}
        return False, "job jopx failed (exit 3)"
    m.dispatch_model = fd
    res = m._author_staged(a_op, wt_, "app/x.py", "modify")
    chk("an operator stop is NOT re-prompted", (res[0], len(jobs2)), (False, 1))
    chk("...and recorded no failed attempt", m.load_attempts(a_op), [])

    # fixture written but wrong -> next job runs with the failure shown (not a no-write)
    bad_fx = write("test_fixture.py", "def test_x():\n    assert 0, 'SCAFFOLD_INCOMPLETE'\n# changed\n")
    res, jobs, _ = run_staged([write("TASK.md", GOOD_TASK), bad_fx, write("test_fixture.py", GOOD_FX),
                               write("refimpl.py", GOOD_REF)])
    chk("a changed-but-red stage gets another job and converges", res[0], True)
    chk("...whose prompt shows what is still failing", "NOT yet passing" in jobs[2]["prompt"], True)

    # (2026-10-09, rt-costco-receipt-attach-s2 b90e77ace2b2) a job that finished DONE but whose stage
    # CHECK is red is a CHECK failure: real check output tail kept, not class "other"/check_tail null.
    a_chk = mk_a(tmp, label="t-checkfail")
    res, jobs, _ = run_staged([write("TASK.md", GOOD_TASK), bad_fx, write("test_fixture.py", GOOD_FX),
                               write("refimpl.py", GOOD_REF)], a=a_chk)
    h_chk = m.load_attempts(a_chk)
    chk("done-job + red stage check records ONE attempt", len(h_chk), 1)
    chk("...its check_tail carries the stage check's output", bool((h_chk[0] or {}).get("check_tail")), True)
    chk("...tail names the failing check (SCAFFOLD guard)", "SCAFFOLD" in str((h_chk[0] or {}).get("check_tail")), True)
    chk("...class is not 'other'", (h_chk[0] or {}).get("class") not in (None, "other"), True)
    chk("...and the record is flagged a check failure", (h_chk[0] or {}).get("kind"), "check")
    chk("...a plain job failure still records no kind", (m.record_attempt(mk_a(tmp, label="t-plainfail"), "jx",
        "job jx failed (exit 1)", None) or {}).get("kind"), None)

    # handoff placeholder statuses are NOT terminal: an in-flight (pending/running) job whose live row
    # was momentarily missing must not read as 'failed (confirmed via the handoff record)'.
    chk("handoff status: done", m.handoff_status_outcome("done"), "done")
    chk("handoff status: done_unconverged is an OK terminal", m.handoff_status_outcome("done_unconverged"), "done")
    chk("handoff status: failed", m.handoff_status_outcome("failed"), "failed")
    chk("handoff status: needs_opus is terminal bad", m.handoff_status_outcome("needs_opus"), "failed")
    chk("handoff status: running is in flight", m.handoff_status_outcome("running"), None)
    chk("handoff status: pending is in flight", m.handoff_status_outcome("pending"), None)
    _fh = Path(tempfile.mkdtemp(prefix="fakehome-"))
    _hd = _fh / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff"
    (_hd / "pending").mkdir(parents=True)
    (_hd / "complete").mkdir(parents=True)
    (_hd / "pending" / "2026-10-09-lbl-aaaaaaaaaaaa.md").write_text("# l\n\n- **job id**: `aaaaaaaaaaaa`\n- **status**: `running`\n")
    (_hd / "complete" / "2026-10-09-lbl-bbbbbbbbbbbb.md").write_text("- **status**: `done`  (exit 0)\n")
    (_hd / "complete" / "2026-10-09-lbl-cccccccccccc.md").write_text("- **status**: `failed`  (exit 1)\n")
    _oh = m.Path.home
    m.Path.home = classmethod(lambda cls: _fh)
    try:
        chk("prune: in-flight placeholder -> not terminal (None)", m._terminal_after_prune("aaaaaaaaaaaa"), None)
        chk("prune: done record -> done", m._terminal_after_prune("bbbbbbbbbbbb"), "done")
        chk("prune: failed record -> failed", m._terminal_after_prune("cccccccccccc"), "failed")
    finally:
        m.Path.home = _oh

    # entry: staged failure falls back to the legacy ladder with resumed_why on the same tree
    seen = {}
    m._author_staged = lambda a, wt, t, c: (False, "staged boom")
    m._author_with_continuations = lambda a, wt, t, v, resumed_why=None: seen.update(why=resumed_why) or (True, "legacy ok")
    a = mk_a(tmp)
    a._last_job_outcome = None
    chk("staged failure falls back to the continuation ladder", m._author_entry(a, Path(tmp), "app/x.py", "v"),
        (True, "legacy ok"))
    chk("...resuming on the staged failure", seen.get("why"), "staged boom")
    seen.clear()
    chk("--staged off goes straight to the ladder",
        (m._author_entry(mk_a(tmp, staged="off"), Path(tmp), "app/x.py", "v"), seen.get("why")),
        ((True, "legacy ok"), None))
    chk("--drafter-cmd never stages", m.staged_enabled(mk_a(tmp, drafter_cmd="echo")), False)
    os.environ["ODS_STAGED_AUTHOR"] = "0"
    chk("ODS_STAGED_AUTHOR=0 disables staging", m.staged_enabled(mk_a(tmp)), False)
    del os.environ["ODS_STAGED_AUTHOR"]
    chk("default is staged", m.staged_enabled(mk_a(tmp)), True)

    # ---- 4. task class + lane -----------------------------------------------------------------
    wt = make_wt(tmp)
    a = mk_a(tmp)
    chk("modify task classified", m.task_class_for(a, wt, "app/x.py"), "modify")
    chk("class recorded in .dispatch-harness.json",
        json.loads((wt / ".dispatch-harness.json").read_text())["task_class"]["class"], "modify")
    a2 = mk_a(tmp, intent="Create a new module app/new.py with f(x)", target="app/new.py")
    chk("absent target -> create", m.task_class_for(a2, wt, "app/new.py"), "create")
    a3 = mk_a(tmp, task_class="port")
    chk("--task-class overrides", m.task_class_for(a3, wt, "app/x.py"), "port")
    chk("open lane adds no queue args", m.lane_enqueue_args(mk_a(tmp, lane="open")), [])
    tcm = m._tc()
    tcm.queue_supports_mode = lambda *a, **k: False
    chk("fixed lane on a queue without --mode adds nothing", m.lane_enqueue_args(mk_a(tmp, lane="fixed")), [])
    tcm.queue_supports_mode = lambda *a, **k: True
    chk("fixed lane + supporting queue -> --mode fixed (+ --fixed-target, H8 passthrough)", m.lane_enqueue_args(mk_a(tmp, lane="fixed")), ["--mode", "fixed", "--fixed-target", "app/x.py"])
    a4 = mk_a(tmp, lane="auto")
    a4._task_class = "modify"
    chk("auto lane -> fixed for a MODIFY", m.lane_enqueue_args(a4), ["--mode", "fixed", "--fixed-target", "app/x.py"])
    a4._task_class = "create"
    chk("auto lane -> open for a CREATE", m.lane_enqueue_args(a4), ["--mode", "open"])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
