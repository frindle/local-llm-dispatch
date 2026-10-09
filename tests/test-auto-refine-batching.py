#!/usr/bin/env python3
"""Survivor-focused refine rounds + spec traceability (ollama-dispatch-auto, 2026-10-06).

Root cause of the 77d808c3984a prose loop (BFMR TLS refine, >2h): ONE refine round got
all 30 survivors (25 lines of node:https shim plumbing) and 6 + 4*30 = 126 iterations;
the model planned every case at once in prose (12 output-cap cut-offs, per-turn latency
3s -> 350s). Now:
  * each round carries at most REFINE_BATCH_LINES survivor lines, the rest DEFERRED,
  * its iteration budget scales with the batch and is capped (REFINE_ITERS_CAP),
  * the round cap stretches so every batch gets a round (<= REFINE_MAX_ROUNDS_HARD),
  * survivors whose line names nothing in TASK.md are marked, with the sanctioned
    resolution "DELETE unrequired code from refimpl.py",
  * the prompt tells the model to work one survivor at a time, a tool call per turn.

Drives the REAL _preflight_loop with the preflight/dispatch stubbed (no queue, no GPU).
--revert-check mutates the auto (AUTO_SRC) and requires RED for each mutation."""
import importlib.util, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []
F = "lib/apiCallLog.ts"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("oda_batch", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_batch", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    return m


TASK = """# TASK: rt-bfmr-tls-fingerprint
## Required change
loggedFetch in lib/apiCallLog.ts must, for requests whose URL host is bfmr.com or
www.bfmr.com, send them through node:https with ciphers 'DEFAULT'. Export
needsBrowserLikeTls(url) and bfmrTlsConnectOptions(). Keep the request body.
## Must contain
- `needsBrowserLikeTls`
- `node:https`
"""
# (line, original) -- real survivor lines from 77d808c3984a's refine prompt
ORIG = {56: "if (!_bfmrAgent) {", 79: "req.write(opts.body);",
        97: "const buf = Buffer.concat(chunks);", 135: "const isBfmr = needsBrowserLikeTls(url);",
        65: "const https = require('node:https');",
        90: "sig.addEventListener('abort', () => { req.abort(); });"}


def survivors(lines):
    out = []
    for ln in lines:
        out.append({"file": F, "line": ln, "mutation": "m", "snippet": "x",
                    "original": ORIG.get(ln, f"plumbing_{ln}(x);")})
        if ln % 5 == 0:   # some lines carry two mutants
            out.append({"file": F, "line": ln, "mutation": "m2", "snippet": "y",
                        "original": ORIG.get(ln, f"plumbing_{ln}(x);")})
    return out


def drive(m, n_lines=25):
    wt = Path(tempfile.mkdtemp(prefix="refbatch-wt-"))
    (wt / "TASK.md").write_text(TASK)
    (wt / "refimpl.py").write_text("NEW = ''\n")
    alive = list(range(50, 50 + n_lines))
    calls, outcome = [], {}

    def preflight(wt_, req, a):
        if not alive:
            return 0, {"verdict": "GO"}
        return 1, {"verdict": "NO-GO", "survs": survivors(alive),
                   "blockers": [{"check": "verify-relevance"}]}

    def dispatch(wt_, prompt, label, verify_cmd, a, max_iters=None):
        shown = sorted({int(x) for x in re.findall(r"`lib/apiCallLog\.ts:(\d+)`", prompt)})
        calls.append({"label": label, "iters": max_iters, "lines": shown, "prompt": prompt})
        for ln in shown:          # the model kills exactly the survivors it was shown
            if ln in alive:
                alive.remove(ln)
        return True, "converged"

    m.slice_already_satisfied = lambda *x: False
    m.slice_taken_elsewhere = lambda *x: None
    m.driver_gone = lambda a: None
    m.chain_state_write = lambda *x, **k: None
    m.enforce_target_at_head = lambda *x: []
    m.run_preflight = preflight
    m.wait_out_operational_blockers = lambda wt_, req, a, rc, data: (rc, data)
    m.survivors_of = lambda data: data.get("survs") or []
    m.relevance_verdict = lambda data: "FAIL" if data.get("survs") else None
    m.write_refine_guard = lambda *x, **k: None
    m.clear_refine_guard = lambda *x, **k: None
    m.map_survivor_lines = lambda *x, **k: None
    m.patched_target_text = lambda *x, **k: ""
    m.must_contain_from_task = lambda wt_: []
    m.dispatch_model = dispatch
    m.pause_for_review = lambda *x, **k: outcome.setdefault("go", True) and 0
    m._autoslice_on_failure = lambda a, t, why: outcome.setdefault("fail", why) and 9
    m._print_diff = lambda *x, **k: None
    a = SimpleNamespace(require=[], max_rounds=4, no_progress_rounds=2, author_max_iters=24,
                        lang="typescript", slice_plan=None, slice_id=None, label="x")
    rc = m._preflight_loop(a, wt, F, "python3 auto-harness-check.py")
    return rc, calls, outcome


def main():
    m = load()
    rc, calls, outcome = drive(m)
    check("25 survivor lines reach GO (not the 4-round cap)", outcome.get("go"), True)
    check("...in 5 refine rounds of <= 6 lines each",
          [len(c["lines"]) for c in calls], [6, 6, 6, 6, 1])
    check("no round gets more than REFINE_ITERS_CAP iterations (was 6+4*30=126)",
          max(c["iters"] for c in calls) <= m.REFINE_ITERS_CAP, True)
    check("a 6-line round gets 30 iterations", calls[0]["iters"], 30)
    check("a 30-line batch is capped at REFINE_ITERS_CAP=40 (was 126)",
          m.refine_round_iters(24, survivors(range(30))), 40)
    check("the first round names the deferred survivors",
          "DEFERRED to the next round" in calls[0]["prompt"], True)
    check("the last round defers nothing", "DEFERRED" in calls[-1]["prompt"], False)
    check("every round tells the model to work one survivor at a time",
          all("One survivor at a time" in c["prompt"] for c in calls), True)

    # traceability: TLS survivor lines against the (trimmed) TLS TASK.md
    sv = [{"file": F, "line": ln, "original": o, "mutation": "m", "snippet": "x"}
          for ln, o in ORIG.items()]
    ut = m.untraced_survivor_keys(sv, TASK)
    check("untraced: the agent cache / Buffer.concat / abort listener",
          sorted(k[1] for k in ut), [56, 90, 97])
    check("traced: needsBrowserLikeTls, node:https, the request body",
          sorted(k[1] for k in sv_keys(sv) if k not in ut), [65, 79, 135])
    neg = TASK + ("Do NOT implement AbortSignal/AbortController or timeout handling -- it is "
                  "not required. Tests: no abort-signal cases.\n")
    check("a NEGATED mention ('Do NOT implement AbortSignal') does not trace the abort line",
          (F, 90) in m.untraced_survivor_keys(sv, neg), True)
    rp = m.refine_prompt(F, sv, [], lang="typescript", task_text=TASK)
    check("the prompt offers the sanctioned resolution (delete unrequired code)",
          "UNREQUIRED CODE" in rp and "DELETE it" in rp, True)
    marked = [ln for ln in ORIG if re.search(rf"`lib/apiCallLog\.ts:{ln}`[^\n]*names nothing in TASK", rp)]
    check("...and marks exactly the untraced lines", sorted(marked), [56, 90, 97])
    check("...and lifts the 'do not edit the reference impl' rule only for that",
          "the one exception is DELETING code TASK.md does not require" in rp, True)
    rp0 = m.refine_prompt(F, sv, [], lang="typescript")
    check("no TASK text -> no traceability claims", "UNREQUIRED CODE" in rp0, False)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def sv_keys(sv):
    return [(s["file"], s["line"]) for s in sv]


MUTATIONS = [
    ("all survivors in one round",
     "    keep = set(sorted(order, key=lambda t: (str(t[0]), int(t[1] or 0)))[:k])",
     "    keep = set(order)"),
    ("no iteration cap", "    return min(REFINE_ITERS_CAP, max(author_max_iters, 6 + 4 * n))",
     "    return max(author_max_iters, 6 + 4 * n)"),
    ("round cap not stretched",
     "            _max_rounds = max(_max_rounds, refine_round_cap(a.max_rounds, survs))",
     "            pass"),
    ("no traceability marks", '            _ut = " [names nothing in TASK.md]" if key in untraced else ""',
     '            _ut = ""'),
    ("negated clauses count as spec", "    task_text = spec_text_for_trace(task_text)\n", ""),
    ("no work rule", "        lines.append(REFINE_WORK_RULE + \"\\n\")\n", ""),
    ("batch not used for the dispatch",
     "        ok, why = dispatch_model(wt, refine_prompt(target, _batch, rel_blockers, a.lang,",
     "        ok, why = dispatch_model(wt, refine_prompt(target, survs, rel_blockers, a.lang,"),
]


def revert_check():
    bad = 0
    src = AUTO.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
