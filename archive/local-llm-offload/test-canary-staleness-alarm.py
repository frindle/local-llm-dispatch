#!/usr/bin/env python3
"""test-canary-staleness-alarm.py -- canary-staleness-check.py (hardening design 4.6).

RED classes notify immediately with dedupe key canary-<class>; AMBER classes go in ONE daily digest
(canary-amber); a clean state sends nothing; CANARY_STALE_ALARM=0 disables the job; the exit code
is 1 only when something is RED.

  python3 test-canary-staleness-alarm.py [--bin DIR]     -> prints STALENESS_ALARM_OK
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

BIN = Path(__file__).resolve().parent
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
spec = importlib.util.spec_from_file_location("csc_under_test", str(BIN / "canary-staleness-check.py"))
M = importlib.util.module_from_spec(spec)
sys.modules["csc_under_test"] = M
spec.loader.exec_module(M)

FAILS: list = []
NOW = 1_800_000_000.0
H = 3600.0


def check(name, ok, note=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -- {note}"))
    if not ok:
        FAILS.append(name)


def base(**kw):
    inp = {"now": NOW, "stale_hours": 2, "e2e": {"ok": True}, "analysis": {
        "missing": [], "unproven": [], "anchor_lost": {}, "reprove": {}, "recheck": {}},
        "selftest_ok": True, "first_seen": {}, "full_prove_ts": NOW - 3 * H, "changed_age_s": None,
        "e2e_stale_age_s": None, "unmirrored": []}
    for k, v in kw.items():
        if k in ("missing", "unproven", "anchor_lost", "reprove", "recheck"):
            inp["analysis"][k] = v
        else:
            inp[k] = v
    return inp


def classes(res, kind):
    return sorted(c for c, _ in res[kind])


r = M.classify(base())
check("clean.nothing_red_or_amber", r == {"red": [], "amber": []}, str(r))
check("red.e2e_failed", classes(M.classify(base(e2e={"ok": False, "failed": ["x"]})), "red") == ["e2e"])
check("red.anchor_lost", classes(M.classify(base(anchor_lost={"s1": ["f anchor x0"]})), "red") == ["anchor-lost"])
check("red.selftest_red", classes(M.classify(base(selftest_ok=False)), "red") == ["selftest"])
check("selftest.unknown_is_not_red", classes(M.classify(base(selftest_ok=None)), "red") == [])
r = M.classify(base(missing=["newseam"], first_seen={"newseam": NOW - 2 * H}))
check("red.unproven_seam_after_1h", classes(r, "red") == ["unproven"], str(r))
r = M.classify(base(missing=["newseam"], first_seen={"newseam": NOW - 0.5 * H}))
check("unproven.within_grace_is_quiet", classes(r, "red") == [], str(r))
check("red.full_prove_older_than_26h", classes(M.classify(base(full_prove_ts=NOW - 27 * H)), "red") == ["prove-age"])
check("red.never_proven_at_all", classes(M.classify(base(full_prove_ts=None)), "red") == ["prove-age"])
check("prove_age.25h_is_fine", classes(M.classify(base(full_prove_ts=NOW - 25 * H)), "red") == [])
r = M.classify(base(reprove={"s1": ["x"]}, recheck={"s2": ["y"]}, changed_age_s=3 * H))
check("amber.stale_seams_older_than_N_hours", classes(r, "amber") == ["stale"], str(r))
r = M.classify(base(reprove={"s1": ["x"]}, changed_age_s=1 * H))
check("stale.fresh_change_is_quiet", classes(r, "amber") == [], str(r))
r = M.classify(base(reprove={"s1": ["x"]}, changed_age_s=1 * H, stale_hours=0.5))
check("stale.threshold_is_configurable", classes(r, "amber") == ["stale"], str(r))
check("amber.e2e_skipped_for_hours", classes(M.classify(base(e2e_stale_age_s=5 * H)), "amber") == ["e2e-skipped"])
check("e2e_skipped.recent_is_quiet", classes(M.classify(base(e2e_stale_age_s=0.5 * H)), "amber") == [])
check("amber.unmirrored_live_vs_repo_head", classes(M.classify(base(unmirrored=["a.py"])), "amber") == ["unmirrored"])

calls = []
note = lambda title, msg, key, s: calls.append((title, key, s))     # noqa: E731
M.deliver(M.classify(base(e2e={"ok": False, "failed": ["x"]}, selftest_ok=False, unmirrored=["a.py"])), note, NOW)
keys = sorted(k for _, k, _ in calls)
check("deliver.red_per_class_and_one_digest", keys == ["canary-amber", "canary-e2e", "canary-selftest"], str(keys))
check("deliver.red_repeats_6h_digest_daily", {k: s for _, k, s in calls} == {"canary-e2e": 21600, "canary-selftest": 21600, "canary-amber": 86400}, str(calls))
calls.clear()
M.deliver(M.classify(base()), note, NOW)
check("deliver.clean_sends_nothing", calls == [])
calls.clear()
M.deliver(M.classify(base(unmirrored=["a.py"], e2e_stale_age_s=9 * H)), note, NOW)
check("deliver.amber_is_a_single_digest", len(calls) == 1 and calls[0][1] == "canary-amber", str(calls))

# main(): wiring, exit code, kill switch, state files
tmp = Path(tempfile.mkdtemp(prefix="csc-test-"))
M.STATE_DIR, M.OUT_PATH, M.STATE_PATH = tmp, tmp / "staleness.json", tmp / "state.json"
M.load_canary = lambda b: None
sent = []
M._notify = lambda t, m_, k, s: sent.append(k)


def run_main(inp, argv=("--no-selftest",)):
    sent.clear()
    inp = dict(inp)
    inp["_state"] = {"first_seen": {}}
    M.gather = lambda cn, b, st, now: dict(inp)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = M.main(list(argv))
    return rc, buf.getvalue()


rc, out = run_main(base(e2e={"ok": False, "failed": ["boom"]}))
check("main.red_exit_1_and_notifies", rc == 1 and sent == ["canary-e2e"] and "RED   e2e" in out, f"{rc} {sent} {out}")
check("main.writes_panel_file", M.OUT_PATH.is_file() and "boom" in M.OUT_PATH.read_text())
rc, out = run_main(base(unmirrored=["a.py"]))
check("main.amber_only_exit_0", rc == 0 and sent == ["canary-amber"], f"{rc} {sent}")
rc, out = run_main(base())
check("main.clean_exit_0_silent", rc == 0 and sent == [] and "current and green" in out, f"{rc} {sent} {out}")
rc, out = run_main(base(e2e={"ok": False, "failed": ["boom"]}), argv=("--no-selftest", "--dry-run"))
check("main.dry_run_notifies_nobody", rc == 1 and sent == [], str(sent))
os.environ["CANARY_STALE_ALARM"] = "0"
try:
    rc, out = run_main(base(e2e={"ok": False, "failed": ["boom"]}))
    check("main.kill_switch_disables_job", rc == 0 and sent == [] and "disabled" in out, f"{rc} {sent} {out}")
finally:
    del os.environ["CANARY_STALE_ALARM"]

# unmirrored noise exclusion (is_noise + the real gather() loop against a throwaway git repo)
import subprocess
import types
for n in (".DS_Store", ".net-watch-state", ".restart-at-gap.sh", "x.py.bak-20261001", "y.py.retired-20261008",
          "__pycache__/z.cpython-312.pyc", "z.pyc", "q.py.severed-1", "w.py.pre-x-backup", "signoff.json", "a.log"):
    check(f"noise.{n}", M.is_noise(n), n)
for n in ("ollama-queue.py", "hardening-wave1b-seams.md", "gate-on-complete.py", "ollama-dispatch-auto"):
    check(f"real.{n}", not M.is_noise(n), n)
nt = Path(tempfile.mkdtemp(prefix="csc-noise-"))
(nt / "mc" / "bin").mkdir(parents=True)
(nt / "bin").mkdir()
subprocess.run(["git", "-C", str(nt / "mc"), "init", "-q"], check=True)
names = ["real-a.py", "real-b.md", ".DS_Store", ".net-watch-state", "c.py.bak-1", "d.py.retired-1", "empty.py", "state.json", "link.py"]
for n in names[:-1]:
    (nt / "bin" / n).write_text("live\n")
(nt / "other.py").write_text("elsewhere\n")
(nt / "bin" / "empty.py").write_text("")
(nt / "bin" / "link.py").symlink_to(nt / "other.py")
(nt / "mc" / "bin" / "real-b.md").write_text("live\n")          # mirrored and identical -> not listed
subprocess.run(["git", "-C", str(nt / "mc"), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
subprocess.run(["git", "-C", str(nt / "mc"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t"], check=True)
fake = types.SimpleNamespace(
    load_coverage=lambda: {}, analyze_coverage=lambda b, c: {"reprove": {}, "recheck": {}, "missing": [], "unproven": [], "anchor_lost": {}},
    STATUS_PATH=nt / "nope.json", _h=lambda p: None, pipeline_hashes=lambda b: {}, list_files=lambda: sorted(names))
M.STATE_PATH = nt / "st.json"
M.MC_REPO = nt / "mc"
spec2 = importlib.util.spec_from_file_location("csc_fresh", str(BIN / "canary-staleness-check.py"))
M2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(M2)
M2.STATE_PATH, M2.MC_REPO = nt / "st.json", nt / "mc"
g = M2.gather(fake, nt / "bin", False, NOW)
check("gather.unmirrored_is_only_the_real_pipeline_file", g["unmirrored"] == ["real-a.py"], str(g["unmirrored"]))

print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
if not FAILS:
    print("STALENESS_ALARM_OK")
sys.exit(1 if FAILS else 0)
