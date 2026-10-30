"""Plan context + deferred-concern OBLIGATIONS for intermediate slices (2026-10-03).

THE CLASS THIS EXISTS FOR (143d9acfe43a, replay-endorse s2): an intermediate slice
of a chain is reviewed as if it were the final state of the file. Its reviewer
flagged "the original global dedup by trackingNumber is removed" -- true of s2 in
isolation, and exactly what the dependent slice s3 (endorsement selection) is
planned to establish. The authoritative verdict was `concerns`, the chain parked
needs_opus, and the coordinator had to --accept-slice by hand.

WHAT THIS DOES -- a concern is MOVED, never dropped:
  1. The reviewer of a slice that has dependents is told the plan: dependent ids +
     intents, and that this slice is not the final state. It must still report every
     finding; it may tag one `[restored-by: <slice-id>]`.
  2. A tagged finding is DEFERRED only when it is mechanically mappable:
       - the tagged id is a TRANSITIVE dependent of this slice in the plan,
       - the finding is review-sourced, category=code, and not high,
       - the finding shares >=1 content term with that dependent's intent/must_contain,
     AND the whole verdict qualifies: verdict=concerns, no code_high, EVERY issue is
     such a finding (any verify/scope/completeness/baseline/relevance issue blocks
     deferral), and the job's own verify was green (job_exit_code == 0).
     All-or-nothing: one unmapped concern -> nothing deferred, it escalates as before.
  3. Each deferred finding becomes an OPEN obligation on the named dependent
     (slice-runs/_obligations/<run>.json). The dependent's reviewer is shown it and
     must answer `RESTORED: <oid>` or `NOT-RESTORED: <oid>`. NOT-RESTORED -> a code
     HIGH on the dependent (fails its gate). The obligation is DISCHARGED only by
     the dependent's terminal PASS with its own verify green, evidence recorded.
  4. --land-integration REFUSES while any obligation of the run is open (a skipped,
     dropped or never-gated dependent leaves it open -> a human decides).
"""
import json
import os
import re
import time
from pathlib import Path

SLICE_RUNS = Path(os.environ.get("OLLAMA_DISPATCH_HOME",
                                 str(Path.home() / ".ollama-dispatch"))) / "slice-runs"

RESTORED_BY_RE = re.compile(r"\[\s*restored[- ]by\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*\]", re.I)

_STOP = set("""
that this with from into have will when then than they them their there these those
which while where what were been being does done each every only also must should
would could just like such same other some more most less very into onto over under
after before between within without about above below because since until code line
lines file files function functions change changes changed slice slices test tests
return returns value values call calls used uses using make makes made still keep
""".split())


def base_label(label):
    # Strip the gate's ' [auto-fix rN]' requeue marker FIRST (2026-10-06): an
    # auto-fix round of a slice job (01fdda651fba 'rt-egift-link-s1-s0-db-schema
    # [auto-fix r1]') resolved to no slice at all, so its review got no plan
    # context and its inherited-obligation acks were never checked.
    s = re.sub(r"\s*\[auto-fix r\d+\]\s*$", "", str(label or ""))
    s = re.sub(r"^(?:auto-author|auto-refine)-", "", s)
    return re.sub(r"-r\d+$", "", s)


def load_runs(root=None):
    out = []
    root = Path(root or SLICE_RUNS)
    try:
        for p in sorted(root.glob("*.json")):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if isinstance(d, dict) and d.get("label") and d.get("slices"):
                out.append(d)
    except Exception:
        pass
    return out


def run_and_slice(label, runs):
    """(run, slice_id) owning this job label, or (None, None). Longest run label wins."""
    b = base_label(label)
    best, sid = None, None
    for run in runs or []:
        rl = str((run or {}).get("label") or "")
        if not rl or not b.startswith(rl + "-"):
            continue
        cand = b[len(rl) + 1:]
        if cand not in (run.get("slices") or {}):
            continue
        if best is None or len(rl) > len(str(best.get("label") or "")):
            best, sid = run, cand
    return best, sid


