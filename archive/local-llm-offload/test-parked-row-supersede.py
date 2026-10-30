#!/usr/bin/env python3
"""Parked queue rows must not deadlock their own replacement (2026-10-02, BFMR).

Live: the superseded attempt's rows `needs-opus-auto-bfmr-replace-tracking` and
`auto-author-bfmr-replace-tracking` (status needs_opus, no process) sat on the
worktree. The preflight counted the unrecognised status as ACTIVE -> NO-GO
cwd-exclusive for the replacement, forever; and the enqueue never folded them, so
only a manual `resolve` cleared either. Pinned here:
  preflight: a PARKED row (needs_opus/blocked/done_unconverged) on this tree is
             WARN (named), not FAIL; a running/held/planned row still FAILs; a
             superseded row is ignored.
  queue:     enqueueing coding job X in cwd W supersedes needs_opus rows whose
             label is X / auto-author-X / needs-opus-auto-X in W -- never in another
             cwd, never a running row.
--revert-check mutates each fix and requires this suite to go RED.
PREFLIGHT_SRC / QUEUE_SRC env override the files under test."""
import importlib.util, json, os, subprocess, sys, tempfile, types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
PF = Path(os.environ.get("PREFLIGHT_SRC") or BIN / "ollama-dispatch-preflight")
QU = Path(os.environ.get("QUEUE_SRC") or BIN / "ollama-queue.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    loader.exec_module(m)
    return m


def main():
    tmp = Path(tempfile.mkdtemp(prefix="parked-"))
    qs = tmp / "state.json"
    os.environ["OLLAMA_QUEUE_STATE"] = str(qs)
    pf = load(PF, "pf_parked")
    pf.QUEUE_STATE = qs
    wt = (tmp / "wt").resolve()
    wt.mkdir()
    other = (tmp / "other").resolve()
    other.mkdir()

    def cwd_check(jobs):
        qs.write_text(json.dumps({"jobs": jobs}))
        p = pf.Preflight(types.SimpleNamespace(worktree=str(wt)))
        p.check_cwd_exclusive()
        r = [x for x in p.results if x.id == "cwd-exclusive"][-1]
        return r.status, r.msg + " " + r.detail

    st, txt = cwd_check([
        {"id": "e074cffcd8d0", "status": "needs_opus", "label": "needs-opus-auto-x", "cwd": str(wt)},
        {"id": "4c3b21254be9", "status": "needs_opus", "label": "auto-author-x", "cwd": str(wt)}])
    chk("preflight: needs_opus rows on this tree do NOT block (not FAIL)", st != pf.FAIL, True)
    chk("preflight: ...they are reported as WARN", st, pf.WARN)
    chk("preflight: ...naming the parked row ids", "e074cffcd8d0" in txt and "4c3b21254be9" in txt, True)
    for s in ("blocked", "done_unconverged"):
        chk(f"preflight: a {s} row does not block", cwd_check(
            [{"id": "b1", "status": s, "label": "x", "cwd": str(wt)}])[0], pf.WARN)
    for s in ("running", "pending", "held", "planned"):
        chk(f"preflight: a {s} row on this tree still FAILs", cwd_check(
            [{"id": "r1", "status": s, "label": "x", "cwd": str(wt)}])[0], pf.FAIL)
    chk("preflight: a superseded needs_opus row is ignored -> PASS", cwd_check(
        [{"id": "s1", "status": "needs_opus", "label": "x", "cwd": str(wt),
          "superseded_by": "new"}])[0], pf.PASS)
    chk("preflight: a parked row in ANOTHER tree -> PASS", cwd_check(
        [{"id": "o1", "status": "needs_opus", "label": "x", "cwd": str(other)}])[0], pf.PASS)

    q = load(QU, "q_parked")
    W = "/w/resell"
    rows = [{"id": "a1", "label": "needs-opus-auto-bfmr-rt", "status": "needs_opus", "cwd": W},
            {"id": "a2", "label": "auto-author-bfmr-rt", "status": "needs_opus", "cwd": W},
            {"id": "a3", "label": "auto-author-bfmr-rt", "status": "needs_opus", "cwd": "/w/OTHER"},
            {"id": "a4", "label": "auto-author-bfmr-rt", "status": "running", "cwd": W},
            {"id": "a5", "label": "something-else", "status": "needs_opus", "cwd": W}]
    new = {"id": "n1", "label": "bfmr-rt", "status": "pending", "cwd": W, "task_kind": "coding"}
    sup = q._mark_superseded(rows + [new], new, now="T")
    chk("queue: coding X supersedes parked needs-opus-auto-X and auto-author-X in the same cwd",
        sorted(sup), ["a1", "a2"])
    chk("queue: a superseded needs_opus row folds out of the worklist",
        q._is_worklist_job(rows[0]), False)
    chk("queue: an unsuperseded needs_opus row is still a worklist item",
        q._is_worklist_job(rows[4]), True)
    chk("queue: retry-base strips stage prefixes",
        (q._retry_base("needs-opus-auto-bfmr-rt"), q._retry_base("auto-author-bfmr-rt-r2")),
        ("bfmr-rt", "bfmr-rt"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    (PF, "PREFLIGHT_SRC", "parked counted as unknown/active",
     "                    or st in PARKED_JOB_STATUSES:\n", "                    or False:\n"),
    (PF, "PREFLIGHT_SRC", "superseded rows not skipped",
     "            if st in INACTIVE_JOB_STATUSES or j.get(\"superseded_by\"):",
     "            if st in INACTIVE_JOB_STATUSES:"),
    # (held/planned in ACTIVE is not mutated: an unknown status was already
    #  counted active, so dropping them is an equivalent mutant.)
    (QU, "QUEUE_SRC", "needs_opus not supersedable",
     "                            \"needs_opus\")", "                            )"),
    (QU, "QUEUE_SRC", "stage prefixes not stripped",
     "|auto-author-|needs-opus-auto-|needs-opus-)+", ")+"),
]


def revert_check():
    bad = 0
    for path, env, name, old, new in MUTANTS:
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(BIN)) as f:
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