def plan_slices(run):
    try:
        plan = json.loads(Path(run["plan_path"]).read_text())
        return [s for s in (plan.get("slices") or []) if isinstance(s, dict) and s.get("id")]
    except Exception:
        return []


def transitive_dependents(slices, sid):
    """Ids of every slice that (transitively) depends on `sid`, in plan order."""
    deps = {s["id"]: set(s.get("depends_on") or []) for s in slices}
    out, frontier = set(), {sid}
    while frontier:
        nxt = {k for k, v in deps.items() if v & frontier and k not in out and k != sid}
        out |= nxt
        frontier = nxt
    return [s["id"] for s in slices if s["id"] in out]


def slice_context(label, runs=None):
    """{run_label, sid, target, slices, dependents} for a slice job, else None."""
    runs = load_runs() if runs is None else runs
    run, sid = run_and_slice(label, runs)
    if run is None:
        return None
    slices = plan_slices(run)
    return {"run_label": run["label"], "sid": sid, "target": run.get("target") or "",
            "slices": slices, "dependents": transitive_dependents(slices, sid)}


def _split_camel(t):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", t)


def content_terms(text):
    """PURE. Normalised content words (>=4 chars, crude stem, no stopwords)."""
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", _split_camel(str(text or ""))):
        w = w.lower()
        for suf in ("ations", "ation", "ing", "ies", "ed", "es", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        if len(w) >= 4 and w not in _STOP:
            out.add(w)
    return out


def plan_context_text(ctx):
    """Reviewer context for a slice WITH dependents; '' otherwise."""
    if not ctx or not ctx.get("dependents"):
        return ""
    by = {s["id"]: s for s in ctx["slices"]}
    lines = [f"  - {d}: {str(by.get(d, {}).get('intent') or '')[:400]}"
             for d in ctx["dependents"]]
    return (
        f"\n\nPLAN CONTEXT (mechanical, from the slice plan): this diff is slice "
        f"`{ctx['sid']}` of chain `{ctx['run_label']}`. It is an INTERMEDIATE step, NOT "
        f"the final state of {ctx.get('target') or 'the target'}. Later slices in the "
        f"plan build on it:\n" + "\n".join(lines) +
        "\nStill REPORT every defect you find -- do not omit any. If a finding is a "
        "behaviour one of those later slices is planned to establish or restore, end "
        "its description with `[restored-by: <slice-id>]` naming that slice. The tag "
        "does NOT clear the finding: it is handed to that slice's gate, which must "
        "prove it restored; an untagged finding is judged here as usual.")


def inherited_text(obls):
    if not obls:
        return ""
    lines = [f"  - {o['oid']} (from {o['from_slice']}): {o.get('file') or ''}"
             f"{':' + str(o['line']) if o.get('line') else ''} -- {o.get('what')}"
             for o in obls]
    return (
        "\n\nINHERITED CONCERNS this slice is planned to resolve (deferred from an "
        "earlier slice's gate):\n" + "\n".join(lines) +
        "\nFor EACH, check the code after this diff and write exactly one line in "
        "your report: `RESTORED: <oid>` or `NOT-RESTORED: <oid>` (with the reason). "
        "A NOT-RESTORED inherited concern fails this gate.")


def map_issue(issue, ctx):
    """(dependent_id, shared_terms) when this finding may be deferred, else (None, why)."""
    if issue.get("source") != "review" or issue.get("category") != "code":
        return None, f"not a review code finding (source={issue.get('source')})"
    if str(issue.get("severity")) == "high":
        return None, "code high is never deferred"
    dep = issue.get("restored_by")
    if not dep:
        return None, "reviewer did not tag it [restored-by: ...]"
    if dep not in (ctx.get("dependents") or []):
        return None, f"{dep!r} is not a dependent of {ctx.get('sid')}"
    by = {s["id"]: s for s in ctx["slices"]}
    d = by.get(dep) or {}
    shared = content_terms(issue.get("what")) & content_terms(
        " ".join([str(d.get("intent") or "")] + [str(x) for x in d.get("must_contain") or []]))
    if not shared:
        return None, f"no content term shared with {dep}'s intent/must_contain"
    return dep, sorted(shared)


def decide_deferral(payload, ctx):
    """PURE. (deferred_rows | None, reason)."""
    if not ctx or not ctx.get("dependents"):
        return None, "not an intermediate slice (no dependents)"
    if payload.get("verdict") != "concerns":
        return None, f"verdict {payload.get('verdict')!r} is not concerns"
    if (payload.get("counts") or {}).get("code_high"):
        return None, "code_high present"
    if payload.get("job_exit_code") != 0:
        return None, f"job's own verify not green (job_exit_code={payload.get('job_exit_code')!r})"
    if payload.get("untrusted"):
        return None, "untrusted input"
    issues = payload.get("issues") or []
    if not issues:
        return None, "no issues to defer"
    rows = []
    for i in issues:
        dep, why = map_issue(i, ctx)
        if dep is None:
            return None, f"unmapped concern ({why}): {str(i.get('what'))[:120]}"
        rows.append({"issue": i, "restored_by": dep, "shared_terms": why})
    return rows, "every concern maps to a dependent"


# ---- obligations store --------------------------------------------------------
def _obl_path(run_label, root=None):
    # a SUBDIR (like _abandoned/), so the many `slice-runs/*.json` globs that expect
    # a run-state dict never see a list-shaped obligations file
    d = Path(root or SLICE_RUNS) / "_obligations"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_label}.json"


def load_obligations(run_label, root=None):
    try:
        d = json.loads(_obl_path(run_label, root).read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_obligations(run_label, obls, root=None):
    p = _obl_path(run_label, root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obls, indent=1))
    os.replace(tmp, p)


def add_obligations(run_label, from_slice, from_job, rows, root=None):
    obls = load_obligations(run_label, root)
    have = {o["oid"] for o in obls}
    new = []
    for n, r in enumerate(rows, 1):
        oid = f"{from_job}-o{n}"
        if oid in have:
            continue
        i = r["issue"]
        o = {"oid": oid, "from_slice": from_slice, "from_job": from_job,
             "restored_by": r["restored_by"], "file": i.get("file"), "line": i.get("line"),
             "what": i.get("what"), "shared_terms": r["shared_terms"], "status": "open",
             "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        obls.append(o)
        new.append(o)
    save_obligations(run_label, obls, root)
    return new


def open_obligations(run_label, sid=None, root=None):
    return [o for o in load_obligations(run_label, root)
            if o.get("status") == "open" and (sid is None or o.get("restored_by") == sid)]


def review_acks(report_text):
    """PURE. {oid: 'RESTORED'|'NOT-RESTORED'} from a review report."""
    out = {}
    for m in re.finditer(r"\b(NOT-RESTORED|RESTORED)\s*:\s*`?([A-Za-z0-9._-]+-o\d+)`?",
                         str(report_text or "")):
        oid = m.group(2)
        # NOT-RESTORED wins if both appear
        if out.get(oid) != "NOT-RESTORED":
            out[oid] = m.group(1)
    return out


def discharge(run_label, sid, job_id, evidence, root=None):
    """Mark every open obligation on `sid` discharged with evidence. Returns them."""
    obls = load_obligations(run_label, root)
    done = []
    for o in obls:
        if o.get("status") == "open" and o.get("restored_by") == sid:
            o["status"] = "discharged"
            o["discharged_by_job"] = job_id
            o["discharge_evidence"] = evidence
            o["discharged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            done.append(o)
    if done:
        save_obligations(run_label, obls, root)
    return done
