#!/usr/bin/env python3
"""pipeline-canary.py -- fast, deterministic END-TO-END canary of the dispatch pipeline.

WHY (2026-10-05)
----------------
Every pipeline tool has unit tests. The defects that kept reaching live runs were
at the SEAMS between them:
  - gate relevance racing auto's self-check (no tree lock)
  - two drivers overwriting one bundle record
  - a continuation that converged but never re-ran preflight
  - a plan's verify_shape that never reached the author
  - the slicer marking a slice FAILED while the gate's auto-fix was live
  - READY-TO-LAND rows going stale
  - a bundle bouncing between lanes
  - Darkbloom base_url and schema failures crashing a review
None of those can be seen by a unit test. This runs the REAL tools together:
  - the queue daemon, the worker and the slicer
  - auto, scaffold, preflight and gate-on-complete
  - code-review-agent, self-heal, ack-reconcile, the escalation watcher and the janitor
The run is a tiny 3-slice plan against STUBBED model servers, with concurrency
injected. At the end it asserts END-STATE INVARIANTS, not exit codes.

THE PLAN (a TS repo, node --test runner)
  s1-lower  clamp() lower bound. Clean path. During its coding job the Darkbloom stub
            RESTARTS: it answers 503, /health goes down, the key rotates, and
            local.json is rewritten WITHOUT base_url.
  s2-fmt    CREATION of lib/fmt.ts.
            - Author round 1 writes a JEST-API fixture (mock.fn().mockReturnValue)
              and burns to the ITERATION CAP.
            - A continuation round has to carry the Jest hint.
            - Preflight has to run after it.
            - The pregate review flags a finding, so the slice is escalated to a
              Darkbloom regate. That regate's strict schema rung answers 422.
  s3-upper  depends on s1; adds clampHigh().
            - The coding round writes the near-miss `clampHihg` and hits the cap.
            - The gate's auto-fix round must name the spelling.
            - The slicer must follow the auto-fix successor and must not mark the
              slice FAILED.

CONCURRENCY INJECTED
  - Two slicer drivers start at once.
  - A chaos thread keeps calling the slicer --execute (polling during auto-fix
    rounds), ack-reconcile and the escalation watcher.
  - Daemon-style state read-modify-write rewrites run under the queue's flock.
  - A FOREIGN bundle's job is enqueued while the canary bundle is committed. It must
    not launch until the canary bundle completes.

ISOLATION
  - A temp HOME with a copy of ~/bin, so every Path.home() state file lands in the
    sandbox.
  - A temp git repo.
  - PATH shims for security, osascript, darkbloom, launchctl, gh and claude, so
    there is no Keychain, no notifications and no real Darkbloom CLI.
  - DARKBLOOM_BASE_URL, hosts.json and GATE_QUEUE_API all point at the in-process
    stubs.
  - A dead proxy for anything else.
  - It never reads or writes the real queue, ledgers, vault or repos.
  - It does NOT set DISPATCH_VERIFY_SANDBOX, because the queue under test has to
    enqueue into the SANDBOX queue. The real-state guard is an INVARIANT instead:
    the real state is snapshotted before and after the run, and no canary label
    may appear in it.

  pipeline-canary.py                    # run, print PASS/FAIL summary
  pipeline-canary.py --keep             # keep the sandbox for inspection
  pipeline-canary.py --bin DIR          # run another bin/ (revert proofs)
  pipeline-canary.py --swap gate-on-complete.py=/path/to/old   # swap one file
  pipeline-canary.py --revert slice=OLD:NEW[@h1,h2]   # un-apply the OLD->NEW change
                                        # (or only those diff hunks) on top of current
  pipeline-canary.py --load 4           # with N CPU burners (flakiness under load)
  pipeline-canary.py --json             # machine-readable result on the last line
  pipeline-canary.py --status           # GREEN / FAIL / SKIPPED (pipeline files changed
                                        # since the last green run); exit 0 / 1 / 2

A plain run against the live ~/bin records its result in
~/.ollama-dispatch/canary/last.json (file hashes taken at start). The session-start
handoff panel shows "PIPELINE E2E: SKIPPED/FAIL" from it.

DETERMINISTIC WINDOWS. Two seams are too narrow for periodic chaos to hit reliably:
  - the gate deciding an auto-fix after a red job (GateWindowProbe polls the slicer
    inside it, SIGSTOPping the merging gate for the sub-second part);
  - the advance-lock acquire race (replayed against the sandbox slicer by
    test-slice-advance-lock-race.py).
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import pwd
import random
import re
import shlex
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
PREGATE_MODEL = "qwen3:14b"
LABEL_PREFIX = "pcanary"

# ---------------------------------------------------------------------------
# the repo + plan
# ---------------------------------------------------------------------------
TSCONFIG = json.dumps({"compilerOptions": {
    "target": "es2022", "module": "esnext", "moduleResolution": "bundler",
    "strict": True, "noEmit": True, "allowImportingTsExtensions": True,
    "skipLibCheck": True}, "include": ["lib/**/*.ts"]}, indent=2) + "\n"
REPO_FILES = {
    "lib/calc.ts": ("export function clamp(x: number, lo: number, hi: number): number {\n"
                    "  return x;\n}\n"),
    "tsconfig.json": TSCONFIG,
    "package.json": json.dumps({"name": "canary", "private": True, "type": "module",
                                "scripts": {"test": "node --experimental-strip-types --test lib/*.test.ts"}}) + "\n",
    ".gitignore": "/node_modules\n",
}

FMT_IMPL = ("export function formatRange(lo: number, hi: number): string {\n"
            "  return `[${lo}, ${hi}]`;\n}\n")
HIGH_IMPL = ("\nexport function clampHigh(x: number, hi: number): number {\n"
             "  return x >= hi ? hi - 1 : x;\n}\n")

SLICES = {
    "s1-lower": dict(
        target="lib/calc.ts", title="lower bound",
        intent=("clamp(x, lo, hi) must return lo when x < lo -- "
                "export function clamp(x: number, lo: number, hi: number): number"),
        literal="if (x < lo) return lo;", depends_on=[],
        verify_shape="CANARY-VS-S1: node:test unit test calling clamp() directly with plain numbers; no mocks",
        cases=[("below lo clamps up", "clamp(-5, 0, 3)", "0"),
               ("inside is unchanged", "clamp(2, 0, 3)", "2"),
               ("exactly lo stays lo", "clamp(0, 0, 3)", "0"),
               ("nonzero lo", "clamp(1, 2, 5)", "2")],
        sym="clamp", imp="./lib/calc.ts"),
    "s2-fmt": dict(
        target="lib/fmt.ts", title="range formatter (new file)",
        intent=("formatRange(lo, hi) must return the string [lo, hi] -- "
                "export function formatRange(lo: number, hi: number): string"),
        literal="export function formatRange(", depends_on=[],
        verify_shape="CANARY-VS-S2: node:test unit test importing formatRange from lib/fmt.ts; assert exact strings; no mocks",
        cases=[("formats a range", "formatRange(1, 3)", "'[1, 3]'"),
               ("formats negatives", "formatRange(-2, 0)", "'[-2, 0]'"),
               ("formats equal ends", "formatRange(4, 4)", "'[4, 4]'")],
        sym="formatRange", imp="./lib/fmt.ts"),
    "s3-upper": dict(
        target="lib/calc.ts", title="upper clamp helper",
        intent=("clampHigh(x, hi) must return hi - 1 when x >= hi, else x -- "
                "export function clampHigh(x: number, hi: number): number"),
        literal="export function clampHigh(", depends_on=["s1-lower"],
        verify_shape="CANARY-VS-S3: node:test unit test calling clampHigh() directly with plain numbers; no mocks",
        cases=[("above hi clamps down", "clampHigh(9, 3)", "2"),
               ("below hi unchanged", "clampHigh(2, 3)", "2"),
               ("exactly hi is capped below hi", "clampHigh(3, 3)", "2"),
               ("negative hi", "clampHigh(0, -1)", "-2")],
        sym="clampHigh", imp="./lib/calc.ts"),
}


def sid_of(cwd: str) -> str | None:
    name = Path(str(cwd or "")).name
    for sid in SLICES:
        if name.endswith("-" + sid):
            return sid
    return None


# ---------------------------------------------------------------------------
# the DRAFTER -- what the stub "author model" runs via run_bash in a worktree
# ---------------------------------------------------------------------------
def _node_test_body(sid: str, jest: bool) -> str:
    c = SLICES[sid]
    lines = ["import { test, mock } from 'node:test';" if jest else "import { test } from 'node:test';",
             "import assert from 'node:assert/strict';",
             f"import {{ {c['sym']} }} from '{c['imp']}';", ""]
    if jest:
        # the Jest API on node:test: TypeError "...mockReturnValue is not a function"
        lines.append("const spy = mock.fn().mockReturnValue(1);")
    for desc, call, want in c["cases"]:
        lines.append(f"test({desc!r}, () => {{ assert.equal({call}, {want}); }});")
    return "\n".join(lines) + "\n"


def _fill_task(wt: Path, sid: str):
    c = SLICES[sid]
    t = (wt / "TASK.md").read_text()
    out = []
    for b in t.split("\n\n"):
        if "TODO" not in b:
            out.append(b)
            continue
        if b.startswith("TODO -- state the CONFIRMED"):
            out.append(f"{c['sym']} lacks the property; observed: the canary baseline fails "
                       f"the cases below.")
        elif b.startswith("TODO -- ") and ":<line>" in b:
            out.append(f"{c['target']}:1")
        elif "Behaviour that must NOT change" in b:
            out.append("Behaviour that must NOT change:\n- every other export of the file "
                       "keeps its behaviour.")
        elif b.startswith("- `TODO"):
            out.append(f"- `{c['literal']}`")
        elif b.startswith("TODO -- state the property"):
            out.append(c["intent"])
        else:
            out.append(re.sub(r"TODO[^\n]*", "(n/a)", b))
    (wt / "TASK.md").write_text("\n\n".join(out))


def _refimpl(wt: Path, sid: str):
    c = SLICES[sid]
    if sid == "s1-lower":
        body = ("OLD = '{\\n  return x;\\n}'\nNEW = '{\\n  if (x < lo) return lo;\\n  return x;\\n}'\n"
                "assert OLD in t, 'refimpl anchor not found'\nt = t.replace(OLD, NEW, 1)\n")
    elif sid == "s2-fmt":
        body = f"t = {FMT_IMPL!r}\n"
    else:
        body = f"t = t.rstrip('\\n') + '\\n' + {HIGH_IMPL!r}\n"
    (wt / "refimpl.py").write_text(
        "#!/usr/bin/env python3\nimport pathlib, sys\n"
        "wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')\n"
        f"p = wt / {c['target']!r}\np.parent.mkdir(parents=True, exist_ok=True)\n"
        "t = p.read_text() if p.exists() else ''\n" + body +
        "p.write_text(t)\nprint('refimpl applied')\n")


def _fixtures(wt: Path):
    man = json.loads((wt / ".dispatch-harness.json").read_text())
    return [f for f in man.get("authored", [])
            if f not in ("TASK.md", "verify.sh", "check_literals.py", "refimpl.py")
            and not f.endswith((".gitignore",))]


def draft(sid: str, mode: str):
    wt = Path.cwd()
    _fill_task(wt, sid)
    _refimpl(wt, sid)
    fx = [f for f in _fixtures(wt) if f.endswith((".test.ts", ".test.mts"))]
    if not fx:
        raise SystemExit(f"drafter: no node-test fixture in the manifest: {_fixtures(wt)}")
    for rel in fx:
        (wt / rel).write_text(_node_test_body(sid, jest=(mode == "jest")))
    print(f"drafter[{sid}/{mode}]: filled TASK.md, refimpl.py, {fx}")


# ---------------------------------------------------------------------------
# the STUB model servers
# ---------------------------------------------------------------------------
class Obs:
    """Thread-safe observation log (what the stubs saw), persisted as jsonl."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.rows = []

    def add(self, kind, **kw):
        row = {"t": round(time.time(), 2), "kind": kind, **kw}
        with self.lock:
            self.rows.append(row)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")

    def find(self, kind, **kw):
        with self.lock:
            return [r for r in self.rows if r["kind"] == kind
                    and all(r.get(k) == v for k, v in kw.items())]


class FaultPlan:
    """--seed N: ONE reproducible fault schedule (the SOAK mode runs many seeds). Every
    choice comes from random.Random(seed); per-request model failure shapes come from a
    hash of (seed, request key), so the same seed injects the same shapes on the same
    requests regardless of thread timing. Thread interleavings themselves are only
    seeded as far as the chaos periods and fault times go."""
    SHAPES = ("http500", "cutoff", "badargs", "empty")

    def __init__(self, seed):
        r = random.Random(int(seed))
        self.seed = int(seed)
        self.periods = {"execute": r.uniform(5, 20), "ack": r.uniform(10, 40),
                        "watcher": r.uniform(15, 45), "rmw": r.uniform(1, 3),
                        "selfcheck": r.uniform(8, 30), "heal": r.uniform(20, 60)}
        self.kill_driver_at = r.uniform(8, 90) if r.random() < 0.7 else None
        self.resume_after = r.uniform(1, 10)
        self.daemon_restart_at = r.uniform(10, 150) if r.random() < 0.6 else None
        self.db_restart = {"k": r.randint(1, 6), "down": round(r.uniform(2, 8), 1),
                           "drop_base_url": r.random() < 0.7}
        self.shape_p = r.choice([0.0, 0.05, 0.1, 0.15])
        self.rng = random.Random(self.seed * 7919 + 1)   # chaos-thread-only choices

    def shape_for(self, key):
        h = int(hashlib.sha1(f"{self.seed}|{key}".encode()).hexdigest()[:12], 16) / float(16 ** 12)
        if h < self.shape_p:
            return self.SHAPES[int(h * 1e9) % len(self.SHAPES)]
        return None

    def describe(self):
        return {"seed": self.seed, "periods": {k: round(v, 1) for k, v in self.periods.items()},
                "kill_driver_at": self.kill_driver_at and round(self.kill_driver_at, 1),
                "resume_after": round(self.resume_after, 1),
                "daemon_restart_at": self.daemon_restart_at and round(self.daemon_restart_at, 1),
                "db_restart": self.db_restart, "shape_p": self.shape_p}


class StubState:
    def __init__(self, sb):
        self.sb = sb
        self.obs = sb.obs
        self.lock = threading.Lock()
        self.key = "canary-key-1"
        self.down_until = 0.0
        self.restart_done = False
        self.coding_turns = {}          # cwd -> count of coding requests seen
        self.review_422 = set()         # conversations already 422'd once
        self.injected = set()           # request keys a fault shape was injected on
        self.coding_seen = 0            # soak: coding/autofix requests seen (restart trigger)

    # -- Darkbloom restart: 503 + /health down, key rotation, base_url dropped --
    def maybe_restart(self, role, sid):
        fp = getattr(self.sb, "fault", None)
        if fp is not None:
            # SOAK: the restart lands on the k-th coding/autofix request of the run
            if self.sb.args.no_restart or self.restart_done or role not in ("coding", "autofix"):
                return
            with self.lock:
                self.coding_seen += 1
                if self.coding_seen != fp.db_restart["k"] or self.restart_done:
                    return
                self.restart_done = True
                self.down_until = time.time() + fp.db_restart["down"]
                self.key = "canary-key-2"
            lj = self.sb.home / ".darkbloom" / "local.json"
            tmp = lj.with_suffix(".tmp")
            rec = {"api_key": self.key}
            if not fp.db_restart["drop_base_url"]:
                rec["base_url"] = self.sb.db_url
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, lj)
            self.obs.add("restart", sid=sid, note=f"soak restart {fp.db_restart}")
            return
        if self.sb.args.no_restart or self.restart_done or role != "coding" or sid != "s1-lower":
            return
        with self.lock:
            n = self.coding_turns.get(sid, 0) + 1
            self.coding_turns[sid] = n
            if n != 2 or self.restart_done:
                return
            self.restart_done = True
            self.down_until = time.time() + 4.0
            self.key = "canary-key-2"
        lj = self.sb.home / ".darkbloom" / "local.json"
        tmp = lj.with_suffix(".tmp")
        tmp.write_text(json.dumps({"api_key": self.key}))       # 0.9.17 shape: no base_url
        os.replace(tmp, lj)
        self.obs.add("restart", sid=sid, note="503 + /health down 4s, key rotated, base_url dropped")

    def down(self):
        return time.time() < self.down_until


def _tc(name, args, i=0):
    return {"index": i, "id": f"call_{name}_{int(time.time() * 1000) % 100000}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _task_text(msgs):
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


def _system_text(msgs):
    return "\n".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")


def _role_of(task):
    if "## Gate feedback from previous attempt" in task or "[auto-fix r" in task:
        return "autofix"
    if task.lstrip().startswith("# CONTINUE") or "# CONTINUE --" in task[:400]:
        return "continue"
    if "# REFINE TASK" in task[:400]:
        return "refine"
    if "# AUTHORING TASK" in task[:600]:
        return "author"
    return "coding"


def _turns(msgs):
    """The assistant tool calls so far, in order (name list)."""
    out = []
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                # a soak-injected malformed call is not a step of the script
                if tc.get("id") == "call_bad":
                    continue
                try:
                    a = fn.get("arguments")
                    if isinstance(a, str) and a.strip():
                        json.loads(a)
                except ValueError:
                    continue
                out.append(fn.get("name"))
    return out


def _last_tool_result(msgs):
    for m in reversed(msgs):
        if m.get("role") == "tool":
            return str(m.get("content") or "")
    return ""


def plan_agent_turn(stub: StubState, msgs):
    """The scripted 'model': returns (tool_name, args) or None for a content-only turn."""
    sb = stub.sb
    sysm = _system_text(msgs)
    m = re.search(r"Your working directory is:\s*(\S[^\n]*)", sysm)
    cwd = (m.group(1).strip().rstrip(".") if m else "")
    task = _task_text(msgs)
    role = _role_of(task)
    sid = sid_of(cwd)
    turns = _turns(msgs)
    n = len(turns)
    drafter = f"{shlex.quote(sys.executable)} {shlex.quote(str(sb.bin / 'pipeline-canary.py'))}"
    key = (cwd, role, hashlib.sha1(task[:4000].encode()).hexdigest()[:10])
    if n == 0:
        stub.obs.add("agent-start", sid=sid, role=role, cwd=cwd,
                     verify_shape=bool(sid and SLICES[sid]["verify_shape"].split(":")[0] in task),
                     jest_hint=("JEST's API" in task), near_miss=("you wrote `clampHihg`" in task),
                     spelling=("SPELLING, not missing work" in task), task_head=task[:160])
    if "decoy" in cwd:
        if n == 0:
            return "write_file", {"path": "notes.txt", "content": "decoy done\n"}
        return "task_complete", {"summary": "decoy written"}
    if sid is None:
        return "task_complete", {"summary": "nothing to do"}
    stub.maybe_restart(role, sid)
    c = SLICES[sid]
    wt = Path(cwd)
    if role in ("author", "continue", "refine"):
        jest = (sid == "s2-fmt" and role == "author")
        if n == 0:
            return "run_bash", {"command": f"{drafter} --draft {sid} {'jest' if jest else 'good'}"}
        if jest:
            if n == 1:
                return "task_complete", {"summary": "fixture written"}
            # NON-CONVERGENCE: burn to the iteration cap with DISTINCT actions (an
            # identical re-read is a different failure shape: read thrash)
            return "run_bash", {"command": f"node -e 'console.log({n} * 7)'"}
        if role == "refine" and n == 1:
            # MUTFB survivor-feedback round (2026-10-09): the gate now feeds a surviving mutant
            # back to the author, and the refine self-check refuses "nothing changed yet". The
            # canary's s3 target is now `x >= hi ? hi - 1 : x` (NOT a clamp: a clamp's `>` vs `>=` is an equivalent, unkillable mutant that escalates, correctly), so a real, harmless edit of the
            # fixture is the faithful answer; the next preflight then stops on no-new-kills.
            return "run_bash", {"command": "printf '\\n// refined: equivalent boundary mutant reviewed\\n' >> verify.test.ts"}
        return "task_complete", {"summary": "harness authored"}
    # coding / autofix
    tgt = wt / c["target"]
    cur = tgt.read_text() if tgt.exists() else ""
    if n == 0:
        if sid == "s1-lower":
            new = cur.replace("{\n  return x;\n}", "{\n  if (x < lo) return lo;\n  return x;\n}", 1)
        elif sid == "s2-fmt":
            new = FMT_IMPL
        else:
            if role == "autofix" or "clampHihg" in cur:
                new = cur.replace("clampHihg", "clampHigh")
            else:
                new = cur.rstrip("\n") + "\n" + HIGH_IMPL.replace("clampHigh", "clampHihg")
        if new == cur and role == "autofix":
            return "task_complete", {"summary": "already fixed"}
        return "write_file", {"path": c["target"], "content": new}
    if sid == "s3-upper" and role == "coding" and "clampHihg" in cur:
        # the near-miss round: never converges (the verify is red on the spelling)
        if n == 1:
            return "task_complete", {"summary": "clampHigh added"}
        return "run_bash", {"command": f"node -e 'console.log({n} * 3)'"}
    return "task_complete", {"summary": f"{sid} done"}


def review_answer(stub: StubState, text: str, schema_kind: str, lane: str):
    """JSON content for a review/verdict call."""
    sid = None
    for s in SLICES:
        if SLICES[s]["target"] in text or SLICES[s]["sym"] in text:
            sid = s if (s != "s1-lower" or "clampHigh" not in text) else sid
    if "lib/fmt.ts" in text:
        sid = "s2-fmt"
    if schema_kind == "verdict":
        return {"verdict": "UNSURE" if lane == "unraid" else "NOT_A_DEFECT",
                "reason": "canary stub", "probe": "", "probe_shows_difference": False}
    if sid == "s2-fmt" and lane == "unraid":
        q = "return `[${lo}, ${hi}]`;"
        return {"findings": [{"quote": q, "severity": "medium",
                              "claim": "canary: formatRange may mis-handle NaN",
                              "failure_scenario": "formatRange(NaN, 1) returns the string [NaN, 1] instead of rejecting the input"}]}
    return {"findings": []}


def _schema_kind(schema):
    props = (schema or {}).get("properties") or {}
    if "verdict" in props:
        return "verdict"
    if "findings" in props:
        return "review"
    return "other"


class _Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json", raw=None):
        b = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}


def make_darkbloom_handler(stub: StubState):
    class H(_Quiet):
        def _auth_ok(self):
            return self.headers.get("Authorization", "") == "Bearer " + stub.key

        def do_GET(self):
            if self.path.startswith("/health"):
                if stub.down():
                    return self._send(503, {"status": "loading"})
                return self._send(200, {"status": "ok"})
            if self.path.startswith("/v1/models"):
                if stub.down():
                    return self._send(503, {"error": "restarting"})
                if not self._auth_ok():
                    stub.obs.add("auth-401", path=self.path)
                    return self._send(401, {"error": "bad key"})
                return self._send(200, {"data": [{"id": MODEL, "object": "model", "owned_by": "stub"}]})
            if self.path.startswith("/props"):
                return self._send(200, {"routes": [], "server": "mlx-server"})
            return self._send(404, {"error": "nope"})

        def do_POST(self):
            body = self._body()
            if not self.path.startswith("/v1/chat/completions"):
                return self._send(404, {"error": "nope"})
            if stub.down():
                stub.obs.add("db-503")
                return self._send(503, {"error": "provider restarting"})
            if not self._auth_ok():
                stub.obs.add("auth-401", path=self.path)
                return self._send(401, {"error": "stale key"})
            msgs = body.get("messages") or []
            rf = body.get("response_format")
            if body.get("tools") and len(msgs) == 1 and msgs[0].get("content") == "ready":
                return self._reply(body, {"role": "assistant", "content": "ready"}, "stop")
            fp = getattr(stub.sb, "fault", None)
            if body.get("tools") and fp is not None and fp.shape_p:
                _k = hashlib.sha1((_system_text(msgs)[:300] + "|" + _task_text(msgs)[:600]
                                   ).encode()).hexdigest()[:10] + f"|{len(_turns(msgs))}"
                shape = None if _k in stub.injected else fp.shape_for(_k)
                if shape:
                    stub.injected.add(_k)
                    stub.obs.add("fault-shape", shape=shape, key=_k)
                    if shape == "http500":
                        return self._send(500, {"error": "internal server error (soak)"})
                    if shape == "cutoff":
                        return self._reply(body, {"role": "assistant", "content":
                                                  "Let me think about this again. " * 200}, "length")
                    if shape == "badargs":
                        bad = {"index": 0, "id": "call_bad", "type": "function",
                               "function": {"name": "write_file", "arguments": '{"path": "lib/'}}
                        return self._reply(body, {"role": "assistant", "content": "",
                                                  "tool_calls": [bad]}, "tool_calls")
                    return self._reply(body, {"role": "assistant", "content": ""}, "stop")
            if body.get("tools"):
                step = plan_agent_turn(stub, msgs)
                if step is None:
                    return self._reply(body, {"role": "assistant", "content": "done"}, "stop")
                name, args = step
                return self._reply(body, {"role": "assistant", "content": "",
                                          "tool_calls": [_tc(name, args)]}, "tool_calls")
            # structured review / regate call (darkbloom_chat)
            text = "\n".join(str(m.get("content") or "") for m in msgs)
            schema = None
            rung = "none"
            if rf and rf.get("type") == "json_schema":
                js = rf.get("json_schema") or {}
                schema = js.get("schema")
                rung = "strict" if js.get("strict") else "relaxed"
            else:
                m = re.search(r"matching this JSON Schema, no prose, no code fences:\n(\{.*\})\s*$",
                              msgs[0].get("content") or "" if msgs else "", re.S)
                if m:
                    try:
                        schema = json.loads(m.group(1))
                    except ValueError:
                        schema = None
            kind = _schema_kind(schema)
            conv = hashlib.sha1(text[-3000:].encode()).hexdigest()[:12]
            stub.obs.add("db-review", rung=rung, schema=kind, conv=conv)
            if "CANARY-EXHAUST" in text and kind == "review":
                # SCHEMA RUNGS EXHAUSTED: strict + relaxed 422, unconstrained output is prose
                stub.obs.add("db-exhaust", rung=rung, conv=conv)
                if rung in ("strict", "relaxed"):
                    return self._send(422, {"error": "Generated output did not satisfy response_format"})
                return self._reply(body, {"role": "assistant",
                                          "content": "I think the change looks fine overall."}, "stop")
            if rung == "strict" and not stub.sb.args.no_422:
                # SCHEMA RUNG 422: Darkbloom's post-hoc validator rejects the strict rung
                stub.obs.add("db-422", schema=kind, conv=conv)
                return self._send(422, {"error": "Generated output did not satisfy response_format"})
            ans = review_answer(stub, text, kind, "darkbloom")
            return self._reply(body, {"role": "assistant", "content": json.dumps(ans)}, "stop")

        def _reply(self, body, message, finish):
            usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
            if not body.get("stream"):
                return self._send(200, {"id": "x", "object": "chat.completion", "model": MODEL,
                                        "choices": [{"index": 0, "message": message,
                                                     "finish_reason": finish}],
                                        "usage": usage})
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(delta["tool_calls"])]
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                      {"choices": [], "usage": usage}]
            raw = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return H


def make_ollama_handler(stub: StubState):
    """The Unraid lane: native Ollama /api/* for the pregate review (qwen3:14b)."""
    mrow = {"name": PREGATE_MODEL, "model": PREGATE_MODEL, "size": 9 * 1024 ** 3,
            "size_vram": 9 * 1024 ** 3, "digest": "0" * 12,
            "details": {"parameter_size": "14B", "quantization_level": "Q4_K_M", "family": "qwen3"}}

    class H(_Quiet):
        def do_GET(self):
            if self.path.startswith("/api/tags"):
                return self._send(200, {"models": [mrow]})
            if self.path.startswith("/api/ps"):
                return self._send(200, {"models": [dict(mrow, expires_at="2099-01-01T00:00:00Z",
                                                         context_length=6144)]})
            if self.path.startswith("/api/version"):
                return self._send(200, {"version": "0.12.0"})
            return self._send(200, {})

        def do_POST(self):
            body = self._body()
            if self.path.startswith("/api/show"):
                return self._send(200, {"details": mrow["details"],
                                        "model_info": {"qwen3.context_length": 40960},
                                        "capabilities": ["completion", "tools"]})
            if self.path.startswith("/api/generate"):
                return self._send(200, {"response": "", "done": True})
            if self.path.startswith("/api/chat"):
                msgs = body.get("messages") or []
                if body.get("tools"):
                    step = plan_agent_turn(stub, msgs)
                    name, args = step or ("task_complete", {"summary": "ok"})
                    return self._send(200, {"message": {"role": "assistant", "content": "",
                                                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                                            "done": True, "prompt_eval_count": 10, "eval_count": 10})
                text = "\n".join(str(m.get("content") or "") for m in msgs)
                fmt = body.get("format")
                kind = _schema_kind(fmt if isinstance(fmt, dict) else None)
                stub.obs.add("ollama-review", schema=kind)
                ans = review_answer(stub, text, kind, "unraid")
                return self._send(200, {"message": {"role": "assistant", "content": json.dumps(ans)},
                                        "done": True, "done_reason": "stop",
                                        "prompt_eval_count": 50, "eval_count": 10})
            return self._send(200, {"status": "success"})
    return H


def make_api_handler(stub: StubState):
    """Stand-in for the dashboard queue-api (gate janitor GET/POST /api/runs)."""
    class H(_Quiet):
        def do_GET(self):
            stub.obs.add("api-get", path=self.path)
            return self._send(200, {"runs": []})

        def do_POST(self):
            self._body()
            stub.obs.add("api-post", path=self.path)
            return self._send(200, {"ok": True})
    return H


class _TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_bloom_handler(stub):
    """Stub BloomGauge optimizer-control API (bloom_control.py talks to it). Without it the
    sandboxed queue daemon's bloom hook reached the REAL BloomGauge on 127.0.0.1:8765
    (a canary isolation leak) and, with no provider.toml, stalled every pair-model job."""
    class H(_Quiet):
        def _view(self):
            return {"controlVersion": "stub-v1", "automatic": {"mode": "manual", "canEnable": True},
                    "providerRunning": True, "providerVersion": "0.9.17", "currentModel": MODEL,
                    "manager": {"pinned": False}}

        def do_GET(self):
            if self.path.startswith("/api/optimizer/control"):
                return self._send(200, self._view())
            return self._send(404, {"error": "nope"})

        def do_POST(self):
            body = self._body()
            stub.obs.add("bloom-post", action=(body or {}).get("action") if isinstance(body, dict) else None)
            return self._send(200, self._view())
    return H


def pipe_capacity() -> int:
    """Bytes a fresh pipe holds before write() blocks. macOS shrinks new pipes to 512 B when the
    kernel is short of pipe memory (e.g. an app leaking ~2000 pipe fds); then any shell heredoc >512 B
    deadlocks in bash 5.3 (2026-10-08: verify.sh hung 'SELFCHECK_HANG at baseline')."""
    import fcntl
    r, w = os.pipe()
    try:
        fcntl.fcntl(w, fcntl.F_SETFL, os.O_NONBLOCK)
        n = 0
        try:
            while n < 70000:
                n += os.write(w, b"x" * 256)
        except BlockingIOError:
            pass
        return n
    finally:
        os.close(r)
        os.close(w)


def make_cpu_handler(stub: StubState):
    """Stand-in for the CPU lane API (/api/cpu/*): answers 'no runner online' so every pipeline CPU stage
    takes the local-fallback path inside the sandbox, and counts each contact."""
    class H(_Quiet):
        def do_GET(self):
            stub.obs.add("cpu-api-get", path=self.path)
            if self.path.startswith("/api/cpu/runners"):
                return self._send(200, {"runners": []})
            return self._send(200, {"ok": True})

        def do_POST(self):
            self._body()
            stub.obs.add("cpu-api-post", path=self.path)
            return self._send(503, {"error": "canary stub: no runner"})
    return H


def serve(handler):
    srv = _TS(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------
SHIMS = {
    "security": "exit 44",                    # no Keychain, ever
    "osascript": "exit 0",
    "launchctl": "exit 0",
    "gh": "exit 1",
    "claude": "exit 1",
    "darkbloom": ('if [ "$1" = status ]; then\n'
                  'cat <<EOF\ndarkbloom 0.9.17\nDaemon: running\n0 unfinished requests\n'
                  'Live load memory: 40.0 GB usable now\nRequests served: 1\n'
                  f'Warm models: {MODEL}\nServing concurrency: operator cap 2 {MODEL}=2\nEOF\n'
                  'fi\nexit 0'),
    "pmset": "exit 0",
}

# Sandbox-only ACCELERATION patches (poll intervals). Each is (file, old, new); a
# patch whose `old` is absent (an older bin under --bin/--swap) is skipped and noted.
ACCEL = [
    ("ollama-dispatch-auto", "        time.sleep(15)\n",
     "        time.sleep(float(os.environ.get('CANARY_POLL_S', '15')))\n"),
]


BIN_IGNORE = ("__pycache__", "node_modules", "*.log", "ollama-queue-logs",
              "ollama-worker-logs", "ollama-queue-livelogs", "*.jsonl",
              "ollama-queue-state.json*", "*.bak*", "*.orig", "*.corrupt-*",
              ".git", "ollama-queue-daemon.lock*")


def copy_bin(src_bin: Path, dst: Path):
    shutil.copytree(src_bin, dst, symlinks=True, ignore=shutil.ignore_patterns(*BIN_IGNORE))
    shutil.copy2(__file__, dst / "pipeline-canary.py")


def _resolve_bak(src_bin: Path, name: str, ref: str) -> Path:
    """A --revert operand: a path, a backup basename in src_bin, or '' (= current FILE)."""
    if not ref:
        return src_bin / name
    p = Path(ref).expanduser()
    return p if p.is_absolute() or p.exists() else src_bin / ref


def _detach_symlink(p: Path):
    """The sandbox bin keeps ~/bin's symlinks (ollama-queue-api.py, bundle_view.py point
    into the dashboard repo). A proof/revert WRITES the file, which through a symlink
    would mutate the REAL repo file. Replace the link with a private copy first."""
    if p.is_symlink():
        data = p.read_bytes()
        p.unlink()
        p.write_bytes(data)
        try:
            p.chmod(0o755)
        except OSError:
            pass


def apply_revert(bin_dir: Path, spec: str, src_bin: Path):
    """--revert FILE=OLD:NEW[@h1,h2] -- un-apply the OLD->NEW change on top of the
    FILE in bin_dir (later unrelated fixes stay in). OLD/NEW: paths or backup
    basenames in src_bin; NEW empty = the current src_bin/FILE.
    Returns (status, note): status in clean | fuzz | partial."""
    name, _, pair = spec.partition("=")
    pair, _, sel = pair.partition("@")       # optional @1,3 = only those hunks
    old, _, new = pair.partition(":")
    oldp, newp = _resolve_bak(src_bin, name, old), _resolve_bak(src_bin, name, new)
    d = subprocess.run(["diff", "-u", str(newp), str(oldp)], capture_output=True, text=True).stdout
    if sel:
        keep = {int(x) for x in sel.split(",") if x.strip()}
        parts = re.split(r"(?m)^(?=@@ )", d)
        d = parts[0] + "".join(h for i, h in enumerate(parts[1:], 1) if i in keep)
    if not d.strip():
        return "partial", f"{name}: empty revert diff ({oldp.name} == {newp.name})"
    target = bin_dir / name
    _detach_symlink(target)
    orig = target.read_bytes()
    status = "clean"
    r = subprocess.run(["patch", "--no-backup-if-mismatch", "-s", "-f", str(target)],
                       input=d, capture_output=True, text=True)
    if r.returncode != 0:
        # later edits moved a hunk's context: retry the whole patch with fuzz 3 on a
        # clean copy (noted, so a misapplied revert is visible)
        target.write_bytes(orig)
        r = subprocess.run(["patch", "--no-backup-if-mismatch", "-s", "-f", "-F3", str(target)],
                           input=d, capture_output=True, text=True)
        status = "fuzz" if r.returncode == 0 else "partial"
    for rej in bin_dir.glob(name + ".rej"):
        rej.unlink()
    note = f"{name} -= ({oldp.name} -> {newp.name})"
    if status == "partial":
        note += f" [PARTIAL] {r.stdout.strip()[:300]} {r.stderr.strip()[:200]}"
    elif status == "fuzz":
        note += " [fuzz 3]"
    return status, note


def make_shims(root: Path) -> Path:
    shim = root / "shim"
    shim.mkdir(exist_ok=True)
    for name, body in SHIMS.items():
        p = shim / name
        p.write_text(f"#!/bin/bash\necho \"$(date +%s) {name} $*\" >> {shlex.quote(str(root / 'shim.log'))}\n{body}\n")
        p.chmod(0o755)
    return shim


def base_env(root: Path, home: Path, shim: Path) -> dict:
    """The isolated environment every sandboxed process gets (no real Keychain,
    queue, vault, proxy or notifications)."""
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("OLLAMA_", "GATE_", "DISPATCH_", "SLICE_", "DARKBLOOM_",
                                 "VAULT_", "OBSIDIAN_", "AUTO_", "BONSAI_", "LANE_",
                                 "BLOOM", "CPU_"))
            and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    gc = home / ".gitconfig"
    if not gc.exists():
        gc.write_text("[user]\n\tname = canary\n\temail = canary@example.invalid\n"
                      "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
    env = dict(keep)
    env.update({
        "HOME": str(home), "GIT_CONFIG_GLOBAL": str(gc), "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": f"{shim}:{keep.get('PATH', '/usr/bin:/bin')}",
        "PYTHONDONTWRITEBYTECODE": "1", "npm_config_offline": "true", "NO_UPDATE_NOTIFIER": "1",
        "OLLAMA_QUEUE_NO_NOTIFY": "1",
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost",
        "CANARY_SANDBOX": str(root),
        # CPU lane (Phase 6): NEVER the real queue API / runner from the sandbox. Dead port by default;
        # the e2e Sandbox swaps in a counting stub that reports "no runner online".
        "CPU_RUNNER_API": "http://127.0.0.1:9",
        "CPU_RUNNER_TOKEN_FILE": str(home / ".config" / "dispatch-cpu-runner" / "token"),
    })
    return env


class Sandbox:
    def __init__(self, root: Path, src_bin: Path, args):
        self.root = root
        self.args = args
        self.home = root / "home"
        self.bin = self.home / "bin"
        self.home.mkdir(parents=True)
        self.obs = Obs(root / "observations.jsonl")
        copy_bin(src_bin, self.bin)
        self.swapped = []
        for sw in args.swap or []:
            name, _, src = sw.partition("=")
            shutil.copy2(src, self.bin / name)
            self.swapped.append(f"{name} <- {src}")
        # --revert FILE=OLD:NEW  un-applies the OLD->NEW change on top of the
        # current FILE (so later unrelated fixes stay in). Used for revert proofs.
        self.revert_fail = []
        for rv in args.revert or []:
            status, note = apply_revert(self.bin, rv, src_bin)
            if status == "partial":
                self.revert_fail.append(note)
            self.swapped.append(note)
        self.accel_notes = []
        for fn, old, new in ACCEL:
            p = self.bin / fn
            t = p.read_text()
            if old in t:
                p.write_text(t.replace(old, new, 1))
            else:
                self.accel_notes.append(f"accel patch not applied to {fn}")
        nm = None
        for cand in (src_bin / "ts-mutator" / "node_modules", REAL_HOME / "bin" / "ts-mutator" / "node_modules"):
            if (cand / "typescript").is_dir():
                nm = cand
                break
        self.ts_nm = nm
        if nm and (self.bin / "ts-mutator").is_dir():
            (self.bin / "ts-mutator" / "node_modules").symlink_to(nm)
        self.shim = make_shims(root)
        dh = self.home / ".ollama-dispatch"
        dh.mkdir()
        (self.home / ".darkbloom").mkdir()
        self.fault = FaultPlan(args.seed) if getattr(args, "seed", None) is not None else None
        self.stub = StubState(self)
        self.db_srv, self.db_url = serve(make_darkbloom_handler(self.stub))
        self.ol_srv, self.ol_url = serve(make_ollama_handler(self.stub))
        self.bl_srv, self.bl_url = serve(make_bloom_handler(self.stub))
        # the bloom_control hook (queue daemon) must find the pair SERVED: provider.toml,
        # /health, /v1/models and loaded-models.json agree on exactly the stub's model
        (self.home / ".config" / "darkbloom").mkdir(parents=True)
        (self.home / ".config" / "darkbloom" / "provider.toml").write_text(
            "[backend]\nmax_model_slots = 2\nidle_timeout_mins = 0\n"
            f"preload_models = [ '{MODEL}' ]\nenabled_models = [ '{MODEL}' ]\nauto_update = false\n")
        (self.home / ".darkbloom" / "loaded-models.json").write_text(json.dumps({"models": [MODEL]}))
        self.api_srv, self.api_url = serve(make_api_handler(self.stub))
        self.cpu_srv, self.cpu_url = serve(make_cpu_handler(self.stub))
        (self.home / ".darkbloom" / "local.json").write_text(
            json.dumps({"base_url": self.db_url, "api_key": self.stub.key}))
        (dh / "hosts.json").write_text(json.dumps({"hosts": {
            "unraid": {"url": self.ol_url, "usable_bytes": 10307921510}}}, indent=1))
        (dh / "defaults.json").write_text(json.dumps({"model": MODEL, "host": "studio"}))
        for f in ("model-ladder.json",):
            src = REAL_HOME / ".ollama-dispatch" / f
            if src.is_file():
                shutil.copy2(src, dh / f)
        self.seam_env = base_env(root, self.home, self.shim)
        self.env = dict(self.seam_env)
        self.env.update({
            # production gate env (com.example.ollama-queue-daemon.plist)
            "GATE_AUTOFIX_MODE": "live", "GATE_TWO_TIER": "1", "GATE_PREGATE_HOST": "unraid",
            "GATE_PREGATE_MODEL": PREGATE_MODEL, "GATE_PREGATE_NUM_CTX": "6144",
            "BONSAI_CTX": "65536",
            # isolation
            "DARKBLOOM_BASE_URL": self.db_url,
            "DARKBLOOM_CTX": "65536", "GATE_QUEUE_API": self.api_url, "CPU_RUNNER_API": self.cpu_url,
            "BLOOMCTL_PAIR": MODEL, "BLOOMCTL_BLOOM_URL": self.bl_url,
            "BLOOMCTL_WAIT_S": "30", "BLOOMCTL_DRAIN_S": "5",
            # speed (poll intervals only)
            "CANARY_POLL_S": "2", "AUTO_OPERATIONAL_POLL_S": "2",
            "LANE_RESTART_WAIT_S": "60",
            # SCENARIO PINS (2026-10-09 drift fix): this 3-slice script predates (a) staged
            # authoring (ODS_STAGED_AUTHOR: one job per TASK/fixture/refimpl stage -- the
            # scripted stub answers the single legacy author round, and the stage-1 enqueue was
            # refused by the ctx gate, so rounds/labels no longer matched; the w3staged seam
            # covers the staged flow itself) and (b) the mutant-feedback refine loop (MUTFB:
            # s3's fixture leaves an equivalent surviving mutant, so an `auto-refine` round the
            # stub answered with a no-op was enqueued and paused 'verify_uninformative', leaving the
            # canary bundle with no live job so the foreign decoy launched; the stub now edits).
            "ODS_STAGED_AUTHOR": "0",
            # (c) the queue's CONTEXT GATE: the sandbox's AUTO-TASK names auto-harness-check.py
            # (~75KB) and dispatch-env.ts (~60KB), so the char estimate (~45k tokens) is refused against
            # the stub model's 65536 ceiling and the first author round was never enqueued (every slice
            # started at a -c1 continuation, so the scripted jest round never ran). A real host's ceiling
            # is larger; pin one here.
            "DARKBLOOM_CTX": "131072",
        })
        self.procs = []
        self.label = f"{LABEL_PREFIX}-{int(time.time()) % 100000:05d}{os.getpid() % 100:02d}"
        self.bundle = self.label
        self.repo = None
        self.plan_path = None
        self.landed = False
        self.land_note = ""
        self.probe = None
        self.init_sha = "main"
        self.staged_seen = False

    # -- processes --
    def popen(self, cmd, log_name, cwd=None, extra=None):
        env = dict(self.env, **(extra or {}))
        logf = open(self.root / log_name, "ab")
        p = subprocess.Popen(cmd, cwd=cwd or str(self.root), env=env, stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def run(self, cmd, timeout=600, cwd=None, log_name=None):
        p = subprocess.run(cmd, cwd=cwd or str(self.root), env=self.env, capture_output=True, text=True,
                           timeout=timeout)
        if log_name:
            with (self.root / log_name).open("a") as f:
                f.write(f"\n$ {' '.join(map(str, cmd))}\nrc={p.returncode}\n{p.stdout}{p.stderr}")
        return p

    def py(self, tool, *a, **kw):
        return self.run([sys.executable, str(self.bin / tool), *map(str, a)], **kw)

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        time.sleep(1.5)
        # anything still alive whose command line names THIS sandbox (detached
        # advances, gate children, workers) -- never anything else
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        except Exception:
            out = ""
        me = os.getpid()
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if str(self.root) in cmd and pid.isdigit() and int(pid) != me:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        for s in (self.db_srv, self.ol_srv, self.api_srv):
            try:
                s.shutdown()
            except Exception:
                pass

    # -- state readers --
    def qstate(self):
        try:
            return json.loads((self.bin / "ollama-queue-state.json").read_text())
        except (OSError, ValueError):
            return None

    def slice_state(self):
        p = self.home / ".ollama-dispatch" / "slice-runs" / f"{self.label}.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_AUTHOR_NAME": "c", "GIT_AUTHOR_EMAIL": "c@x",
                               "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": "c@x"})


def make_repo(sb: Sandbox) -> Path:
    r = sb.root / "repo"
    for rel, body in REPO_FILES.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    sb.init_sha = git(r, "rev-parse", "HEAD").stdout.strip()
    nm = sb.root / "nm"
    (nm / ".bin").mkdir(parents=True)
    for pkg in ("typescript", "tsx", "esbuild", "@esbuild", "get-tsconfig", "resolve-pkg-maps"):
        if sb.ts_nm and (sb.ts_nm / pkg).exists():
            (nm / pkg).symlink_to(sb.ts_nm / pkg)
    for b in ("tsx", "tsc"):
        if sb.ts_nm and (sb.ts_nm / ".bin" / b).exists():
            (nm / ".bin" / b).symlink_to((sb.ts_nm / ".bin" / b).resolve())
    (r / "node_modules").symlink_to(nm)
    return r


def write_plan(sb: Sandbox) -> Path:
    plan = {"repo": str(sb.repo), "target": "lib/calc.ts", "lang": "ts", "label": sb.label,
            "bundle": sb.bundle, "slices": []}
    for sid, c in SLICES.items():
        s = {"id": sid, "title": c["title"], "intent": c["intent"], "depends_on": c["depends_on"],
             "must_contain": [c["literal"]], "verify_shape": c["verify_shape"]}
        if c["target"] != plan["target"]:
            s["target"] = c["target"]
        plan["slices"].append(s)
    pp = sb.root / f"{sb.label}.plan.json"
    pp.write_text(json.dumps(plan, indent=1))
    return pp


# ---------------------------------------------------------------------------
# real-state guard
# ---------------------------------------------------------------------------
def real_snapshot():
    """Labels/ids in the REAL queue state + slice-runs names (read-only)."""
    out = {"labels": set(), "ids": set(), "slice_runs": set(), "auto_runs": set(), "cpu_bundles": set()}
    try:       # the REAL CPU lane store: a canary stage must never register a job / local marker there
        import sqlite3
        c = sqlite3.connect("file:%s?mode=ro" % (REAL_HOME / ".ollama-dispatch" / "cpu-jobs" / "jobs.sqlite"),
                            uri=True, timeout=5)
        out["cpu_bundles"] = {str(r[0]) for r in c.execute("SELECT bundle_id FROM jobs") if r[0]}
        c.close()
    except Exception:
        pass
    try:
        st = json.loads((REAL_HOME / "bin" / "ollama-queue-state.json").read_text())
        for j in st.get("jobs") or []:
            out["labels"].add(str(j.get("label")))
            out["ids"].add(str(j.get("id")))
    except (OSError, ValueError):
        pass
    for d, k in ((REAL_HOME / ".ollama-dispatch" / "slice-runs", "slice_runs"),
                 (REAL_HOME / ".ollama-dispatch" / "auto-runs", "auto_runs")):
        try:
            out[k] = {p.name for p in d.iterdir()}
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------
# concurrency: chaos + monitor
# ---------------------------------------------------------------------------
LIVE = ("pending", "running", "queued", "held", "paused", "scheduled")


class Monitor(threading.Thread):
    def __init__(self, sb: Sandbox):
        super().__init__(daemon=True)
        self.sb = sb
        self.stop = threading.Event()
        self.violations = []
        self.torn = 0
        self.decoy_started_at = None
        self.bundle_done_at = None
        self.samples = 0
        self.failed_seen = {}
        self.history = {}           # sid -> [statuses in order seen]

    def _v(self, what):
        if what not in self.violations:
            self.violations.append(what)
            self.sb.obs.add("violation", what=what)

    def run(self):
        while not self.stop.wait(1.0):
            try:
                self.tick()
            except Exception as e:
                self.sb.obs.add("monitor-error", err=repr(e))

    def tick(self):
        sb = self.sb
        qp = sb.bin / "ollama-queue-state.json"
        try:
            q = json.loads(qp.read_text()) if qp.exists() else {"jobs": []}
        except ValueError:
            self.torn += 1
            if self.torn >= 2:
                self._v("queue state file read torn/unparseable twice")
            return
        except OSError:
            return
        st = sb.slice_state() or {}
        self.samples += 1
        jobs = q.get("jobs") or []
        live_cwds = {os.path.realpath(str(j.get("cwd"))): j for j in jobs
                     if j.get("status") in LIVE}
        for sid, s in (st.get("slices") or {}).items():
            h = self.history.setdefault(sid, [])
            if not h or h[-1] != s.get("status"):
                h.append(s.get("status"))
                self.sb.obs.add("slice-status", sid=sid, status=s.get("status"),
                                job=s.get("job_id"), reason=str(s.get("failure_reason") or "")[:200])
            if s.get("status") == "failed" and s.get("worktree"):
                j = live_cwds.get(os.path.realpath(s["worktree"]))
                if j:
                    n = self.failed_seen.get(sid, 0) + 1
                    self.failed_seen[sid] = n
                    if n >= 3:   # 3 consecutive samples: not a transient write order
                        self._v(f"{sid} FAILED while live job {j.get('id')} "
                                f"({j.get('label')}, {j.get('status')}) is working its worktree")
                else:
                    self.failed_seen[sid] = 0
        for j in jobs:
            if "decoy" in str(j.get("label")) and j.get("status") not in ("pending", "held", "queued") \
                    and self.decoy_started_at is None:
                self.decoy_started_at = time.time()
                canary_live = [x for x in jobs if str(x.get("label", "")).startswith(sb.label)
                               and x.get("status") in LIVE and "decoy" not in str(x.get("label"))]
                sl = st.get("slices") or {}
                undone = [k for k, v in sl.items() if v.get("status") not in ("done", "skipped")]
                if undone:
                    self._v(f"foreign-bundle decoy launched while the canary bundle was still "
                            f"working (undone slices {undone}, live canary jobs "
                            f"{[x.get('id') for x in canary_live]})")
        sl = st.get("slices") or {}
        if sl and all(v.get("status") in ("done", "skipped") for v in sl.values()) \
                and self.bundle_done_at is None:
            self.bundle_done_at = time.time()


class Chaos(threading.Thread):
    def __init__(self, sb: Sandbox, slicer_cmd):
        super().__init__(daemon=True)
        self.sb = sb
        self.slicer_cmd = slicer_cmd
        self.stop = threading.Event()
        self.counts = {"execute": 0, "ack": 0, "watcher": 0, "rmw": 0}

    def _cwd_owned(self, wt) -> bool:
        """A queue job that is not terminal (pending/queued/running/...) has this cwd.
        Unreadable state -> owned (do not inject blind)."""
        try:
            st = json.loads((self.sb.bin / "ollama-queue-state.json").read_text())
            jobs = st.get("jobs") or []
            if isinstance(jobs, dict):
                jobs = list(jobs.values())
            mine = str(Path(wt).resolve())
            for j in jobs:
                if not isinstance(j, dict) or not j.get("cwd"):
                    continue
                if j.get("status") in ("pending", "queued", "running", "scheduled", "held",
                                       "paused", "yielded") and \
                        str(Path(j["cwd"]).resolve()) == mine:
                    return True
            return False
        except Exception:
            return True

    def run(self):
        t0 = time.time()
        last = {"execute": t0, "ack": t0, "watcher": t0, "selfcheck": t0, "heal": t0}
        fp = getattr(self.sb, "fault", None)
        per = fp.periods if fp else {"execute": 12, "ack": 25, "watcher": 30, "rmw": 2.0}
        self.counts.update({"selfcheck": 0, "heal": 0})
        rmw = (f"import importlib.util,sys\n"
               f"s=importlib.util.spec_from_file_location('oq',{str(self.sb.bin / 'ollama-queue.py')!r})\n"
               f"q=importlib.util.module_from_spec(s); s.loader.exec_module(q)\n"
               f"with q._Locked() as L:\n    st=L.load(); L.save(st)\n")
        while not self.stop.wait(per["rmw"]):
            now = time.time()
            try:
                self.sb.run([sys.executable, "-c", rmw], timeout=60)
                self.counts["rmw"] += 1
                if fp and now - last["selfcheck"] > per["selfcheck"]:
                    # the auto self-check racing the gate / slicer on a live worktree
                    last["selfcheck"] = now
                    wts = sorted((self.sb.home / ".ollama-dispatch" / "worktrees").glob(
                        "*/auto-harness-check.py"))
                    # Production only self-checks a tree NO queue job owns (auto runs it
                    # between its own authoring rounds; preflight's cwd-exclusive and the
                    # slicer's PENDING resume both refuse an owned tree). Injecting it into a
                    # tree with a live/pending coding job is not a real interleaving: its
                    # revert() wiped the auto-fix model's write mid-run (soak seed 7).
                    wts = [p for p in wts if not self._cwd_owned(p.parent)]
                    if wts:
                        w = fp.rng.choice(wts).parent
                        # as production does it (ollama-dispatch-auto self-check): under
                        # the worktree's tree lock, which the queue launch now honours
                        locked = ("import fcntl,subprocess,sys\n"
                                  "g=subprocess.run(['git','rev-parse','--absolute-git-dir'],"
                                  "capture_output=True,text=True).stdout.strip()\n"
                                  "fh=open(g+'/dispatch-tree.lock','a+')\n"
                                  "fcntl.flock(fh.fileno(),fcntl.LOCK_EX)\n"
                                  # re-check ownership UNDER the lock (a launch may have
                                  # slipped in between the pre-filter and the flock)
                                  "import json,os\n"
                                  f"sp={str(self.sb.bin / 'ollama-queue-state.json')!r}\n"
                                  "J=json.load(open(sp)).get('jobs') or []\n"
                                  "J=list(J.values()) if isinstance(J,dict) else J\n"
                                  "me=os.path.realpath('.')\n"
                                  "if any(isinstance(j,dict) and j.get('cwd') and os.path.realpath(j['cwd'])==me "
                                  "and j.get('status') in ('pending','queued','running','scheduled','held','paused','yielded') "
                                  "for j in J):\n"
                                  "    print('skip: a queue job owns this tree'); sys.exit(0)\n"
                                  "sys.exit(subprocess.run([sys.executable,'auto-harness-check.py'])"
                                  ".returncode)\n")
                        self.sb.run([sys.executable, "-c", locked], cwd=str(w),
                                    timeout=300, log_name="chaos-selfcheck.log")
                        self.counts["selfcheck"] += 1
                if fp and now - last["heal"] > per["heal"]:
                    last["heal"] = now
                    for sw in ("--job-sweep", "--hygiene-sweep", "--retire-sweep"):
                        self.sb.py("dispatch-self-heal.py", sw, timeout=300, log_name="chaos-heal.log")
                    self.counts["heal"] += 1
                if now - last["execute"] > per["execute"]:
                    last["execute"] = now
                    self.sb.popen(self.slicer_cmd, "chaos-execute.log")
                    self.counts["execute"] += 1
                if now - last["ack"] > per["ack"] and (self.sb.bin / "dispatch-ack-reconcile.py").exists():
                    last["ack"] = now
                    self.sb.py("dispatch-ack-reconcile.py", timeout=120, log_name="chaos-ack.log")
                    self.counts["ack"] += 1
                if now - last["watcher"] > per["watcher"] and (self.sb.bin / "dispatch-escalation-watcher.py").exists():
                    last["watcher"] = now
                    self.sb.py("dispatch-escalation-watcher.py", "--once", timeout=300,
                               log_name="chaos-watcher.log")
                    self.counts["watcher"] += 1
            except Exception as e:
                self.sb.obs.add("chaos-error", err=repr(e)[:300])


class GateWindowProbe(threading.Thread):
    """Fires a slicer --execute INSIDE the windows between the queue reaping s3's red
    coding job and the gate requeueing its auto-fix round -- where a poll used to
    mark the slice FAILED while the gate was still deciding:
      1. job reaped, gate + its review running (seconds wide);
      2. review merged (verdict=fail written) but the auto-fix decision not yet
         made (sub-second): the merging gate process is SIGSTOPped for the poll,
         then resumed, so this window is hit deterministically too.
    Periodic chaos only hits these by luck; this makes the revert proof reliable."""

    def __init__(self, sb, slicer_cmd):
        super().__init__(daemon=True)
        self.sb, self.slicer_cmd = sb, slicer_cmd
        self.stop = threading.Event()
        self.fired = 0
        self.in_window = 0
        self.merge_probe = None    # None = not reached; else "frozen"/"missed"

    def _gate_pids(self, jid):
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        except OSError:
            return []
        pids = []
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if "gate-on-complete.py" in cmd and f"gate-{jid}" in cmd and str(self.sb.root) in cmd:
                pids.append(int(pid))
        return pids

    def run(self):
        log = self.sb.root / "daemon.log"
        lab = re.escape(f"{self.sb.label}-s3-upper")
        launched = re.compile(r"launched (\w+) \(" + lab + r"\)")
        red = re.compile(r"\(" + lab + r"\) FAILED on")
        fix = re.compile(r"\(" + lab + r" \[auto-fix r1\]\)")
        while not self.stop.wait(0.05):
            try:
                t = log.read_text(errors="replace")
            except OSError:
                continue
            if not red.search(t):
                continue
            if fix.search(t):
                if self.merge_probe is None:
                    self.merge_probe = "missed"
                return
            m = launched.search(t)
            jid = m.group(1) if m else None
            rec = None
            if jid and self.merge_probe is None:
                try:
                    rec = json.loads((self.sb.bin / "ollama-queue-logs" / f"{jid}.gate.json").read_text())
                except (OSError, ValueError):
                    rec = None
            if isinstance(rec, dict) and rec.get("review") == "done" and (
                    "auto_fix_action" not in rec
                    or (rec.get("auto_fix_action") == "requeue"
                        and "auto_fix_enqueue_status" not in rec)):
                # review merged, auto-fix not decided: freeze the merging gate here
                pids = self._gate_pids(jid)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except OSError:
                        pass
                try:
                    self.merge_probe = "frozen" if pids else "no-gate-pid"
                    self.sb.run(self.slicer_cmd, timeout=120, log_name="chaos-window.log")
                    self.fired += 1
                    self.in_window += 1
                finally:
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGCONT)
                        except OSError:
                            pass
                continue
            if self.fired >= 2:
                continue
            self.fired += 1
            self.sb.run(self.slicer_cmd, timeout=120, log_name="chaos-window.log")
            try:
                if not fix.search(log.read_text(errors="replace")):
                    self.in_window += 1
            except OSError:
                pass


class FaultInjector(threading.Thread):
    """SOAK: kill the slicer drivers mid-run and resume one; restart the queue daemon
    (SIGTERM, then a fresh `run`), both at the seeded times."""

    def __init__(self, sb, slicer_cmd, daemon_cmd, t0):
        super().__init__(daemon=True)
        self.sb, self.slicer_cmd, self.daemon_cmd, self.t0 = sb, slicer_cmd, daemon_cmd, t0
        self.stop = threading.Event()
        self.done = []

    def run(self):
        fp = self.sb.fault
        ev = sorted([(t, k) for t, k in ((fp.kill_driver_at, "kill"), (fp.daemon_restart_at, "daemon"))
                     if t is not None])
        for at, kind in ev:
            if self.stop.wait(max(0.0, self.t0 + at - time.time())):
                return
            try:
                if kind == "kill":
                    # the DRIVERS: every slicer --execute / detached advance and every
                    # ollama-dispatch-auto of THIS sandbox (SIGKILL: no cleanup runs)
                    killed = []
                    for p in list(getattr(self.sb, "drivers", [])):
                        if p.poll() is None:
                            try:
                                os.killpg(p.pid, signal.SIGKILL)
                                killed.append(p.pid)
                            except OSError:
                                pass
                    try:
                        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                            text=True).stdout
                    except Exception:
                        ps = ""
                    for line in ps.splitlines():
                        pid, _, cmd = line.strip().partition(" ")
                        if (str(self.sb.root) in cmd and pid.isdigit()
                                and ("ollama-dispatch-auto" in cmd or "ollama-dispatch-slice" in cmd)):
                            try:
                                os.kill(int(pid), signal.SIGKILL)
                                killed.append(int(pid))
                            except OSError:
                                pass
                    self.sb.obs.add("fault-kill-driver", pids=killed)
                    if self.stop.wait(fp.resume_after):
                        return
                    self.sb.drivers.append(self.sb.popen(self.slicer_cmd, "driver-resume.log"))
                    self.done.append(f"kill{killed}+resume@{at:.0f}s")
                else:
                    d = self.sb.daemon
                    try:
                        os.kill(d.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    try:
                        d.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        try:
                            os.kill(d.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        d.wait(timeout=10)
                    self.sb.obs.add("fault-daemon-restart", rc=d.returncode)
                    time.sleep(1.0)
                    self.sb.daemon = self.sb.popen(self.daemon_cmd, "daemon.log")
                    self.done.append(f"daemon-restart@{at:.0f}s rc={d.returncode}")
            except Exception as e:
                self.sb.obs.add("fault-error", err=repr(e)[:300])


class Loader:
    """--load N: CPU burners so timing-dependent seams are exercised under load."""

    def __init__(self, n):
        self.procs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(n)]

    def stop(self):
        for p in self.procs:
            p.kill()


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
class Result:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, note=""):
        self.checks.append((name, bool(ok), note))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {note}" if note and not ok else ""), flush=True)
        return ok

    @property
    def ok(self):
        return all(c[1] for c in self.checks)


def enqueue_decoy(sb: Sandbox):
    d = sb.root / "decoy"
    d.mkdir(exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    (d / "README").write_text("decoy\n")
    (d / "check.py").write_text("import pathlib,sys\nsys.exit(0 if pathlib.Path('notes.txt').is_file() else 1)\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "init")
    (d / "TASK.md").write_text("# TASK\nCreate notes.txt containing 'decoy done'. Only edit notes.txt.\n")
    p = sb.py("ollama-queue.py", "enqueue", "--task-file", d / "TASK.md", "--cwd", d,
              "--label", f"{sb.label}-decoy", "--model", MODEL, "--host", "studio",
              "--verify", "python3 check.py", "--bundle", f"{sb.label}-foreign",
              "--task-kind", "coding", "--max-iters", "6", log_name="decoy.log")
    m = re.search(r"\b([0-9a-f]{12})\b", p.stdout or "")
    sb.obs.add("decoy-enqueued", rc=p.returncode, id=m.group(1) if m else None,
               out=(p.stdout + p.stderr)[-300:])
    return m.group(1) if m else None


def wait_for(pred, timeout, step=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def canary(args) -> tuple[Result, Sandbox]:
    # .resolve(): macOS /var -> /private/var; the queue compares RESOLVED runner paths
    # against Path.home()-built allowlist entries, so HOME must have no symlink in it
    root = Path(tempfile.mkdtemp(prefix="pipeline-canary-")).resolve()
    sb = Sandbox(root, Path(args.bin).resolve(), args)
    R = Result()
    before = real_snapshot()
    print(f"sandbox: {root}  bin: {args.bin}  label: {sb.label}"
          + (f"  swapped: {sb.swapped}" if sb.swapped else ""), flush=True)
    _pc = pipe_capacity()
    if _pc < 4096:
        print(f"WARNING: this machine is handing out {_pc}-byte pipes (kernel pipe-memory pressure: find the app "
              "leaking pipe fds, `lsof | awk '$5==\"PIPE\"{c[$1\" \"$2]++}END{for(k in c)print c[k],k}' | sort -rn | head`). "
              "Anything piping >512 B can stall; results below may be environmental.", flush=True)
    for n in sb.accel_notes:
        print(f"  note: {n}")
    for n in sb.revert_fail:
        print(f"  revert PARTIAL: {n}")
    loader = Loader(args.load) if args.load else None
    mon = chaos = None
    t_start = time.time()
    try:
        sb.repo = make_repo(sb)
        sb.plan_path = write_plan(sb)
        if not args.no_seams:
            sb.seams = SeamRunner(sb)
        daemon_cmd = [sys.executable, str(sb.bin / "ollama-queue.py"), "run", "--poll-interval", "1"]
        daemon = sb.daemon = sb.popen(daemon_cmd, "daemon.log")
        time.sleep(2)
        if daemon.poll() is not None:
            R.add("queue daemon starts in the sandbox", False,
                  (root / "daemon.log").read_text()[-800:])
            return R, sb
        slicer = [sys.executable, str(sb.bin / "ollama-dispatch-slice"), str(sb.plan_path), "--execute"]
        mon = Monitor(sb)
        mon.start()
        # TWO DRIVERS on one bundle, started together
        sb.drivers = [sb.popen(slicer, "driver-a.log"), sb.popen(slicer, "driver-b.log")]
        chaos = Chaos(sb, slicer)
        sb.injector = None
        if sb.fault is not None:
            sb.injector = FaultInjector(sb, slicer, daemon_cmd, t_start)
            sb.injector.start()
        sb.window = GateWindowProbe(sb, slicer)
        if not args.no_chaos:
            chaos.start()
            sb.window.start()
        decoy_id = None
        stuck_since = None
        deadline = t_start + args.timeout
        while time.time() < deadline:
            st = sb.slice_state() or {}
            sl = st.get("slices") or {}
            q = sb.qstate() or {}
            if decoy_id is None and not args.no_decoy and any(
                    str(j.get("label", "")).startswith(sb.label) for j in q.get("jobs") or []):
                decoy_id = enqueue_decoy(sb) or "none"
            if sl and all(v.get("status") in ("done", "skipped") for v in sl.values()):
                integ = st.get("integration") or {}
                if str(integ.get("status")) in ("staged", "landed", "integrated") or args.no_integrate_wait:
                    sb.staged_seen = str(integ.get("status")) in ("staged", "landed", "integrated")
                    break
            if sb.daemon.poll() is not None and not (
                    sb.injector is not None and sb.injector.is_alive()):
                R.add("queue daemon stays up", False, (root / "daemon.log").read_text()[-800:])
                break
            # fail fast: every slice parked/terminal, nothing of ours live, for 60s
            live = [j for j in q.get("jobs") or [] if str(j.get("label", "")).find(sb.label) >= 0
                    and j.get("status") in ("pending", "running", "held", "scheduled")]
            if sl and not live and all(v.get("status") in ("done", "skipped", "escalated", "failed", "blocked")
                                       for v in sl.values()) and any(
                    v.get("status") in ("escalated", "failed", "blocked") for v in sl.values()):
                stuck_since = stuck_since or time.time()
                if time.time() - stuck_since > 60:
                    print("  note: every slice parked with nothing live for 60s -- stopping early", flush=True)
                    break
            else:
                stuck_since = None
            time.sleep(3)
        if chaos:
            chaos.stop.set()
        if sb.injector is not None:
            sb.injector.stop.set()
        sb.window.stop.set()
        # settle: let the decoy run, let in-flight gates/reviews finish
        wait_for(lambda: not [j for j in (sb.qstate() or {}).get("jobs") or []
                              if j.get("status") in ("pending", "running")], 240)
        land_and_probe(sb)
        # one janitor/watcher pass at the end (stale READY rows must be gone after it)
        sb.py("dispatch-escalation-watcher.py", "--once", timeout=300, log_name="final-watcher.log")
        mon.stop.set()
        elapsed = time.time() - t_start
        check_invariants(sb, R, mon, chaos, before, decoy_id, elapsed)
    except Exception:
        R.add("canary harness ran without crashing", False, traceback.format_exc()[-1500:])
    finally:
        if mon:
            mon.stop.set()
        if chaos:
            chaos.stop.set()
        if loader:
            loader.stop()
        sb.kill_all()
    return R, sb


def land_and_probe(sb: Sandbox):
    st = sb.slice_state() or {}
    integ = st.get("integration") or {}
    if str(integ.get("status")) == "staged" and getattr(sb.args, "hand_land", False):
        # The owner's other landing path: main absorbs the staged branch BY HAND (no
        # --land-integration). The janitor must still close the READY-TO-LAND row.
        br = integ.get("integrate_branch")
        repo = str(st.get("repo") or sb.repo)
        p = sb.run(["git", "-C", repo, "merge", "--no-edit", "--no-ff", str(br)],
                   timeout=120, log_name="land.log")
        anc = sb.run(["git", "-C", repo, "merge-base", "--is-ancestor", str(br), "HEAD"], timeout=60)
        sb.landed = p.returncode == 0 and anc.returncode == 0
        sb.land_note = f"hand-merge rc={p.returncode} " + (p.stdout + p.stderr)
    elif str(integ.get("status")) == "staged":
        p = sb.run([sys.executable, str(sb.bin / "ollama-dispatch-slice"), str(sb.plan_path),
                    "--land-integration"], timeout=600, log_name="land.log")
        st = sb.slice_state() or {}
        sb.landed = bool((st.get("integration") or {}).get("landed"))
        sb.land_note = f"rc={p.returncode} " + (p.stdout + p.stderr)
    if sb.args.no_probe:
        return
    # HARNESS PROBE: a Darkbloom review whose schema rungs are ALL exhausted, through
    # the queue + runner exactly like a regate (code-review-agent on studio-db)
    logs = sb.bin / "ollama-queue-logs"
    src = next(iter(sorted(logs.glob("*-review/task.json"))), None)
    if not src:
        sb.probe = {"ok": False, "why": "no review task.json to copy"}
        return
    d = sb.root / "hprobe"
    d.mkdir(exist_ok=True)
    tj = json.loads(src.read_text())
    tj["context"] = "CANARY-EXHAUST " + str(tj.get("context") or "")
    (d / "task.json").write_text(json.dumps(tj, indent=1))
    lab = f"gate-{sb.label}-hprobe"
    p = sb.py("ollama-queue.py", "enqueue", "--model", MODEL, "--host", "studio", "--num-ctx", "65536",
              "--cwd", d, "--task-file", d / "task.json", "--runner", sb.bin / "code-review-agent.py",
              "--label", lab, "--allow-no-verify", log_name="hprobe.log")
    m = re.search(r"enqueued ([0-9a-f]{12})", p.stdout or "")
    if not m:
        sb.probe = {"ok": False, "why": "enqueue failed: " + (p.stdout + p.stderr)[-300:]}
        return
    jid = m.group(1)
    def _term():
        j = next((x for x in _jobs(sb) if x.get("id") == jid), None)
        if j is not None and j.get("status") not in LIVE:
            return j.get("status")
        m2 = re.search(rf"^\[queue\] {jid} \(.*?\) (finished on \S+, exit (\d+)|FAILED)",
                       (sb.root / "daemon.log").read_text(errors="replace"), re.M)
        if m2:
            return "done" if m2.group(2) == "0" else "failed"
        return None
    stt = wait_for(_term, 180, 1.0)
    run = {}
    try:
        run = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        pass
    exhausted = len(sb.obs.find("db-exhaust"))
    stats = run.get("stats") or {}
    sb.probe = {"ok": bool(run) and stt in ("done", "pruned") and exhausted >= 2
                and int(stats.get("review_parse_fail") or 0) >= 1,
                "job": jid, "status": stt, "run_json": bool(run), "exhaust_calls": exhausted,
                "parse_fail": stats.get("review_parse_fail")}


def _jobs(sb):
    return (sb.qstate() or {}).get("jobs") or []


def check_invariants(sb: Sandbox, R: Result, mon: Monitor, chaos: Chaos, before, decoy_id, elapsed):
    st = sb.slice_state() or {}
    sl = st.get("slices") or {}
    logs = sb.bin / "ollama-queue-logs"
    obs = sb.obs
    print(f"\n--- invariants ({elapsed:.0f}s) ---", flush=True)
    stats = {k: v.get("status") for k, v in sl.items()}
    R.add("every slice reached done (a real verdict, landed)",
          stats and all(v == "done" for v in stats.values()), f"{stats}")
    # every slice's landed coding job has a gate verdict on disk
    nov = []
    for sid, s in sl.items():
        jid = s.get("job_id")
        g = logs / f"{jid}.gate.json"
        try:
            v = json.loads(g.read_text()).get("verdict")
        except (OSError, ValueError):
            v = None
        if not v or v in ("error", "unknown"):
            nov.append(f"{sid}:{jid}:{v}")
    R.add("every slice's final job has a gate verdict", not nov, f"{nov}")
    integ = st.get("integration") or {}
    ib = integ.get("integrate_branch")
    ref = ib if ib and git(sb.repo, "rev-parse", "--verify", "-q", ib).returncode == 0 else "main"
    diff = sorted(git(sb.repo, "diff", "--name-only", f"{sb.init_sha}..{ref}").stdout.split())
    R.add("integration staged with ONLY the product files",
          sb.staged_seen and diff == ["lib/calc.ts", "lib/fmt.ts"],
          f"integration={ {k: integ.get(k) for k in ('status', 'integrate_branch')} } diff={diff}")
    if True:
        calc = git(sb.repo, "show", f"{ref}:lib/calc.ts").stdout
        fmt = git(sb.repo, "show", f"{ref}:lib/fmt.ts").stdout
        R.add("integration carries all three properties (and no misspelling)",
              "if (x < lo) return lo;" in calc and "export function clampHigh(" in calc
              and "clampHihg" not in calc and "export function formatRange(" in fmt, "")
    jobs = _jobs(sb)
    live = [f"{j.get('id')}:{j.get('label')}:{j.get('status')}" for j in jobs if j.get("status") in LIVE]
    R.add("no live / stuck queue jobs at the end", not live, f"{live}")
    parked = [f"{j.get('id')}:{j.get('label')}" for j in jobs if j.get("status") == "needs_opus"
              or j.get("needs_opus")]
    R.add("nothing parked for a human (needs_opus)", not parked, f"{parked}")
    # slices never FAILED while a live successor existed (sampled live)
    fv = [v for v in mon.violations if "FAILED while live" in v]
    R.add("no slice was FAILED while a live successor job existed", not fv, f"{fv}")
    R.add("queue state never read torn", mon.torn < 2, f"torn={mon.torn}")
    bad_hist = {k: v for k, v in mon.history.items() if any(x in ("escalated", "failed", "blocked") for x in v)}
    R.add("no slice was ever escalated/failed/blocked on the way (status history)", not bad_hist,
          f"{bad_hist}")
    exp_rounds = {"s1-lower": 1, "s2-fmt": 2, "s3-upper": 1}
    got_rounds = {sid: len([j for j in jobs if re.fullmatch(
        rf"auto-author-{re.escape(sb.label)}-{sid}(-c\d+|-r\d+)?", str(j.get("label", "")))]) for sid in SLICES}
    # pruned rows: count from the daemon log instead (every launch is logged once)
    dlog = (sb.root / "daemon.log").read_text(errors="replace")
    got_rounds = {sid: len(set(re.findall(
        rf"launched ([0-9a-f]{{12}}) \(auto-author-{re.escape(sb.label)}-{sid}(?:-c\d+|-r\d+)?\)", dlog)))
        for sid in SLICES}
    R.add("authoring took exactly the scripted rounds (no spurious re-author from a racing tree)",
          got_rounds == exp_rounds, f"got={got_rounds} want={exp_rounds}")
    drv = [(sb.root / f"driver-{x}.log").read_text(errors="replace") for x in "ab"]
    abst = [("another --execute is already driving" in d) for d in drv]
    R.add("of two drivers started together, exactly one drove and the other abstained",
          sum(abst) == 1 and not any("Traceback" in d for d in drv),
          f"abstained={abst} tracebacks={[('Traceback' in d) for d in drv]}")
    # worktrees: no orphaned changes, none unattributable
    wt_root = sb.home / ".ollama-dispatch" / "worktrees"
    orphans, unattrib = [], []
    known = {os.path.realpath(str(s.get("worktree"))) for s in sl.values() if s.get("worktree")}
    if st.get("chain_worktree"):
        known.add(os.path.realpath(st["chain_worktree"]))
    for d in sorted(wt_root.glob("*")) if wt_root.is_dir() else []:
        rp = os.path.realpath(str(d))
        if rp not in known:
            unattrib.append(d.name)
            continue
        dirty = git(d, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty and rp != os.path.realpath(str(st.get("chain_worktree") or "")):
            # a landed slice's worktree may hold its harness, never uncommitted PRODUCT edits
            prod = [l for l in dirty.splitlines() if l[3:].startswith("lib/")]
            if prod:
                orphans.append(f"{d.name}: {prod}")
    R.add("no unattributable worktree", not unattrib, f"{unattrib}")
    R.add("no orphaned (uncommitted product) worktree changes", not orphans, f"{orphans}")
    # bundle record (auto-runs) consistency
    ar = sb.home / ".ollama-dispatch" / "auto-runs"
    bad_rec = []
    recs = list(ar.glob("*.json")) if ar.is_dir() else []
    for p in recs:
        if p.name.endswith(".attempts.json"):
            continue   # auto's per-run attempt ledger (a list), not a chain record
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            bad_rec.append(f"{p.name}: unparseable")
            continue
        runs = rec.get("runs")
        if not isinstance(runs, dict):
            bad_rec.append(f"{p.name}: no per-run map")
            continue
        for lab, r in runs.items():
            if isinstance(r, dict) and r.get("phase") != "ended":
                bad_rec.append(f"{p.name}:{lab} phase={r.get('phase')}")
        if p.stem == sb.label:
            want = {f"{sb.label}-{s}" for s in SLICES}
            if set(runs) != want:
                bad_rec.append(f"bundle record runs={sorted(runs)} (a driver overwrote another's run)")
            for lab, r in runs.items():
                ajobs = sorted((j for j in jobs if str(j.get("label", "")).startswith(f"auto-author-{lab}")
                                and str(j.get("label")) .replace(f"auto-author-{lab}", "")[:2] in ("", "-c", "-r")),
                               key=lambda j: str(j.get("created_at") or j.get("enqueued_at") or ""))
                rounds = r.get("rounds") if isinstance(r, dict) else None
                rids = {str(x.get("job") if isinstance(x, dict) else x) for x in rounds} \
                    if isinstance(rounds, list) else set()
                miss = [j.get("id") for j in ajobs if j.get("id") not in rids and not any(
                    str(j.get("id")) in x for x in rids)]
                if miss:
                    bad_rec.append(f"{lab}: author job(s) {miss} missing from the record's rounds {sorted(rids)}")
    R.add("bundle/auto-run records consistent (parse; no run left mid-flight)",
          recs and not bad_rec, f"{bad_rec or 'no auto-run records'}")
    # handoff rows consistent
    ho = sb.run([sys.executable, str(sb.bin / "handoff-emit.py"), "--json"], timeout=120)
    try:
        rows = json.loads(ho.stdout or "[]")
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    except ValueError:
        rows = None
    ids = {j.get("id") for j in jobs}
    bad_h = []
    if rows is None:
        bad_h.append(f"handoff-emit --json unparseable rc={ho.returncode}: {(ho.stdout + ho.stderr)[-300:]}")
    else:
        for r in rows if isinstance(rows, list) else []:
            jid = r.get("id") or r.get("job_id")
            lab = str(r.get("label") or "")
            if r.get("status") in LIVE:
                bad_h.append(f"{jid} handoff row says live ({r.get('status')})")
            if lab.startswith(sb.label) and jid in ids:
                j = next(x for x in jobs if x.get("id") == jid)
                if j.get("status") in ("done",) and r.get("status") in ("failed",):
                    bad_h.append(f"{jid} done in queue, failed in handoff")
    R.add("handoff rows consistent with the queue", not bad_h, f"{bad_h}")
    # stale READY-TO-LAND rows (escalation index) after the final janitor pass
    stale = []
    esc = sb.home / ".ollama-dispatch" / "escalations"
    for f in (esc.glob("*.md") if esc.is_dir() else []):
        for line in f.read_text(errors="replace").splitlines():
            if line.startswith("- [ ]") and sb.label in line:
                stale.append(f"{f.name}: {line.strip()[:150]}")
    R.add("no open READY-TO-LAND / escalation rows after landing + janitor", not stale, f"{stale[:3]}")
    R.add("the staged chain landed on main in the sandbox repo",
          sb.landed, sb.land_note[-300:])
    # the seams, from what the stubs saw
    starts = obs.find("agent-start")
    auth = [r for r in starts if r.get("role") == "author"]
    R.add("plan verify_shape reached every author prompt",
          auth and all(r.get("verify_shape") for r in auth),
          f"{[(r.get('sid'), r.get('verify_shape')) for r in auth]}")
    cont = [r for r in starts if r.get("role") == "continue" and r.get("sid") == "s2-fmt"]
    R.add("non-converged s2 author round was continued (iteration cap -> c1)", bool(cont),
          f"roles seen for s2: {[r.get('role') for r in starts if r.get('sid') == 's2-fmt']}")
    R.add("the continuation carried the Jest-API hint", any(r.get("jest_hint") for r in cont), "")
    af = [r for r in starts if r.get("role") == "autofix" and r.get("sid") == "s3-upper"]
    R.add("s3 near-miss coding round -> gate auto-fix round", bool(af),
          f"roles seen for s3: {[r.get('role') for r in starts if r.get('sid') == 's3-upper']}")
    R.add("the auto-fix task named the near-miss spelling (clampHihg -> clampHigh)",
          any(r.get("near_miss") for r in af), "")
    # bloom_control hook (queue daemon) is exercised against the STUB pair and the stub BloomGauge:
    # it must hold successfully, never stall the pair-model jobs, and never touch a real BloomGauge.
    _bl = sb.home / ".ollama-dispatch" / "bloom-control.log"
    _blt = _bl.read_text(errors="replace") if _bl.is_file() else ""
    R.add("bloom_control hook held the stub pair (no 'HOLD FAILED', never stalled the queue)",
          "HOLD FAILED" not in _blt and ("HOLD: pair served" in _blt or "already held" in _blt),
          f"bloom-control.log tail: {_blt[-300:]!r}")
    alltxt = ""
    for p in sorted(list(sb.root.glob("*.log")) + list((sb.home / ".ollama-dispatch" / "slice-runs").glob("*.log"))):
        alltxt += p.read_text(errors="replace")
    pf_after = False
    for m in re.finditer(rf"queued [0-9a-f]+ \(auto-author-{re.escape(sb.label)}-s2-fmt-c1\)", alltxt):
        seg = alltxt[m.end():]
        cut = seg.find("# === authoring slice")
        seg = seg if cut < 0 else seg[:cut]
        if re.search(r"\[auto\] preflight round \d+", seg):
            pf_after = True
    # soak: a driver SIGKILLed while polling c1 is resumed by the slicer's own
    # "harness already authored -- resuming" path (clean_and_seal + preflight), which
    # prints the preflight table rather than an [auto] round line
    killed = bool(obs.find("fault-kill-driver"))
    # Either resumer may get there first: the injector's own resume
    # (driver-resume.log) or the chaos loop's periodic slicer --execute
    # (chaos-execute.log; soak seed 5 -- a correct resume the check missed).
    for rlog in (sb.root / "driver-resume.log", sb.root / "chaos-execute.log"):
        if pf_after or not killed or not rlog.exists():
            continue
        rt = rlog.read_text(errors="replace")
        i = rt.find("s2-fmt: harness already authored -- resuming")
        pf_after = i >= 0 and "\npre-flight: " in rt[i:]
        # soak seed 19: the resumer found the slice CONFIRMED (no "resuming" line) and
        # its preflight GO'd straight into the s2 coding enqueue -- equally correct.
        if not pf_after:
            pf_after = bool(re.search(
                r"\npre-flight: \S*wt-slice-%s-s2-fmt\n(?:(?!\npre-flight: ).)*?"
                r"\nenqueued [0-9a-f]+\s+%s-s2-fmt\s" % (re.escape(sb.label), re.escape(sb.label)),
                rt, re.S))
    R.add("preflight ran after the converged continuation", pf_after,
          "(soak: driver killed, slicer resume path checked)" if killed else "")
    if not sb.args.no_422:
        r422 = obs.find("db-422")
        relaxed = [r for r in obs.find("db-review") if r.get("rung") in ("relaxed", "none")]
        R.add("Darkbloom regate saw a schema-rung 422 and recovered on a lower rung",
              bool(r422) and bool(relaxed), f"422={len(r422)} lower={len(relaxed)}")
    if not sb.args.no_restart:
        rs = obs.find("restart")
        a401 = obs.find("auth-401")
        if sb.fault is not None:
            _k = sb.fault.db_restart["k"]
            R.add("Darkbloom restart (soak-timed) survived without stale-key 401s",
                  (bool(rs) or sb.stub.coding_seen < _k) and not a401,
                  f"restart={bool(rs)} k={_k} coding_seen={sb.stub.coding_seen} 401s={len(a401)}")
        else:
            R.add("Darkbloom restart (503, key rotation, base_url dropped) survived",
                  bool(rs) and not a401 and sl.get("s1-lower", {}).get("status") == "done",
                  f"restart={bool(rs)} stale-key 401s={len(a401)}")
    if sb.fault is not None:
        rlog = sb.root / "driver-resume.log"
        rtxt = rlog.read_text(errors="replace") if rlog.exists() else ""
        R.add("[soak] a resumed driver ran without a traceback", "Traceback" not in rtxt,
              rtxt[-400:])
        R.add("[soak] no fault-injector error", not obs.find("fault-error"),
              str(obs.find("fault-error"))[:300])
    if sb.probe:
        R.add("review agent survives exhausted Darkbloom schema rungs (report written, inconclusive verdict)",
              sb.probe.get("ok"), str(sb.probe)[:400])
    # decoy / bundle no-bounce
    if not sb.args.no_decoy:
        dv = [v for v in mon.violations if "decoy" in v]
        dj = [j for j in jobs if "decoy" in str(j.get("label"))]
        R.add("foreign bundle's job did not take the lane while the canary bundle worked",
              not dv, f"{dv}")
        ran = bool(mon.decoy_started_at) or any(j.get("status") in ("done", "failed") for j in dj) or \
            any(p.name.startswith(str(decoy_id)) for p in logs.glob("*.done.json")) if decoy_id else False
        R.add("foreign bundle's job ran after the canary bundle finished", ran,
              f"decoy={decoy_id} rows={[(j.get('id'), j.get('status')) for j in dj]}")
    # nothing escaped the sandbox
    after = real_snapshot()
    leaked = sorted(x for x in (after["labels"] - before["labels"]) if LABEL_PREFIX in x)
    leaked += sorted(x for x in (after["slice_runs"] - before["slice_runs"]) if LABEL_PREFIX in x)
    leaked += sorted(x for x in (after["auto_runs"] - before["auto_runs"]) if LABEL_PREFIX in x)
    leaked += sorted(x for x in (after["cpu_bundles"] - before["cpu_bundles"]) if LABEL_PREFIX in x)
    R.add("no enqueue / run state escaped the sandbox", not leaked, f"{leaked}")
    # CPU lane (Phase 6): the sandbox's pipeline stages went through cpu_stage, talked ONLY to the stub
    # (never the real API/runner), and fell back to the local path ("no CPU runner online").
    try:
        cl = [json.loads(l) for l in (sb.home / ".ollama-dispatch" / "cpu-lane.log").read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        cl = []
    on_runner = [c for c in cl if c.get("path") == "runner"]
    R.add("CPU lane: sandbox stages used the lane wrapper, ran local (stub: no runner online), never remote",
          bool(cl) and not on_runner and all(c.get("path") == "local" for c in cl),
          f"entries={len(cl)} remote={len(on_runner)} reasons={sorted({str(c.get('reason'))[:60] for c in cl})}")
    shim = (sb.root / "shim.log").read_text() if (sb.root / "shim.log").exists() else ""
    # SEAM REPLAYS. Some seams are too narrow (a microsecond lock window) or need a
    # state the e2e run never reaches (an idle lane BETWEEN a committed bundle's
    # steps while a foreign row waits). Their deterministic replays run here against
    # the SANDBOX copies, so a --revert of the fix is still caught by this command.
    # (started in the background at the beginning of the run: SeamRunner)
    seams = getattr(sb, "seams", None)
    if seams is not None:
        res = seams.join()
        for s in SEAMS:
            ok, note, secs = res.get(s["id"], (False, "not run", 0))
            R.add(f"[seam {s['id']}] {s['name']}", ok, note)
    w = getattr(sb, "window", None)
    if w is not None and not sb.args.no_chaos:
        R.add("gate-window probe polled the slicer inside both gate windows (reaped; review merged, auto-fix not yet requeued)",
              w.in_window >= 1 and w.merge_probe == "frozen",
              f"fired={w.fired} in_window={w.in_window} merge={w.merge_probe}")
    print(f"  info: window-probe fired={getattr(w, 'fired', None)} merge={getattr(w, 'merge_probe', None)}")
    print(f"  info: chaos={chaos.counts if chaos else None} samples={mon.samples} "
          f"shim-calls={len(shim.splitlines())} elapsed={elapsed:.0f}s")


# ---------------------------------------------------------------------------
# SEAM REPLAYS + REVERT PROOFS
# ---------------------------------------------------------------------------
# Each seam is a deterministic behavioural test run against the SANDBOX copy of
# ~/bin, so a --revert/--swap/--bin of the fix is caught by the canary itself. Each
# also names the historical fix(es) it guards: `proofs` is a list of revert sets
# (each a list of --revert specs FILE=OLD:NEW[@hunks]; OLD/NEW are backup
# basenames in the source bin, NEW "" = the current file). `--prove` un-applies
# every set in a scratch copy and requires the replay to go RED there, and green
# on the unmodified copy. The result is ~/.ollama-dispatch/canary/coverage.json,
# which --status reports ("N seam checks, M historical fixes proven").
# `proof_fail` (optional): a substring (or a list: any one) the RED output must contain, so a revert
# proves the RIGHT assertion went red (not an import error).
SEAMS = (
    dict(id='autoland', name='gate auto-land: a coding PASS lands on the origin repo ONLY when the verdict is a clean PASS, the second opinion agrees, the diff is code-only, the repo HEAD is still the chain baseline and the repo validate passes before push; any miss leaves it ready-to-apply untouched; AUTO_LAND=0 opts out; push failure rewinds',
         tool='test-auto-land.py', args=['--bin', '{bin}'], marker='AUTO_LAND_OK', timeout=300,
         proofs=[[{'mut': 'gate-on-complete.py', 'old': '    if not _gate_is_clean(payload):\n        why.append(f"gate verdict', 'new': '    if False:\n        why.append(f"gate verdict', 'why': 'a concerns / code-finding verdict is auto-landed'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    elif ag != "agree":\n', 'new': '    elif False:\n', 'why': 'a PASS with no (or unfinished) second opinion is auto-landed'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    if payload.get("second_opinion_disagreement") or ag == "disagree":\n', 'new': '    if False:\n', 'why': 'a PASS the cross-family reviewer disagrees with is auto-landed'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    if leaked or stage.get("scaffold_leaked"):\n', 'new': '    if False:\n', 'why': 'scaffold files in the deliverable diff are landed'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    extra = sorted(set(files) - set(targets or []))\n', 'new': '    extra = []\n', 'why': 'files the gate never judged are landed'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    if not mb or not tip or mb != tip or int(cls.get("behind") or 0) != 0:\n', 'new': '    if False:\n', 'why': 'a chain lands on a repo whose HEAD moved off its baseline'}],
                 [{'mut': 'gate-on-complete.py', 'old': '        if vr is None or vr.returncode != 0:\n', 'new': '        if False:\n', 'why': 'a failing validate command does not block the land'}],
                 [{'mut': 'gate-on-complete.py', 'old': '        if not _auto_land_enabled():\n', 'new': '        if False:\n', 'why': 'AUTO_LAND=0 opt-out is ignored'}],
                 [{'mut': 'gate-on-complete.py', 'old': '                _al_git(repo_root, "reset", "--keep", base_full)\n', 'new': '                pass\n', 'why': 'a rejected push leaves main advanced locally (partial land)'}],
                 [{'mut': 'gate-on-complete.py', 'old': '    return AUTO_LAND_VALIDATE_DEFAULTS.get(Path(repo_root).name)\n', 'new': '    return AUTO_LAND_VALIDATE_DEFAULTS.get(Path(repo_root).name) or "true"\n', 'why': 'a repo with no known validate command lands unvalidated'}]]),
    dict(id="ctxscaffold",
         name="queue ctx estimate: harness scaffold files (auto-harness-check.py etc.) named in AUTO-TASK.md are not counted as named files; real target files still are",
         tool="test-queue-ctx-scaffold-files.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-queue.py", "old": '        if rel.rsplit("/", 1)[-1] in CTX_SCAFFOLD_FILES:\n            continue\n', "new": '', "why": 'scaffold files inflate the named-file estimate and the ctx gate refuses the enqueue'}]]),
    dict(id="runnercls",
         name="wrapper-runner rows (gpu-exclusive / bakeoff driver) fail with their REAL cause (signal, vram/ssh step, driver exit 4, hung driver), never 'worker produced no iterations'; the VRAM check retries a transient ssh failure; a hung bake-off driver can be capped (v12 Strata h2h, 2026-10-09)",
         tool="test-runner-failure-classify.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-queue.py", "old": '            _rf = classify_runner_failure(\n', "new": '            _rf = None and classify_runner_failure(\n', "why": "runner rows get the worker 'no iterations' wording again"}],
                 [{"mut": "gpu-exclusive-runner.py", "old": '            if clock() >= deadline:\n                return False, None, f"vram_check failed', "new": '            if True:\n                return False, None, f"vram_check failed', "why": "one ssh blip fails the whole exclusive job"}],
                 [{"mut": "bakeoff-runner.py", "old": '                return max(0, int(s[len("MAX_S="):].strip()))', "new": '                return 0', "why": "a hung driver can never be capped"}]]),
    dict(id="prefetchfeed",
         name="CPU prefetch feeding: --prefetch / self-heal register work idempotently and never raise; finished entries are never resurrected; a stuck chain's resume stays immediate",
         tool="test-prefetch-register.py", marker="PREFETCH_REGISTER_OK",
         proofs=[
                 [{"mut": "cpu-prefetch.py", "old": '            if st in TERMINAL and not force:\n                return "skip:%s" % st', "new": '            if False:\n                return "skip:%s" % st', "why": 'a finished (landed) entry is resurrected by an automatic registrar'}],
                 [{"mut": "cpu-prefetch.py", "old": '            if st in ("queued", "launched"):\n                return "already:%s" % st', "new": '            if False:\n                return "already:%s" % st', "why": 'registration is not idempotent'}],
                 [{"mut": "cpu-prefetch.py", "old": '    except BaseException as ex:    # SystemExit from add_entry\'s validation included', "new": '    except ZeroDivisionError as ex:', "why": 'a registration error propagates into the calling flow'}],
                 [{"mut": "ollama-dispatch-auto", "old": '        sys.exit(prefetch_register(a, sys.argv[1:]))', "new": '        prefetch_register(a, sys.argv[1:])', "why": '--prefetch registers AND then runs the driver anyway'}],
                 [{"mut": "cpu-prefetch.py", "old": '    if not DRIVER_RE.match(cmd or ""):', "new": '    if not re.search("ollama-dispatch-auto", cmd or ""):', "why": 'any process mentioning the tool + label (the registering shell itself) blocks the launch as a live driver'}],
                 [{"mut": "dispatch-self-heal.py", "old": '        st = prefetch_register(run_label, "resume",', "new": '        st = "skip:lost"; _ = (run_label, "resume",', "why": 'a failed self-heal relaunch loses the converged harness (not deferred to the backlog)'}],
                 [{"mut": "dispatch-self-heal.py", "old": '    try:\n        pid = launch(cmd, cwd, log)\n    except Exception as e:', "new": '    pid = launch(cmd, cwd, log)\n    if False:\n        e = None', "why": 'a launch failure raises into the sweep'}]]),
    dict(id="cpuprefetch",
         name="CPU prefetch: queued work's CPU prep starts early, bounded by GPU runway + CPU headroom, never on held/parked/owned trees, never twice (GPU never waits on CPU)",
         tool="test-cpu-prefetch.py", marker="CPU_PREFETCH_OK",
         proofs=[
                 [{"mut": "cpu-prefetch.py", "old": '            if j.get("status") == "needs_opus":\n                return', "new": '            if False:\n                return', "why": 'a needs_opus bundle is started'}],
                 [{"mut": "cpu-prefetch.py", "old": '        if pv and pv.get("kind") not in BENIGN_PARKS:', "new": '        if False:', "why": 'a blocked/stalled parked bundle is started'}],
                 [{"mut": "cpu-prefetch.py", "old": '            return _cmd_is_driver(c, label)\n    return False', "new": '            return True\n    return False', "why": 'pid reuse: any live pid counts as a driver'}],
                 [{"mut": "cpu-prefetch.py", "old": '    return max(0, min(cfg["max_prep"] - n_advancing, cfg["target_ready"] - (n_ready + n_advancing)))', "new": '    return cfg["max_prep"]', "why": 'CPU prep unbounded by runway / in-flight drivers'}],
                 [{"mut": "cpu-prefetch.py", "old": '            queued = [e for e in entries if e.get("status") == "queued"]', "new": '            queued = list(entries)', "why": 'an already launched entry is launched again'}],
                 [{"mut": "cpu-prefetch.py", "old": '        if (h.get("tree_lock_held") or tree_lock_held)(wt):', "new": '        if False:', "why": 'a worktree another process holds the tree lock of is started'}],
                 [{"mut": "cpu-prefetch.py", "old": '        miss = [f for f in HARNESS_FILES if not (Path(wt) / f).exists()]', "new": '        miss = []', "why": 'a resume of an unauthored harness is started'}],
                 [{"mut": "cpu-prefetch.py", "old": '    if driver_pids(label, rows):\n        return True', "new": '    if False:\n        return True', "why": 'a second driver is launched beside a live one'}],
                 [{"mut": "cpu-prefetch.py", "old": '            return [("*", "skip", "another pass holds launch.lock")]', "new": '            pass', "why": 'concurrent passes both launch'}],
                 [{"mut": "cpu-prefetch.py", "old": '    if (h.get("slicer_owns") or slicer_owns)(bundle):', "new": '    if False:', "why": 'a slicer-owned bundle gets a rival driver'}],
                 [{"mut": "cpu-prefetch.py", "old": '    hit = (h.get("open_index_rows") or open_index_rows)(keys)\n    if hit:', "new": '    hit = None\n    if hit:', "why": 'a held READY-TO-LAND / HARNESS GO chain is started'}],
                 [{"mut": "cpu-prefetch.py", "old": '    if KILL_FILE.exists():', "new": '    if False:', "why": 'kill switch ignored'}],
                 [{"mut": "cpu-prefetch.py", "old": '    return (first, 0 if e.get("kind") == "resume" else 1, -int(e.get("priority") or 0),', "new": '    return (0, 0, 0,', "why": 'candidates not ordered by when the GPU needs them'}]]),
    dict(id="harnesslint",
         name="harness-lint: a failing spec (near-miss literal, literal the refimpl never writes, scope contradiction, stale prompt, repeat failure) is a named SPEC_DEFECT before any model run",
         tool="test-harness-lint.py", marker="HARNESS_LINT_OK",
         proofs=[[{"mut": "harness_lint.py", "old": "        nm = hg.literal_near_miss(lit, base)\n        if nm:",
                   "new": "        nm = None\n        if nm:", "why": "near-miss 'did you mean' disabled"}],
                 [{"mut": "harness_lint.py", "old": "    if hg.repeat_missing_literals(ma, mb):",
                   "new": "    if False:", "why": "repeat missing-literal set never parks"}],
                 [{"mut": "harness_lint.py", "old": "    if task_mtime is not None and tb is not None and task_mtime > tb:\n        return []",
                   "new": "    if False:\n        return []", "why": "an operator re-spec no longer lifts the retry block"}],
                 [{"mut": "harness_lint.py", "old": "        if \"VERIFY_OK\" in (v.stdout or \"\") and v.returncode == 0:\n            return []",
                   "new": "        if True:\n            return []", "why": "a refimpl that fails the fixture's own verify.sh passes lint (rt-walmart-cancel 0 !== 1)"}]]),
    dict(id='launchcap', name='slicer authoring cap is signature-blind and names the problem class: every launch counted across re-specs (lifetime cap parks), a changing-reason stall gets ONE fixture-class re-spec whose note names NON-DISCRIMINATING FIXTURE (aw-codec-floor s2, 2026-10-09)', tool='test-slice-launch-cap-respec.py', marker='LAUNCH_CAP_RESPEC_OK', timeout=240, proofs=[[{'mut': 'ollama-dispatch-slice', 'old': '    s["author_launches_total"] = int(s.get("author_launches_total") or 0) + 1\n', 'new': '    pass\n', 'why': 'every authoring launch is not counted (a changing-reason stall is invisible to the cap)'}], [{'mut': 'ollama-dispatch-slice', 'old': '    if int(s.get("author_launches_total") or 0) >= MAX_AUTHOR_LAUNCHES_TOTAL:\n        return False      # hard', 'new': '    if False:\n        return False      # hard', 'why': 'a re-spec can reopen a slice that burned the lifetime launch cap'}], [{'mut': 'ollama-dispatch-slice', 'old': 'if n >= RESPEC_CAP and not (cls == "fixture" and "fixture" not in classes):', 'new': 'if n >= RESPEC_CAP:', 'why': 'a changing-reason stall parks with no fixture-class re-spec (the aw-codec-floor s2 park)'}], [{'mut': 'ollama-dispatch-slice', 'old': '    if cls == "fixture":\n        note +=', 'new': '    if False:\n        note +=', 'why': 'the re-spec does not name the non-discriminating-fixture class'}], [{'mut': 'ollama-dispatch-slice', 'old': '            or _budget_why or _launches >= MAX_AUTHOR_LAUNCHES_TOTAL):', 'new': '            or _budget_why):', 'why': 'the lifetime launch cap never parks the slice'}]]),
    dict(id="serverfixhint",
         name="self-check failure hint: a server-side (non-browser) fixture gets the state-aliasing hint, never the installFakeBrowser/page-globals one (rt-walmart-cancel-import misdiagnosis)",
         tool="test-auto-module-not-found-hint.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-dispatch-auto", "old": "        if not _browserfx:\n", "new": "        if False:\n",
                   "why": "server fixtures get the browser-globals hint again"}]]),
    dict(id="literallint",
         name="literal lint gates: near-miss (insert/drop only, never a substitution), refimpl-absent, identical missing set twice -> SPEC_DEFECT, routed to die not auto-slice",
         tool="test-literal-lint.py", marker="LITERAL_LINT_OK",
         proofs=[[{"mut": "dispatch_harness_gates.py", "old": "    return best[1], best[2], best[3]", "new": "    return None", "why": "near-miss detection returns nothing (no did-you-mean)"}],
                 [{"mut": "dispatch_harness_gates.py", "old": "    return bool(prev) and bool(cur) and frozenset(map(tuple, prev)) == frozenset(map(tuple, cur))",
                   "new": "    return False", "why": "repeat never detected"}],
                 [{"mut": "ollama-dispatch-auto", "old": "BASELINE_GREEN_PREFIX, PLAN_SUSPECT_PREFIX,\n                                             SPEC_DEFECT_ROUTE_PREFIX))",
                   "new": "BASELINE_GREEN_PREFIX, PLAN_SUSPECT_PREFIX))", "why": "a SPEC_DEFECT why is auto-sliced again"}]]),
    dict(id="opedit",
         name="a hand-edit to a harness file made while the self-check / preflight refimpl step runs survives the revert",
         tool="test-harness-operator-edit-survives.py", marker="OPERATOR_EDIT_OK",
         proofs=[[{"mut": "ollama-dispatch-auto", "old": "    for f, data in _keep.items():\n        try:\n            (WT / f).write_bytes(data)",
                   "new": "    for f, data in {}.items():\n        try:\n            (WT / f).write_bytes(data)", "why": "operator edit overwritten by the check's revert"}],
                 [{"mut": "ollama-dispatch-preflight", "old": "            if rel in _keep:\n                continue\n            try:\n                p.parent.mkdir",
                   "new": "            if False:\n                continue\n            try:\n                p.parent.mkdir", "why": "preflight revert replays pre-check bytes over the operator edit"},
                  {"mut": "ollama-dispatch-preflight", "old": "        for rel, data in _keep.items():\n            try:\n                (self.wt / rel).write_bytes(data)",
                   "new": "        for rel, data in {}.items():\n            try:\n                (self.wt / rel).write_bytes(data)", "why": "kept edits not rewritten"}]]),
    dict(id="lockrace", name="advance lock: a driver paused mid-acquire never lets a second one in",
         tool="test-slice-advance-lock-race.py", args=["--slice", "{bin}/ollama-dispatch-slice"],
         marker="ALL PASS",
         proofs=[["ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T000503Z-canary:"
                  "ollama-dispatch-slice.bak-20261006T003933Z-canary-gatewait"]]),
    dict(id="nobounce", name="committed bundle: no foreign job takes an idle lane between its steps",
         tool="test-queue-bundle-no-bounce.py",
         args=["--queue", "{bin}/ollama-queue.py", "--plan", "{bin}/ollama-dispatch-plan",
               "--gate", "{bin}/gate-on-complete.py"], marker="NO_BOUNCE_TEST_OK",
         # 2026-10-09 contract (f57ce5e gate-settle yield): a committed bundle HOLDS the lane while
         # its gate hook is fresh (< SLICER_GAP_YIELD_S) and no timer ever drops it; past that it
         # parks cpu_wait (silent) and a non-pass verdict landing meanwhile must park it LOUDLY.
         proof_fail=["takes the idle lane", "yields the idle lane", "parks loudly"],
         # whole pre-fix queue (the test is built to take --queue <bak>); the fix's own
         # diff overlaps later edits (2 of 26 hunks reject -> NameError, wrong reason)
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261005T110942-nobounce"],
                 [{"mut": "ollama-queue.py", "old": "        if SLICER_GAP_YIELD_S > 0 and _gs and _gg >= SLICER_GAP_YIELD_S:\n            return \"waiting\", (f\"waiting for gate verdict",
                   "new": "        if True:\n            return \"waiting\", (f\"waiting for gate verdict",
                   "why": "gate-settle yields the lane immediately: a foreign job takes it between a committed bundle's steps"}],
                 [{"mut": "ollama-queue.py", "old": "        elif (_st == \"blocked\" and (parked.get(_pk) or {}).get(\"kind\") == \"cpu_wait\"",
                   "new": "        elif (False and (parked.get(_pk) or {}).get(\"kind\") == \"cpu_wait\"",
                   "why": "a cpu_wait-yielded bundle whose gate verdict is non-pass is never parked loudly"}]]),
    dict(id="gateprio", name="gates: committed bundle's gates first, others' only on idle non-conflicting lanes; studio-db coding+gate slots",
         tool="test-queue-gate-priority.py", args=["--queue", "{bin}/ollama-queue.py"],
         marker="GATE_PRIORITY_TEST_OK",
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261006-121237-gateprio"]]),
    dict(id="runnerpath", name="allowlisted --runner survives a symlinked HOME",
         tool="test-queue-runner-allowlist-symlink.py", args=["--queue", "{bin}/ollama-queue.py"],
         marker="ALL PASS",
         proofs=[["ollama-queue.py=ollama-queue.py.bak-20261006T011540Z-canary-runnerpath:"]]),
    dict(id="resumeharness", historical=True,
         name="--resume-harness re-enters preflight after a needs_opus park; self-heal relaunches it",
         tool="test-auto-resume-driver.py", marker="checks passed",
         # auto: the fix's 137-line hunk overlaps the later resumeguard edit, so the
         # proof disables its entry point instead (anchor follows the resumeauthor edit
         # that widened the entry to `resume_harness or resume_author`)
         proofs=[[{"mut": "ollama-dispatch-auto",
                   "old": 'if getattr(a, "resume_harness", False) or getattr(a, "resume_author", False):',
                   "new": 'if getattr(a, "resume_author", False):',
                   "why": "--resume-harness entry disabled"}],
                 ["dispatch-self-heal.py=dispatch-self-heal.py.bak-20261005T220611Z-needsopus-2:"]]),
    dict(id="chainbase", historical=True,
         name="auto-fix round gates diff/relevance/review against the chain's ROOT baseline",
         tool="test-gate-tree-race.py", marker="ALL PASS",
         proofs=[["gate-on-complete.py=gate-on-complete.py.bak-20261005T234707Z-chainbase:"],
                 ["verify-relevance.py=verify-relevance.py.bak-20261005T234707Z-chainbase:"
                  "verify-relevance.py.bak-20261006T021858Z-optoutguard"]]),
    dict(id="apikeyfile", historical=True,
         name="worker --api-key-file: key bound to one host, read at call time, never logged",
         tool="test-worker-api-key-file.py", marker="ALL OK",
         proofs=[["ollama-worker.py=ollama-worker.py.bak-20261005T233610Z-apikeyfile:"]]),
    dict(id="preflighttimeout",
         name="worker tool-calling preflight timeout: WORKER_PREFLIGHT_TIMEOUT_S honored (slow-prefill Strata), default 30, invalid/<=0 fall back",
         tool="test-worker-preflight-timeout.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-worker.py", "old": "        v = int(os.environ.get(\"WORKER_PREFLIGHT_TIMEOUT_S\", \"\") or 30)\n",
                   "new": "        v = 30\n", "why": "env var ignored"}],
                 [{"mut": "ollama-worker.py", "old": "    except ValueError:\n        return 30\n    return v if v > 0 else 30\n",
                   "new": "    except ValueError:\n        raise\n    return v\n", "why": "invalid/<=0 value not fallen back"}],
                 [{"mut": "ollama-worker.py", "old": "timeout = WARMUP_TIMEOUT_S if darkbloom else _preflight_timeout_default()",
                   "new": "timeout = WARMUP_TIMEOUT_S if darkbloom else 30", "why": "preflight request ignores the resolved timeout"}]]),
    dict(id="worker422",
         name="worker 422 'Inference generation failed' (Darkbloom MTP+tools) runs a bounded recovery ladder "
              "(tool_choice=none + nudge, then truncated last tool result) instead of pausing the cell",
         tool="test-worker-422-recovery.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-worker.py",
                   "old": "if api_style == \"openai\" and not manual_tools and _is_422_gen_failed(chat_err):",
                   "new": "if False and _is_422_gen_failed(chat_err):", "why": "ladder never runs"}],
                 [{"mut": "ollama-worker.py",
                   "old": "                payload[\"tool_choice\"] = tool_choice\n",
                   "new": "                pass\n", "why": "tool_choice never sent"}],
                 [{"mut": "ollama-worker.py",
                   "old": "        if rec[\"attempts\"] >= RECOVERY_422_MAX_ATTEMPTS:\n",
                   "new": "        if False:\n", "why": "per-run cap removed"}],
                 [{"mut": "ollama-worker.py",
                   "old": "        out[idx] = {**m, \"content\": new}\n",
                   "new": "        out[idx] = m\n", "why": "truncation step sends the full result"}]]),
    dict(id="janitorready", historical=True,
         name="janitor closes the HARNESS GO READY row (default index paths) once the coding job is enqueued",
         tool="test-janitor-harness-go-row.py", marker="ALL PASS",
         # hunks 1-2 = job_with_label + the HARNESS GO rule (hunk 3, the path parse,
         # overlaps a later edit); hunk numbers are stable between two fixed backups
         proofs=[["escalation_index_janitor.py=escalation_index_janitor.py.bak-20261005T220611Z-needsopus:"
                  "escalation_index_janitor.py.bak-20261005T222206Z-regate@1,2"]]),
    dict(id="regate", historical=True,
         name="--regate of a failed slice re-runs the gate and the slicer adopts the late verdict",
         tool="test-slice-late-regate-verdict.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-slice=ollama-dispatch-slice.bak-20261005T222036Z-regate:"
                  "ollama-dispatch-slice.bak-20261005T224125Z-treerace"]]),
    dict(id="regatefailed", historical=True,
         name="--regate accepts a FAILED slice (non-destructive way back for a fixed harness)",
         tool="test-regate-failed.py", marker="ALL PASS", proof_fail="FAILED slice is re-gated",
         # fix landed 2026-10-04 (bak 20261004T130106 -> T142632); its core hunk overlaps
         # the later PENDING+hand-lock clause, so the proof drops FAILED from REGATEABLE
         proofs=[[{"mut": "ollama-dispatch-slice", "old": "REGATEABLE = frozenset({ESCALATED, FAILED})",
                   "new": "REGATEABLE = frozenset({ESCALATED})", "why": "FAILED not regateable"}]]),
    dict(id="regatepending", historical=True,
         name="--regate accepts a HAND-LOCKED pending slice (the exit --execute advertises)",
         tool="test-regate-failed.py", marker="ALL PASS", share="regatefailed",
         proof_fail="hand-locked PENDING",
         proofs=[["ollama-dispatch-slice=ollama-dispatch-slice.bak-20261005T232005Z-regatepending:"
                  "ollama-dispatch-slice.bak-20261005T234000Z-autofixadopt"]]),
    dict(id="parentcleared", historical=True,
         name="a regate verdict whose parent row was cleared lands in the ARCHIVED record (no tail)",
         tool="test-gate-merge-archived-parent.py", marker="ALL PASS",
         proofs=[["gate-on-complete.py=gate-on-complete.py.bak-20261005T215918Z-regate:"
                  "gate-on-complete.py.bak-20261005T224125Z-treerace"]]),
    dict(id="needsopus", historical=True,
         name="needs_opus job: bounded local continuation, cleared on pass, stale park swept",
         tool="test-needs-opus-job-heal.py", marker="checks passed",
         args=["--queue", "{bin}/ollama-queue.py", "--heal", "{bin}/dispatch-self-heal.py",
               "--watcher", "{bin}/dispatch-escalation-watcher.py"],
         proofs=[[{"mut": "ollama-queue.py", "old": '        if _es == "cleared":\n            continue\n', "new": '        if False:\n            continue\n',
                   "why": "a needs_opus row whose continuation PASSED still parks its bundle"}],
                 [{"mut": "ollama-queue.py", "old": "    if _esc_pending:\n        j = _esc_pending[0]\n", "new": "    if False:\n        j = _esc_pending[0]\n",
                   "why": "a live job-ladder continuation no longer holds the bundle as working"}],
                 # heal_job did not exist pre-fix; its 450-line hunk overlaps the later
                 # resume_auto_driver edit, so the proof is the whole pre-fix file
                 ["swap:dispatch-self-heal.py=dispatch-self-heal.py.bak-20261005T214419Z-needsopus"]]),
    dict(id="harnessrefresh", historical=True,
         name="refresh_harness_check re-renders auto-harness-check.py from the template; TLA/mock hints",
         tool="test-auto-tla-mock-refresh.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T034924Z-tlamock:"]]),
    dict(id="optoutguard", historical=True,
         name="`relevance: unobservable` on a behavioural line is ignored by the gate and rejected by self-check",
         tool="test-relevance-behavioural-optout.py", marker="BEHAVIOURAL OPT-OUT TEST PASSED",
         timeout=600,
         proofs=[["verify-relevance.py=verify-relevance.py.bak-20261006T021858Z-optoutguard:"]]),
    dict(id="scopeline", historical=True,
         name="harness files on TASK.md's scope line fail the harness self-check",
         tool="test-relevance-behavioural-optout.py", marker="BEHAVIOURAL OPT-OUT TEST PASSED",
         timeout=600, proof_fail="scope accepted (want FAIL)", share="optoutguard",
         proofs=[[{"mut": "ollama-dispatch-auto", "old": "if _in_scope_harness:\n    fail(\"TASK.md's scope line lets", "new": "if False:\n    fail(\"TASK.md's scope line lets",
                   "why": "a harness file on the scope line passes the self-check"}]]),
    dict(id="revdedup", historical=True,
         name="escalation watcher dedups reviews by subject (one live review per chain)",
         tool="test-escalation-review-dedup.py", marker="REVIEW DEDUP TEST PASSED",
         env={"WATCHER_FILE": "{bin}/dispatch-escalation-watcher.py"},
         proofs=[["dispatch-escalation-watcher.py=dispatch-escalation-watcher.py.bak-20261006T0300Z-revdedup:"
                  "dispatch-escalation-watcher.py.bak-20261006T031621Z-litdrift"]]),
    dict(id="litdrift", historical=True,
         name="escalation context carries a machine-checked literals block; drifted answers are held",
         tool="test-escalation-literal-drift.py", marker="passed",
         env={"WATCHER_FILE": "{bin}/dispatch-escalation-watcher.py"},
         proofs=[["dispatch-escalation-watcher.py=dispatch-escalation-watcher.py.bak-20261006T031621Z-litdrift:"]]),
    dict(id="modnotfound", historical=True,
         name="unresolved-import hint outranks the fake-environment hint",
         tool="test-auto-module-not-found-hint.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T011214Z-modnotfound:"
                  "ollama-dispatch-auto.bak-20261006T011751Z-redundantunexercised"]]),
    dict(id="redundant", historical=True,
         name="refine names an UNEXERCISED line (not 'redundant: delete it') when its neighbours survive",
         tool="test-redundant-vs-unexercised.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T011751Z-redundantunexercised:"
                  "ollama-dispatch-auto.bak-20261006T012241Z-resumeguard"]]),
    dict(id="resumeguard", historical=True,
         name="--resume-harness clears a stale .refine-guard.json",
         tool="test-resume-stale-refine-guard.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T012241Z-resumeguard:"
                  "ollama-dispatch-auto.bak-20261006T021858Z-optoutguard@2"]]),
    dict(id="refinebatch",
         name="refine rounds carry <= 6 survivor lines, capped iterations, spec-untraced code marked deletable",
         tool="test-auto-refine-batching.py", marker="ALL PASS",
         # (the backup-diff proof went PARTIAL once later fixes touched the same hunks;
         #  the fix's two load-bearing lines are mutated directly instead)
         proofs=[[{"mut": "ollama-dispatch-auto",
                   "old": "    keep = set(sorted(order, key=lambda t: (str(t[0]), int(t[1] or 0)))[:k])",
                   "new": "    keep = set(order)", "why": "all survivors in one round"}],
                 [{"mut": "ollama-dispatch-auto",
                   "old": "    return min(REFINE_ITERS_CAP, max(author_max_iters, 6 + 4 * n))",
                   "new": "    return max(author_max_iters, 6 + 4 * n)", "why": "no iteration cap"}]]),
    dict(id="proseloop",
         name="worker stops a cut/tool/cut prose loop (density) and an auto job at its wall budget",
         tool="test-worker-prose-loop-budget.py", marker="ALL PASS",
         proofs=[["ollama-worker.py=ollama-worker.py.bak-20261006T042016Z-proseloop:"]]),
    dict(id="robustrequery",
         name="worker: an unparseable/truncated tool call is re-prompted FREE (templated error, max 3) then ends repeated_format_error; truncated writes are never run",
         tool="test-worker-robust.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-worker.py", "old": "                i -= 1\n                continue\n\n        if not tool_calls and not content and think_cap_truncated(",
                   "new": "                continue\n\n        if not tool_calls and not content and think_cap_truncated(",
                   "why": "requery charges an iteration"}],
                 [{"mut": "ollama-worker.py", "old": "                if _fe_action == \"stop\":", "new": "                if False:",
                   "why": "format-error loop unbounded"}],
                 [{"mut": "worker_robust.py", "old": "if c[\"name\"] in _READONLY_OK_TRUNCATED:", "new": "if True:",
                   "why": "truncated write_file executed"}]]),
    dict(id="robustloop",
         name="worker: loop detector warns once then ends loop_detected (recorded identical-search loop); stop-gate refuses task_complete without required files / must-contain",
         tool="test-worker-robust.py", marker="ALL PASS", share="robustrequery",
         proofs=[[{"mut": "ollama-worker.py", "old": "            _ld = _loop_det.end_iteration(i)", "new": "            _ld = None",
                   "why": "loop detector never consulted"}],
                 [{"mut": "ollama-worker.py", "old": "                if (converged and _STOP_GATE_MAX > 0 and (_sg_files or _sg_lits_raw)",
                   "new": "                if (False and (_sg_files or _sg_lits_raw)", "why": "stop-gate disabled"}]]),
    dict(id="robustrunaway",
         name="worker: endless reasoning stream is cut at the budget and the turn retried with thinking disabled",
         tool="test-worker-robust.py", marker="ALL PASS", share="robustrequery",
         proofs=[[{"mut": "ollama-worker.py", "old": "    _REASONING_BUDGET[0] = _reasoning_budget_chars", "new": "    _REASONING_BUDGET[0] = 0",
                   "why": "runaway budget not armed"}],
                 [{"mut": "ollama-worker.py", "old": "                _turn_think = False\n                messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message",
                   "new": "                messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message",
                   "why": "retry keeps thinking on"}]]),
    dict(id="proserunaway",
         name="worker: a coding turn that streams past the no-tool-call prose budget (or loops) is aborted mid-stream into the cut-off handling, raw turn persisted; a large legit write_file is not aborted",
         tool="test-worker-prose-runaway.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-worker.py", "old": "                    if _PROSE_BUDGET[0] and not tc_slots and _pc_len - _pc_checked >= PROSE_GUARD_STRIDE:",
                   "new": "                    if False:", "why": "stream guard never consulted"}],
                 [{"mut": "ollama-worker.py", "old": "                    if _PROSE_BUDGET[0] and not tc_slots and _pc_len", "new": "                    if _PROSE_BUDGET[0] and _pc_len",
                   "why": "tool call in progress is aborted"}],
                 [{"mut": "ollama-worker.py", "old": "            _co = persist_cutoff_turn(",
                   "new": "            _co = (lambda *a, **k: {'chars': 0, 'completion_tokens': 0, 'dup_line_ratio': 0, 'longest_repeated_line_block': 0, 'streamed': 0})(",
                   "why": "raw cut-off turn not persisted"}]]),
    dict(id="editguards",
         name="worker: edit_file/write_file reject a NEW syntax error (lint delta, pre-existing errors never block), failed edits get did-you-mean / already-applied hints, all tool params required (old calls tolerated), non-converged diff salvaged to a patch",
         tool="test-worker-edit-guards.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-worker.py", "old": "    rejected = _syntax_guard_reject(args[\"path\"], before, content)\n",
                   "new": "    rejected = None\n", "why": "syntax guard never rejects a write"}],
                 [{"mut": "worker_robust.py", "old": "        if before is not None and syntax_error_for(path, before, timeout):\n            return None\n",
                   "new": "", "why": "guard not a delta (broken files block edits)"}],
                 [{"mut": "ollama-worker.py", "old": "        if new_string and new_string.strip() and new_string in content:", "new": "        if False:",
                   "why": "no already-applied hint"}],
                 [{"mut": "ollama-worker.py", "old": "\"required\": [\"path\", \"offset\", \"length\"],", "new": "\"required\": [\"path\"],",
                   "why": "read_file params optional again"}],
                 [{"mut": "ollama-worker.py", "old": "            _salv = save_salvage_patch(cwd,", "new": "            _salv = (lambda *a, **k: {})(cwd,",
                   "why": "no salvage patch on non-converged exit"}]]),
    dict(id="loopdetectors",
         name="worker: loop detector stops same-action+same-error (nudge at 3, stop 4th), 3 no-tool turns, A,B,A,B alternation, endless grep/find/ls/read navigation -- each with a NAMED exit reason",
         tool="test-worker-loop-detectors.py", marker="ALL PASS",
         proofs=[[{"mut": "worker_robust.py", "old": "if self.err_sig and ss and self.err_streak >= ss:", "new": "if False:",
                   "why": "error streak never stops"}],
                 [{"mut": "worker_robust.py", "old": "if self.cfg.get(\"enabled\") and n and self.no_tool_streak >= n:", "new": "if False:",
                   "why": "monologue never fires"}],
                 [{"mut": "worker_robust.py", "old": "if ns and self._alternating(ns):", "new": "if False:",
                   "why": "alternation never stops"}],
                 [{"mut": "worker_robust.py", "old": "if nst and self.nav_streak >= self.nav_warn_at + nst:", "new": "if False:",
                   "why": "navigation streak never stops"}],
                 [{"mut": "ollama-worker.py", "old": "                _ld_reason = _wr.loop_exit_reason(_ld_kind)", "new": "                _ld_reason = _wr.REASON_LOOP",
                   "why": "kind not mapped to its named reason"}]]),
    dict(id="fanoutguard",
         name="worker: a turn over the per-turn tool-call cap is truncated + warned, a repeat ends tool_fanout_loop; consecutive past-EOF reads end read_past_eof_loop (all task kinds, not generic nav_loop)",
         tool="test-worker-fanout-guard.py", marker="ALL PASS",
         proofs=[[{"mut": "worker_robust.py", "old": "        if self.offences >= self.max_offences:", "new": "        if False:",
                   "why": "repeat fan-out offences never stop"}],
                 [{"mut": "worker_robust.py", "old": "            if self.eof_streak >= self.eof_stop:", "new": "            if False:",
                   "why": "past-EOF streak never stops"}],
                 [{"mut": "ollama-worker.py", "old": "            _fan_v = _fan_guard.on_turn(len(tool_calls))", "new": "            _fan_v = None",
                   "why": "worker ignores the fan-out guard"}],
                 [{"mut": "ollama-worker.py", "old": "                _eof_v = _fan_guard.on_call(name, _wr.is_past_eof_result(result))", "new": "                _eof_v = None",
                   "why": "worker never counts past-EOF reads"}],
                 [{"mut": "ollama-worker.py", "old": "        if _fan_abort and not paused_for_review:", "new": "        if False:",
                   "why": "fan-out abort never ends the run"}],
                 [{"mut": "ollama-dispatch-auto", "old": "    if tr in WORKER_EXIT_RUNAWAY:", "new": "    if False:",
                   "why": "runaway exits are blind-retried, not re-specced"}]]),
    dict(id="stalerestart",
         name="a driver whose code changed on disk re-execs itself via --resume-harness between rounds (capped, alerting)",
         tool="test-auto-stale-restart.py", marker="ALL PASS",
         proofs=[["ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T072538Z-stalestamp:"]]),
    dict(id="hygiene",
         name="superseded / launch-failed esc-reviews retired via cancel --automated / resolve (capped, alerting); D not reviewed once continued; prune spares live review sets",
         tool="test-self-heal-hygiene.py", marker="ALL PASS",
         proofs=[["swap:dispatch-self-heal.py=dispatch-self-heal.py.bak-20261006T081253Z-hygiene"],
                 ["swap:dispatch-escalation-watcher.py=dispatch-escalation-watcher.py.bak-20261006T081020Z-seam"]]),
    dict(id="pidreuse",
         name="driver liveness checks the command line, so a reused pid is not a live driver (self-heal + queue)",
         tool="test-driver-pid-reuse.py", marker="ALL PASS",
         proofs=[[{"mut": "dispatch-self-heal.py",
                   "old": '        return "ollama-dispatch-auto" in (r.stdout or "")',
                   "new": "        return True", "why": "self-heal liveness pid-only"}],
                 ["swap:ollama-queue.py=ollama-queue.py.bak-20261006T082957Z-pidreuse"]]),
    dict(id="treelock",
         name="queue launch waits on the worktree tree lock (no dirty launch baseline from a "
              "concurrent self-check/gate rewrite; soak seed 1)",
         tool="test-queue-launch-treelock.py", marker="ALL PASS",
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261006T091122Z-seam"],
                 [{"mut": "ollama-queue.py", "old": "                if _tbusy:\n",
                   "new": "                if False:\n", "why": "launch ignores the lock"}]]),
    dict(id="resumeauthor",
         name="a slice whose authoring was interrupted resumes AUTHORING (--resume-author), "
              "not preflight -> escalation (soak seed 2)",
         tool="test-resume-author.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-dispatch-slice",
                   "old": "            if harness_authored(wt) and _sc_fail is None:\n",
                   "new": "            if harness_authored(wt):\n", "why": "presence-only resume"}],
                 [{"mut": "ollama-dispatch-auto",
                   "old": '        if (not ok and getattr(a, "resume_author", False)\n',
                   "new": '        if (False and getattr(a, "resume_author", False)\n',
                   "why": "--resume-author ignored"}]]),
    dict(id="ttccont",
         name="progress continuation: one capped in-place resume round with the allowance's extra iterations; SHADOW by default (audited, not applied); never for a repeat/flapping signature, a spent cap, a retired bundle; a continuing job is not re-sealed",
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "cont"],
         marker="ALL PASS",
         proofs=[[{'mut': 'ollama-queue.py', 'old': '        if mode != "live":\n            job["continuation_shadow"]', 'new': '        if False:\n            job["continuation_shadow"]', 'why': 'shadow mode applies the continuation'}],
                 [{'mut': 'ollama-queue.py', 'old': '        if latest in [sorted(s) for s in (c.get("signatures") or [])]:\n', 'new': '        if False:\n', 'why': 'a repeating signature is granted again'}],
                 [{'mut': 'ollama-queue.py', 'old': '    if len(done) >= allowed:\n', 'new': '    if False:\n', 'why': 'no cap on continuation rounds'}],
                 [{'mut': 'ollama-queue.py', 'old': '        if bundle_is_retired(job_group_key(job)) or bundle_is_retired(job.get(BUNDLE_FIELD)):\n', 'new': '        if False:\n', 'why': 'a retired bundle is continued'}],
                 [{'mut': 'ollama-queue.py', 'old': '    if job.get("continuation_active") and job.get("continuation_extra_iterations"):\n', 'new': '    if False:\n', 'why': "continuation launches with the row's full max_iters"}],
                 [{'mut': 'ollama-queue.py', 'old': '    if job.get("continuation_active"):\n        return None          # an in-place progress continuation resumes', 'new': '    if False:\n        return None          # an in-place progress continuation resumes', 'why': "a continuing job's own dirt is sealed as a previous round"}],
                 [{'mut': 'ollama-queue.py', 'old': '    if job.get("continuations"):\n        # the queue already spent', 'new': '    if False:\n        # the queue already spent', 'why': 'a second allowance after the round is spent'}]]),
    dict(id="ttcnsample",
         name='N-sample + verify-gated selection: OFF by default, shadow only logs, live spawns fresh siblings (verifier-facts brief, T>=0.6, one at a time), first green wins, pass>smallest diff, R5 escalation from config, R6 respec, GPU cap',
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "nsample"],
         marker="ALL PASS",
         proofs=[[{'mut': 'ollama-queue.py', 'old': '    if not isinstance(c, dict) or not c.get("enabled"):\n        return None\n    try:\n        k = int', 'new': '    if not isinstance(c, dict):\n        return None\n    try:\n        k = int', 'why': 'samples without being enabled'}],
                 [{'mut': 'ollama-queue.py', 'old': '    if job.get("failure_class") not in NSAMPLE_CLASSES:\n', 'new': '    if False:\n', 'why': 'harness/spec failures get sampled (R0)'}],
                 [{'mut': 'ollama-queue.py', 'old': '    ok.sort(key=lambda c: (int(c.get("diff_lines")', 'new': '    ok.sort(key=lambda c: (0 * int(c.get("diff_lines")', 'why': 'selection ignores diff size'}],
                 [{'mut': 'ollama-queue.py', 'old': '    same_last2 = len(seq) >= 2 and', 'new': '    same_last2 = False and len(seq) >= 2 and', 'why': 'no R5 escalation'}],
                 [{'mut': 'ollama-queue.py', 'old': '        if o.get("status") in ("running", "launching", "paused"):\n            return f"sample', 'new': '        if False:\n            return f"sample', 'why': 'siblings run concurrently in one worktree'}],
                 [{'mut': 'ollama-queue.py', 'old': '    lines.append("Do not repeat the same kind of edit if it left the same checks failing.")\n', 'new': '    lines.append("Do not repeat the same kind of edit if it left the same checks failing.")\n    lines.append(str(facts.get("transcript") or facts.get("reasoning") or "SECRET-MODEL-REASONING-DO-NOT-LEAK"))\n', 'why': "the brief leaks the failed attempt's reasoning"}]]),
    dict(id="ttcreasons",
         name='fixed_lane_exhausted / apply_failed classify as context, transport_error as infra (retry-safe), ledger tags all three',
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "reasons"],
         marker="ALL PASS",
         proofs=[[{'mut': 'ollama-queue.py', 'old': '"fixed_lane_exhausted", "fixed_lane_apply_failed"})', 'new': '})', 'why': 'fixed-lane reasons not context'}],
                 [{'mut': 'ollama-queue.py', 'old': '    if tr in _FAILURE_INFRA_REASONS:\n        return "harness"', 'new': '    if False:\n        return "harness"', 'why': 'transport error not infra'}],
                 [{'mut': 'failure_ledger.py', 'old': '        tags.append("fixed-lane-transport-error")', 'new': '        pass', 'why': 'ledger misses transport error'}]]),
    dict(id="ttcbloom",
         name='bloom_control drain timeout adapts to a slow drain (doubles to 600s, never overrides an operator pin) and the switching hold outlives it',
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "bloom"],
         marker="ALL PASS",
         proofs=[[{'mut': 'ollama-queue.py', 'old': '        return int(min(BLOOM_DRAIN_MAX_S, max(cur * 2, base)))\n', 'new': '        return cur\n', 'why': 'drain never adapts'}],
                 [{'mut': 'ollama-queue.py', 'old': '    if env.get("BLOOMCTL_DRAIN_S"):\n        return None\n    cur = int(prev or base)', 'new': '    cur = int(prev or base)', 'why': 'operator drain pin overridden'}],
                 [{'mut': 'ollama-queue.py', 'old': 'hb_max = max(hb_max, float(st.get("drain_s") or 0) + BLOOM_DRAIN_HB_SLACK_S)', 'new': 'pass', 'why': 'hold expires mid-drain'}]]),
    dict(id="ttcretire",
         name="a retired bundle's tagged rows raise no Needs-attention alarm",
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "retire"],
         marker="ALL PASS",
         proofs=[[{'mut': 'ollama-queue-api.py', 'old': '        if _tag and _tag.strip() in _retired:\n            continue\n', 'new': '', 'why': 'tag-only retired rows alarm'}],
                 [{'mut': 'ollama-queue-api.py', 'old': '        gk = r.get("group_key") or None\n        if gk and gk in _retired:\n            continue\n', 'new': '        gk = r.get("group_key") or None\n', 'why': 'keyed retired rows alarm'}]]),
    dict(id="ttcprofile",
         name='thinking_policy + per-role escalation models live in model_profiles.yaml; the A/B arm cannot override gate/review roles',
         tool="test-queue-ttc.py", args=["--queue", "{bin}/ollama-queue.py", "--api", "{bin}/ollama-queue-api.py", "--ledger", "{bin}/failure_ledger.py", "--profile-src", "{bin}/model_profile.py", "--profiles-yaml", "{bin}/model_profiles.yaml", "--group", "profile"],
         marker="ALL PASS",
         proofs=[[{'mut': 'model_profile.py', 'old': '    if arm and str(role or "author") in (tp.get("ab_roles") or []):\n', 'new': '    if arm:\n', 'why': 'experiment arm overrides every role'}],
                 [{'mut': 'model_profile.py', 'old': '    return _ONOFF.get(v)\n', 'new': '    return None\n', 'why': 'arm table ignored'}],
                 [{'mut': 'model_profile.py', 'old': '    return [str(m) for m in (e.get("models") or []) if m]\n', 'new': '    return []\n', 'why': 'escalation models not read from config'}],
                 [{'mut': 'ollama-queue.py', 'old': '                    _env.update(job_env_overrides(job))\n', 'new': '', 'why': 'the A/B arm never reaches the worker'}]]),
    dict(id="lanesnow",
         name="dashboard per-lane RUNNING NOW: one equal row per lane (studio-db, unraid, CPU lane) with label/model/host/elapsed/bundle/live line, explicit 'unraid: idle', gpu-exclusive phase",
         tool="test-dashboard-lanes-now.py", args=["--src", "{bin}/ollama-queue-api.py"],
         marker="ALL PASSED",
         proofs=[[{'mut': 'ollama-queue-api.py', 'old': 'return NOW_LANES.map(([k, name]) => ({lane: k, name, idle: !by[k].length, jobs: by[k]}));', 'new': 'return NOW_LANES.map(([k, name]) => ({lane: k, name, idle: !by[k].length, jobs: by[k]})).filter(r => !r.idle);', 'why': 'an idle lane is silently omitted'}],
                 [{'mut': 'ollama-queue-api.py', 'old': "if (l.includes('unraid')) return 'unraid';", 'new': "if (false) return 'unraid';", 'why': 'unraid jobs land in the studio lane'}],
                 [{'mut': 'ollama-queue-api.py', 'old': "const NOW_LANES = [['studio-db', 'studio-db'], ['unraid', 'unraid'], ['cpu', 'CPU lane']];", 'new': "const NOW_LANES = [['studio-db', 'studio-db'], ['unraid', 'unraid']];", 'why': 'the CPU lane is not shown'}],
                 [{'mut': 'ollama-queue-api.py', 'old': "if (j.gpu_wait) return 'waiting for VRAM: ' + j.gpu_wait;", 'new': "if (false) return '';", 'why': 'gpu-exclusive phase is not shown'}],
                 [{'mut': 'ollama-queue-api.py', 'old': '      ${other.length ? `<div class="now-more">Off-GPU, live now: ${other.map(fmtAct).join(\' &middot; \')}</div>` : \'\'}\n      ${laneNowHtml(laneNowRows(jobs, acts, window.cpuLaneRunning))}`;', 'new': '      ${other.length ? `<div class="now-more">Off-GPU, live now: ${other.map(fmtAct).join(\' &middot; \')}</div>` : \'\'}`;', 'why': 'tile does not render the lane rows (idle branch)'}],
                 [{'mut': 'ollama-queue-api.py', 'old': "\n      ${laneNowHtml(laneNowRows(jobs, acts, window.cpuLaneRunning))}`;\n    now.style.cursor = 'pointer';", 'new': "`;\n    now.style.cursor = 'pointer';", 'why': 'tile does not render the lane rows (running branch)'}]]),
    dict(id="failclass",
         name="failure classes: stale-base only when main's commits touch the task's files; authoring TODO "
              "placeholders = model not spec; progressing authoring nonconvergence gets nonconvergence_progressing "
              "+ continuation_allowance; SIGTERM-paused/force-stopped rows are not failed attempts; a parked "
              "declared job stops holding the lane (smoke 2026-10-09 D4-D7)",
         tool="test-queue-failure-class.py", args=["--queue", "{bin}/ollama-queue.py"],
         marker="ALL PASS",
         proofs=[[{"mut": "ollama-queue.py", "old": "            if behind and touched:\n", "new": "            if behind:\n",
                   "why": "stale-base without scope evidence"}],
                 [{"mut": "ollama-queue.py", "old": "        if authoring and rx.pattern == _TODO_MARKER_RE.pattern:\n",
                   "new": "        if False:\n", "why": "authoring TODO placeholders = spec"}],
                 [{"mut": "ollama-queue.py", "old": '    spec_scan = scan if _vs >= 0 else _FAILURE_TOOL_LINE_RE.sub("", scan)\n',
                   "new": "    spec_scan = scan\n", "why": "earlier self-check echo classifies"}],
                 [{"mut": "ollama-queue.py",
                   "old": '    if cls != "model" or job.get("terminal_reason") != "nonconvergence" or not _is_authoring_job(job):\n',
                   "new": "    if True:\n", "why": "progress never classified"}],
                 [{"mut": "ollama-queue.py", "old": "            delta = 1 if _kind in ATTEMPT_BUDGET_KINDS else 0\n",
                   "new": "            delta = 1\n", "why": "interrupted row counts as failed attempt"}],
                 [{"mut": "ollama-queue.py",
                   "old": "            if any(j.get(\"id\") == d.get(\"job\") and j.get(\"status\") == ESCALATION_STATUS for j in jobs):\n",
                   "new": "            if False:\n", "why": "parked job keeps holding the lane"}]]),
    dict(id="nextstep",
         name="a terminal job DECLARES its bundle's next step; a pending continuation holds the "
              "lane (no hop to another bundle between a failed row and its -c1; smoke 2026-10-09)",
         tool="test-queue-next-step-declaration.py", args=["--queue", "{bin}/ollama-queue.py"],
         marker="ALL PASS",
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261009-nextstep"],
                 [{"mut": "ollama-queue.py", "old": '        if _nk == "hold" and not _chain_cpu_only:\n',
                   "new": '        if False:\n', "why": "continue declaration no longer holds the lane"}],
                 [{"mut": "ollama-queue.py", "old": "                    _declare_terminal_next_step(state, job)   # same state write as the status\n",
                   "new": "                    pass\n", "why": "terminal write declares nothing"}]]),
    dict(id="deadattempt",
         name="a relaunch after a dead attempt stashes that attempt's own edits instead of "
              "sealing them as the previous round (soak seed 3)",
         tool="test-queue-dead-attempt.py", marker="ALL PASS",
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261006T153419Z-deadattempt"],
                 [{"mut": "ollama-queue.py", "old": "                    _dead = stash_dead_attempt(job)\n",
                   "new": "                    _dead = None\n", "why": "not called at launch"}]]),
    dict(id="deadrecord",
         name="a SIGKILLed driver's chain record is closed by the hygiene sweep (no run left "
              "mid-flight; soak seed 1)",
         tool="test-self-heal-hygiene.py", marker="ALL PASS",
         proofs=[["swap:dispatch-self-heal.py=dispatch-self-heal.py.bak-20261006T101950Z-seam"]]),
    dict(id="orphanround",
         name="a round enqueued by a driver SIGKILLed before it recorded the job id is adopted "
              "into the chain record's rounds when hygiene closes the run (soak seed 47)",
         tool="test-self-heal-hygiene.py", marker="ALL PASS",
         proofs=[[{"mut": "dispatch-self-heal.py",
                   "old": "                        if orphans:   # enqueued, then the driver died before recording it\n",
                   "new": "                        if False:\n",
                   "why": "orphaned round not adopted"}]]),
    dict(id="autosealcreation",
         name="preflight --auto-seal seals a creation task's target STUB (re-stubbing a "
              "clobbered impl) so it is never absent/implemented at baseline (soak seed 4)",
         tool="test-preflight-autoseal-creation-target.py", marker="ALL PASS",
         proofs=[[{"mut": "ollama-dispatch-preflight",
                   "old": "        creation_rel = (self._prepare_creation_target()\n",
                   "new": "        creation_rel = (None and self._prepare_creation_target()\n",
                   "why": "creation target left out of the auto-seal"}]]),
    dict(id="nearmissdiff",
         name="the auto-fix near-miss spelling hint reads the job's RECORDED diff, not a live "
              "tree a concurrent self-check may hold at the refimpl (soak seed 26)",
         tool="test-gate-near-miss-recorded-diff.py", marker="ALL PASS",
         proofs=[[{"mut": "gate-on-complete.py",
                   "old": "                                    diff_path=Path(out_dir) / f\"{job_id}.diff\")\n",
                   "new": "                                    diff_path=None)\n",
                   "why": "requeue builder ignores the recorded diff"}]]),
    dict(id="pflock",
         name="preflight's both-ways proof (refimpl + relevance mutants) holds the worktree tree "
              "lock, so a racing self-check cannot restore a mutant into the target (soak seed 1)",
         tool="test-preflight-treelock.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-preflight=ollama-dispatch-preflight.bak-20261006T154711Z-seam"],
                 [{"mut": "ollama-dispatch-preflight",
                   "old": "        _lk = preflight_tree_lock(args.worktree)\n",
                   "new": "        _lk = None\n", "why": "lock not taken"}]]),
    dict(id="stagelock",
         name="the slicer holds the tree lock across land (verify -> stage -> commit) and confirm -> seal -> preflight; "
              "a racing self-check cannot fail a green slice or unseal the fixture (soak seeds 3, 6)",
         tool="test-slice-stage-treelock.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T155007Z-stagelock"],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "            if self.lp in _WT_LOCKS_HELD:",
                   "new": "            if True:", "why": "lock never really taken"}],
                 ["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T155758Z-confirmlock"]]),
    dict(id="creationreset",
         name="an authoring continuation (-cN or refine) resets an untracked creation target to "
              "its scaffold stub (not deleted) and launches clean (soak seed 4)",
         tool="test-queue-creation-reset.py", marker="ALL PASS",
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261006T155331Z-creationreset"],
                 [{"mut": "ollama-queue.py",
                   "old": "            stub = _creation_stub_for(cwd, path)\n",
                   "new": "            stub = None\n", "why": "creation target deleted again"}],
                 [{"mut": "ollama-queue.py",
                   "old": '    if job.get("continues") and re.match(r"^auto-author-.+-c\\d+$", lbl):\n',
                   "new": "    if False:\n", "why": "-cN not a continuation"}]]),
    dict(id="staleconfirm",
         name="a stale driver's confirm_and_enqueue re-reads disk + the queue under the tree lock and "
              "leaves a slice another driver already enqueued alone (soak seed 10)",
         tool="test-slice-stale-confirm.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T163210Z-staledriver"],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "        _why = confirm_abort_reason(st, sid, expect or _CONFIRM_ENTRY_STATUSES)\n",
                   "new": "        _why = None\n", "why": "disk never re-read"}]]),
    dict(id="inflightcont",
         name="the slicer's in-flight guard sees author continuation jobs (-cN / -esc), so a second "
              "--execute cannot re-seal + preflight a tree a pending c1 owns (soak seed 19)",
         tool="test-slice-inflight-continuation.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T165405Z-inflightcont"],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "    conts = [(l, cont_re.match(l)) for l in by_label]\n",
                   "new": "    conts = []\n", "why": "continuations invisible again"}]]),
    dict(id="planlint",
         name="plan lint root causes: a `Name: value` must_contain literal / intent-vs-verify body-shape "
              "conflict is refused at plan gen AND before any author job; --status shows ESCALATED loudly "
              "(rt-egift-link-s1-s4)",
         tool="test-plan-lint-rootcause.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-plan=ollama-dispatch-plan.bak-20261006T180000Z-planlint"],
                 ["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T181500Z-unsat"],
                 [{"mut": "plan_lint.py",
                   "old": "            if \"-\" in name or name.lower() in _PLAIN_HEADERS:\n",
                   "new": "            if False:\n", "why": "header pairs no longer flagged"}]]),
    dict(id="failureledger",
         name="the failure ledger sweep turns a synthetic terminal-failed job into ONE row with a "
              "signature (read-loop / write-thrash / literal-cap ...), idempotently, SAME_AS-linked, and "
              "the ONE attempt counter + repeat hint read it (rt-egift-link-s1-s4)",
         tool="test-failure-ledger.py", marker="ALL PASS",
         proofs=[[{"mut": "failure_ledger.py",
                   "old": "            if _is_terminal_failed(rec) and jid not in known:\n",
                   "new": "            if False:\n", "why": "sweep records nothing"}],
                 [{"mut": "failure_ledger.py",
                   "old": "        if any(r.get(\"job_id\") == job_id and not r.get(\"event\") for r in rows):\n",
                   "new": "        if False:\n", "why": "record_job not idempotent"}]]),
    dict(id="selfcheckhang",
         name="a hung verify/self-check is killed (whole process group) at a hard wall and reported as "
              "SELFCHECK_HANG with a clear cause; the scaffold verify has a 10s per-test timeout + a "
              "network-denying preload; the driver wall alerts + ledgers; preflight verify wall is 300s "
              "(rt-bfmr-tls-fingerprint -c2: 16 idle minutes)",
         tool="test-selfcheck-hang.py", marker="SUITE PASS", args=["--suite", "{bin}"], timeout=420,
         proofs=[["swap:ollama-dispatch-scaffold=ollama-dispatch-scaffold.bak-20261008T000000Z-heredoc"],
                 [{"mut": "ollama-dispatch-auto",
                   "old": "SELFCHECK_CMD_TIMEOUT_S = int(_os.environ.get(\"DISPATCH_SELFCHECK_CMD_TIMEOUT_S\") or 180)",
                   "new": "SELFCHECK_CMD_TIMEOUT_S = 100000", "why": "no per-command wall"}],
                 [{"mut": "ollama-dispatch-scaffold",
                   "old": "DISPATCH_TEST_TIMEOUT_MS:-10000", "new": "DISPATCH_TEST_TIMEOUT_MS:-120000",
                   "why": "per-test timeout back to 120s"}]]),
    dict(id="bloomctl",
         name="bloom_control: queue work -> BloomGauge Manual + pair-only Darkbloom (locked, idempotent, API-drift "
              "degrade); queue done -> Automatic at once (no timer); launches wait while switching; a parked or "
              "fit-held bundle releases the lanes immediately (rt-egift-link-s1)",
         tool="test-bloom-control.py",
         args=["--queue", "{bin}/ollama-queue.py", "--bc", "{bin}/bloom_control.py"],
         marker="ALL PASS", timeout=420,
         proofs=[[{"mut": "bloom_control.py",
                   "old": "    if last is not None and last[0] == sig and now - last[1] < hb:\n        return False\n",
                   "new": "    if False:\n        return False\n", "why": "drift-guard WARN logged on every verify"}],
                 [{"mut": "bloom_control.py", "old": 'rec.get("base_url") or _env("DARKBLOOM_BASE_URL") or ""',
                   "new": 'rec.get("base_url") or ""', "why": "no DARKBLOOM_BASE_URL fallback"}],
                 [{"mut": "bloom_control.py", "old": 'ok, w = bloom_set(cfg, "set-automatic", enabled=True)',
                   "new": 'ok, w = bloom_set(cfg, "set-automatic", enabled=False)', "why": "release leaves Manual"}],
                 [{"mut": "bloom_control.py", "old": "fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n                return self",
                   "new": "return self", "why": "no lock: concurrent callers double-switch"}],
                 [{"mut": "bloom_control.py",
                   "old": 'raise BloomShape("control view lacks controlVersion/automatic.mode (BloomGauge API changed?)")',
                   "new": "pass", "why": "no API shape check"}],
                 [{"mut": "ollama-queue.py", "old": 'if st.get("phase") == "switching":\n            return "bloom-control',
                   "new": 'if st.get("phase") == "never":\n            return "bloom-control', "why": "launches do not wait while switching"}],
                 [{"mut": "ollama-queue.py", "old": "if active is None or active not in (parked or {}):\n        return active",
                   "new": "if True:\n        return active", "why": "parked bundle keeps the lanes"}],
                 [{"mut": "ollama-queue.py", "old": 'human_held = [j for j in rows if j.get("user_hold") or j.get("fit_hold")',
                   "new": 'human_held = [j for j in rows if j.get("user_hold")', "why": "fit-held row counts as live work"}],
                 [{"mut": "ollama-queue.py", "old": "    settling = gate_hooks_settling(now, hooks)\n    if settling:",
                   "new": "    settling = gate_hooks_settling(now, hooks)\n    if False and settling:", "why": "gate hook in flight ignored"}],
                 [{"mut": "ollama-queue.py", "old": '            if dec not in ("wait", "blocked"):\n                out.append((jid, "pending"))',
                   "new": '            if True:\n                out.append((jid, "pending"))', "why": "row behind a held dep counts as work"}]]),
    dict(id="cpulane",
         name="CPU lane (queue side): a bundle waiting only on a CPU stage (remote Unraid job, local marker, "
              "running gate hook) holds no GPU lane/commitment -- parked cpu_wait without an alert, the next "
              "bundle launches, the waiting bundle resumes first; outstanding jobs come from the durable store",
         tool="test-cpu-lane-queue.py", args=["--queue", "{bin}/ollama-queue.py", "--lane", "{bin}/cpu_lane.py"],
         marker="CPU_LANE_QUEUE_TEST_OK", timeout=240,
         proofs=[[{"mut": "ollama-queue.py", "old": '    if cpu_wait:\n        return "waiting", (f"waiting on',
                   "new": '    if False and cpu_wait:\n        return "waiting", (f"waiting on',
                   "why": "a cpu stage keeps the bundle 'working' (holds the lanes)"}],
                 [{"mut": "ollama-queue.py", "old": '        elif st == "waiting":\n            # CPU LANE: nothing',
                   "new": '        elif st == "never-waiting":\n            # CPU LANE: nothing',
                   "why": "waiting bundle is parked as a failure (alert) instead of cpu_wait"}],
                 [{"mut": "ollama-queue.py", "old": "cpu_wait=cpu_by.get(k),", "new": "cpu_wait=None,",
                   "why": "the daemon ignores outstanding cpu jobs"}],
                 [{"mut": "cpu_lane.py", "old": "                    if now - (r[\"created\"] or now) > t + 1800:\n                        continue",
                   "new": "                    pass", "why": "an orphaned job wedges its bundle forever"}],
                 [{"mut": "ollama-queue.py", "old": '            out["cpu_only"] = chain_step_cpu_only(d.get("step"))',
                   "new": '            out["cpu_only"] = False',
                   "why": "preflight advancing still counts as a moving driver: the idle Studio lane is pinned for ~17 min"}],
                 [{"mut": "ollama-queue.py", "old": '    if _chain_cpu_only:\n        return "waiting"',
                   "new": '    if False:\n        return "waiting"',
                   "why": "a cpu-only chain stage reads 'working' again"}],
                 [{"mut": "ollama-queue.py", "old": '                if _cst == "working" and _cmv:\n                    _yw',
                   "new": '                if False:\n                    _yw',
                   "why": "a visitor bundle keeps the commitment after the parked bundle's next model job is ready (interleave tail)"}],
                 [{"mut": "ollama-queue.py", "old": 'CHAIN_CPU_ONLY_STEP_RE = re.compile(r"^(preflight|relevance|harness-check)',
                   "new": 'CHAIN_CPU_ONLY_STEP_RE = re.compile(r"^(preflight|relevance|harness-check|enqueue|author)',
                   "why": "an enqueue/author step (about to submit a model row) releases the lane"}]]),
    dict(id="escbundle",
         name="Escalation review bundling: a needs-opus-auto-X placeholder is born with its real bundle (explicit / "
              "chain record / label default), an esc-review joins the reviewed row's real bundle instead of minting "
              "job-<id>, the janitor re-tags unbundled rows (never a running one), and an esc-review row never makes "
              "its bundle the running/committed bundle (no stranded or held lanes)",
         tool="test-esc-review-bundle.py", args=["--bin", "{bin}"],
         marker="ESC_REVIEW_BUNDLE_TEST_OK", timeout=240,
         proofs=[[{"mut": "ollama-queue.py", "old": "        BUNDLE_FIELD: bundle or None,\n",
                   "new": "        BUNDLE_FIELD: None,\n", "why": "the needs_opus placeholder is born bundle-less again"}],
                 [{"mut": "ollama-queue.py", "old": "        jm = re.match(r\"^job-([0-9a-f]{12})$\", em.group(1))\n        if jm:",
                   "new": "        jm = None\n        if jm:", "why": "esc-review mints a job-<id> bundle of one"}],
                 [{"mut": "ollama-queue.py", "old": "            and not _is_esc_review_job(j)]\n    _by_id",
                   "new": "            ]\n    _by_id", "why": "a running esc-review makes its bundle the working/committed one"}],
                 [{"mut": "ollama-queue.py", "old": "    return [(i, o, n) for i, o, n, st in out if st != \"running\"]",
                   "new": "    return [(i, o, n) for i, o, n, st in out]", "why": "the janitor retags a RUNNING row"}],
                 [{"mut": "dispatch-escalation-watcher.py", "old": "                    b = _queue_label_bundle(j.get(\"label\") or e.get(\"label\"))",
                   "new": "                    b = None", "why": "watcher leaves a bundle-less row to the queue's slug"}]]),
    dict(id="cpustage",
         name="CPU lane (pipeline side): every whole-stage CPU run (baseline/final verify, preflight, relevance, "
              "harness-check) falls back to the local path when the runner is unreachable / errors / is ineligible, "
              "honours the kill switch (env + file) with no marker, discards a stale-snapshot remote result, "
              "passes exit/stdout through unchanged, and holds no GPU lane (marker / outstanding row => waiting)",
         tool="test-cpu-stage.py", args=["--bin", "{bin}"],
         marker="CPU_STAGE_TEST_OK", timeout=240,
         proofs=[[{"mut": "cpu_stage.py", "old": "            if snap1 != snap0:\n                why = \"STALE SNAPSHOT",
                   "new": "            if False:\n                why = \"STALE SNAPSHOT",
                   "why": "a result for a tree that changed mid-run is trusted"}],
                 [{"mut": "cpu_stage.py", "old": "    v = (os.environ.get(\"CPU_LANE\") or \"\").strip().lower()\n    if v in (",
                   "new": "    v = (os.environ.get(\"CPU_LANE\") or \"\").strip().lower()\n    if False and v in (",
                   "why": "CPU_LANE=0 is ignored"}],
                 [{"mut": "cpu_stage.py", "old": "        if DISABLE_FILE.exists():", "new": "        if False:",
                   "why": "the disable file is ignored"}],
                 [{"mut": "cpu_stage.py", "old": "        except RuntimeError as e:\n            why = str(e)\n",
                   "new": "        except RuntimeError as e:\n            raise\n",
                   "why": "a runner error aborts the stage instead of falling back to local"}],
                 [{"mut": "cpu_stage.py", "old": "        end = _marker(stage, bundle_id, timeout_s)\n    else:",
                   "new": "        end = (lambda c=None: None)\n    else:",
                   "why": "the opt-in local marker is not registered, so the bundle holds the GPU lanes"}],
                 [{"mut": "cpu_stage.py", "old": 'os.environ.get("CPU_LANE_LOCAL_MARKER") == "1"',
                   "new": 'True',
                   "why": "every local fallback frees the lane: a foreign bundle interleaves when the runner is down"}],
                 [{"mut": "cpu_stage.py", "old": "            ship = _shipped_host_paths() | extra\n",
                   "new": "            ship = _shipped_host_paths()\n",
                   "why": "a stage naming its own shipped $HOME/bin/vr-args.json is 'host-only' -> 17 min local mutation run"}],
                 [{"mut": "cpu_stage.py", "old": 'harness="bin/dispatch-harness.json" in xt)', "new": "harness=False)",
                   "why": "the git-excluded .dispatch-harness.json never reaches the runner -> relevance rc=3 'no tracked diff'"}],
                 [{"mut": "gate-on-complete.py", "old": 'if res.exit_code == 3 and "nothing to mutate" in (res.stderr or "")',
                   "new": 'if False and "nothing to mutate" in (res.stderr or "")',
                   "why": "a clean verify-relevance abstain is vetoed -> the mutation stage is silently re-run locally"}],
                 [{"mut": "cpu_stage.py", "old": '"remote result vetoed after %.0fs of runner work (re-running locally): %s"',
                   "new": '"remote result vetoed: %.0f %s"',
                   "why": "discarded runner work is invisible in the lane log"}]]),
    dict(id="cpuapi",
         name="CPU lane (API): bearer token on every /api/cpu route (constant-time), Cloudflare-fronted requests "
              "refused even with a valid token, atomic claim, lease expiry -> pending -> failed_infra after 3, "
              "cancel via heartbeat, durable across an API restart, read-only dashboard feed leaks nothing; "
              "runner enrollment: single-use short-lived code, LAN-only, rate limited, token rotation revokes",
         tool="test-cpu-lane-api.py", args=["--api", "{bin}/ollama-queue-api.py"],
         marker="CPU_LANE_API_TEST_OK", timeout=240,
         proofs=[[{"mut": "cpu_lane.py", "old": "    return hmac.compare_digest(header_value[7:].strip().encode(), tok.encode())",
                   "new": "    return True", "why": "any bearer value accepted"}],
                 [{"mut": "cpu_lane.py", "old": "    if any(k in keys for k in _PROXY_HEADERS):\n        return True",
                   "new": "    if False:\n        return True", "why": "Cloudflare-fronted requests reach the token routes"}],
                 [{"mut": "cpu_lane.py", "old": '                    elif r["attempt"] >= self.max_attempts:',
                   "new": '                    elif False:', "why": "a lease that keeps expiring is re-queued forever"}],
                 [{"mut": "cpu_lane.py", "old": '        c = sqlite3.connect(self.db, timeout=30, isolation_level=None)',
                   "new": '        c = sqlite3.connect(":memory:", timeout=30, isolation_level=None)',
                   "why": "state is not durable"}],
                 [{"mut": "ollama-queue-api.py", "old": 'return self._cpu_lane("PUT")',
                   "new": 'return self._text("not found", 404)', "why": "blob uploads are not routed"}],
                 [{"mut": "cpu_lane.py", "old": 'SELECT code_hash FROM enroll WHERE used IS NULL AND expires >= ?',
                   "new": 'SELECT code_hash FROM enroll WHERE expires >= ?',
                   "why": "an enrollment code can be redeemed more than once"},
                  {"mut": "cpu_lane.py", "old": '"UPDATE enroll SET used=? WHERE code_hash=? AND used IS NULL", (now, hit)).rowcount == 1',
                   "new": '"UPDATE enroll SET used=? WHERE code_hash=?", (now, hit)).rowcount >= 0',
                   "why": "(both consume guards removed)"}],
                 [{"mut": "cpu_lane.py", "old": '    if len(_enroll_fail) >= _ENROLL_MAX_FAILS:',
                   "new": '    if False:', "why": "enrollment codes can be brute-forced"}]]),
    dict(id="keepwarm",
         name="darkbloom-keepwarm: an evicted queue model is re-warmed (resident models touched first so the "
              "other queue model is never the LRU victim), never while a job runs, rate-limited, secrets scrubbed; "
              "the queue's launch waits while the model is re-warming",
         tool="test-darkbloom-keepwarm.py",
         args=["--script", "{bin}/darkbloom-keepwarm.py", "--queue", "{bin}/ollama-queue.py"],
         marker="ALL PASS", timeout=420,
         proofs=[["swap:ollama-queue.py=ollama-queue.py.bak-20261006T204756Z-keepwarm"],
                 [{"mut": "darkbloom-keepwarm.py", "old": "        if busy:\n            log(cfg, msg + ",
                   "new": "        if False:\n            log(cfg, msg + ", "why": "running job no longer blocks re-warm"}]]),
    dict(id="darkbloomapply",
         name="darkbloom-apply-wide-models.sh: refuses while a job runs, applies the wide filter, verifies both "
              "queue models resident, rolls back to the original filter on failure",
         tool="test-darkbloom-apply.py", args=[], marker="ALL PASS", timeout=300,
         # only red while the machine hands out 512-byte pipes (the heredoc deadlock); on a healthy
         # machine the old heredoc passes, so this proof is environment-gated (see pipe_capacity()).
         proofs=[["swap:darkbloom-apply-wide-models.sh=darkbloom-apply-wide-models.sh.bak-20261008T000000Z-heredoc"]]),
    dict(id="unsatharness",
         name="author flow: refimpl-first gate, plan-pinned must_contain cap, every failing case named + "
              "COMMON CAUSE hint, PLAN SUSPECT stops the continuation ladder (rt-egift-link-s1-s4)",
         tool="test-auto-unsat-harness.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T181500Z-unsat"],
                 [{"mut": "ollama-dispatch-auto",
                   "old": "if _REFIMPL_STUB_MARK in (WT / \"refimpl.py\").read_text(errors=\"replace\"):\n",
                   "new": "if False:\n", "why": "refimpl-first gate off"}]]),
    dict(id="harnessgates",
         name="harness-complete gate (Phase 2): TODO/SCAFFOLD_INCOMPLETE placeholders in TASK.md/refimpl/fixture, "
              "no real must_contain literal, and a dead refimpl anchor each make a harness incomplete; literal "
              "satisfiability + refine budget + contract status",
         tool="test-harness-gates.py", marker="ALL PASS",
         proofs=[[{"mut": 'dispatch_harness_gates.py', "old": '    findings += placeholder_findings(task, _read(wt / "refimpl.py"), fixtures)\n', "new": '    findings += []\n', "why": 'placeholders no longer block'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    findings += refimpl_anchor_findings(wt, target)\n', "new": '    findings += []\n', "why": 'dead refimpl anchor not caught'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    if task.strip() and not must_contain_literals(task):\n', "new": '    if False:\n', "why": 'no-real-literal not caught'}]]),
    dict(id="preflightharness",
         name="preflight (real CLI): raw scaffold = NO-GO harness-complete with the dynamic checks UNPROVEN; "
              "fail-before/pass-after is its own check; stub-aware baseline-clean; refimpl-written test sealed",
         tool="test-preflight-harness-gates.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'ollama-dispatch-preflight', "old": '        hc_ok = self.check_harness_complete()\n', "new": '        hc_ok = self.check_harness_complete() and None\n', "why": 'harness-complete no longer gates the dynamic checks'}],
                 [{"mut": 'ollama-dispatch-preflight', "old": '        return hg.is_stub_text(cur) and hg.is_stub_text(head)\n', "new": '        return False\n', "why": 'stub baseline counted as dirt again'}]]),
    dict(id="autocontinuego",
         name="auto: GO + proven contract + measured relevance auto-continues (confirm, re-check, enqueue with "
              "label+bundle) and logs the decision; otherwise pauses/refines; state-only baseline-clean repaired",
         tool="test-auto-continue-go.py", marker="ALL PASS", timeout=300,
         proofs=[[{"mut": 'ollama-dispatch-auto', "old": '    if cst != "PASS":\n        log_decision("go-continue", a, "pause", f"contract not PASS', "new": '    if False:\n        log_decision("go-continue", a, "pause", f"contract not PASS', "why": 'unproven contract still auto-enqueues'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    if d2.get("verdict") != "GO" or cst2 != "PASS":\n', "new": '    if False:\n', "why": 'post-confirm re-check ignored'}]]),
    dict(id="slicesat",
         name="slicer (Phase 2): 0-occurrence must_contain refused before authoring; already-satisfied invariant "
              "slices skipped; author cap re-specs once instead of parking",
         tool="test-slice-spec-satisfiability.py", marker="ALL PASS",
         proofs=[[{"mut": 'ollama-dispatch-slice', "old": '    bad = hg.literal_findings(lits, body, spec)\n', "new": '    bad = []\n', "why": 'unsatisfiable literal not caught'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    if s.get("kind") == "invariant":\n        # ALREADY-SATISFIED INVARIANT', "new": '    if False:\n        # ALREADY-SATISFIED INVARIANT', "why": 'invariant not skipped'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    if n >= RESPEC_CAP:\n        return False\n', "new": '    return False\n', "why": 'cap never re-specs'}]]),
    dict(id="gaterefine",
         name="gate-on-complete (Phase 2): relevance survivors with VERIFY_OK become a bounded harness refine "
              "(request + counter + logged decision), then a RE-SPEC escalation at the cap",
         tool="test-gate-harness-refine.py", marker="ALL PASS",
         proofs=[[{"mut": 'gate-on-complete.py', "old": '    if action != "refine":\n        return dict(dec,', "new": '    if True:\n        return dict(dec,', "why": 'refine never granted (always parks)'}],
                 [{"mut": 'gate-on-complete.py', "old": '    count = int(rec.get("count") or 0)\n', "new": '    count = 0\n', "why": 'refine budget unbounded'}]]),
    dict(id="phase2replay",
         name="Phase 2 replay corpus: the real parked cases (raw scaffold, stub baseline, MANUAL_SYNC_INIT, "
              "invariant, retry storm) are caught by the new gates",
         tool="test-phase2-replay-corpus.py", marker="ALL PASS",
         proofs=[[{"mut": 'dispatch_harness_gates.py', "old": '    if any(not _COMMENT_LINE_RE.match(ln) and _CASES_FLAG_OFF_RE.search(ln) for ln in lines):\n', "new": '    if False:\n', "why": 'flag-off scaffold guard not caught'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    return "; ".join(f"must_contain {lit!r}: {why}" for lit, why in bad)\n', "new": '    return None\n', "why": '0-occurrence literal passes'}]]),
    dict(id="specdefect",
         name="pre-dispatch contradiction gate (smoke-order-mismatch replay): a creation target that already exists "
              "at base / dead refimpl anchor / already-satisfied -> SPEC_DEFECT, no authoring job; the same failure "
              "3 rounds running is abandoned with the signature fed back",
         tool="test-spec-defect-gate.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'dispatch_harness_gates.py', "old": '    out += creation_target_findings(wt, target, creation, intent, ref)\n', "new": '    pass\n', "why": 'creation-target-exists check removed'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '        if intent_allows_existing(intent_text, p):\n            continue\n', "new": '        if False:\n            continue\n', "why": 'explicit modify wording no longer exempts'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '        if body is None or is_stub_text(body):\n            continue\n', "new": '        if body is None:\n            continue\n', "why": 'a seeded creation stub counted as a defect'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    if verify_green is not True:\n        return []\n', "new": '    if False:\n        return []\n', "why": 'already-satisfied without a green verify'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    if n >= cap:\n        return "abandon", n\n', "new": '    if False:\n        return "abandon", n\n', "why": 'identical failures never abandoned'}],
                 [{"mut": 'ollama-dispatch-preflight', "old": '        sd_ok = self.check_spec_defect()\n', "new": '        sd_ok = None\n', "why": 'preflight no longer runs the spec-defect check'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    _sd = spec_defect_precheck(a, wt, target)\n    if _sd:\n', "new": '    _sd = ""\n    if _sd:\n', "why": 'auto no longer refuses before authoring'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    if explicit:\n        sha = _rev(repo, explicit)\n', "new": '    if False:\n        sha = _rev(repo, explicit)\n', "why": 'explicit --baseline-ref ignored (re-bases/fetches)'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '        fh = open(lockdir / "ods-base-fetch.lock", "a+")\n', "new": '        fh = None; lockdir = lockdir\n', "why": 'fetch no longer locked'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    r = _git_run(wt, "merge-base", "--is-ancestor", base, "HEAD")\n    if r is not None and r.returncode == 0:\n        return []\n', "new": '    return []\n', "why": 'base drift never reported'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        cmd += ["--baseline-ref", a._base_sha]', "new": '        pass  #', "why": 'scaffold not given the resolved base'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        probe = hg.baseline_probe(wt)\n', "new": "        probe = {'green': False}\n", "why": 'baseline probe never green'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        _sdm = spec_defect_from_preflight(data) if verdict != "GO" else ""\n', "new": '        _sdm = ""\n', "why": 'spec-defect NO-GO falls into refine rounds'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        if _fact == "abandon":\n', "new": '        if False:\n', "why": 'auto never abandons on the identical failure'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        if _fact == "hint":\n', "new": '        if False:\n', "why": 'signature not fed back'}]]),
    dict(id="baselinered",
         name="baseline-red classification: an assertion failure is 'fails before'; a bare crash/import/syntax error is not "
              "(creation stubs and spec-named symbols may fail on the missing export); already-satisfied is structured",
         tool="test-preflight-spec-defect.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'ollama-dispatch-preflight', "old": '            if not cls["ok"]:\n', "new": '            if False:\n', "why": 'wrong-reason red counts as fails-before'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '    if bad:\n        return {"ok": False, "kind": "crash"', "new": '    if False:\n        return {"ok": False, "kind": "crash"', "why": 'crash never classified'}],
                 [{"mut": 'dispatch_harness_gates.py', "old": '                (good if creating or _ident_in(name, spec_text) else bad).append(\n                    f"missing symbol', "new": '                (good if True else bad).append(\n                    f"missing symbol', "why": 'spec-unnamed missing symbol accepted'}],
                 [{"mut": 'ollama-dispatch-preflight', "old": '                if fs:\n                    self.add("already-satisfied", FAIL,', "new": '                if False:\n                    self.add("already-satisfied", FAIL,', "why": 'already-satisfied not reported'}]]),
    dict(id="zombielock",
         name="a SIGKILLed-but-unreaped (zombie) driver does not hold the per-plan advance lock "
              "(slicer lock staleness + AUTO's driver-gone check) (soak seed 31)",
         tool="test-zombie-driver-lock.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T175344Z-zombielock"],
                 ["swap:ollama-dispatch-auto=ollama-dispatch-auto.bak-20261006T175410Z-zombielock"]]),
    dict(id="orphanenqueue",
         name="a coding job whose enqueuing driver died before recording it (slice still CONFIRMED) is "
              "adopted + landed on a gate PASS, never re-preflighted into an escalation (soak seed 36)",
         tool="test-slice-orphan-enqueue.py", marker="ALL PASS",
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261006T180637Z-orphanenqueue"],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "    if status not in (ESCALATED, ENQUEUED, CONFIRMED):\n",
                   "new": "    if status not in (ESCALATED, ENQUEUED):\n", "why": "CONFIRMED never adopted"}],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "        if s.get(\"status\") == CONFIRMED:\n            # Adoption is opportunistic",
                   "new": "        if False:\n            # Adoption is opportunistic",
                   "why": "non-adopted CONFIRMED slice escalated"}],
                 [{"mut": "ollama-dispatch-slice",
                   "old": "        awg = slice_job_awaiting_gate(st, sid)\n",
                   "new": "        awg = None\n", "why": "gate-pending job ignored"}]]),
    dict(id="modelprofile",
         name="model-profile conformance: the JSON body the worker (both paths, streaming + blocking) and "
              "darkbloom_chat actually send equals the model card's profile per model x role (sampling incl. "
              "top_k/presence/repetition, max_tokens>=32768, thinking control); queue forwards no hardcoded "
              "temperature; qwen3.8 alias is profile-declared and logged",
         tool="test-model-profile-conformance.py", marker="CONFORMANT", timeout=420,
         proofs=[[{"mut": "ollama-worker.py",
                   "old": "    _f = _profile_fields(model, role, api_style, temperature, num_ctx, top_p, top_k,\n                         max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n\n    if api_style == \"openai\":",
                   "new": "    _f = _profile_fields(model, role, api_style, 0.0, num_ctx, top_p, top_k,\n                         8192, 1.1, think,\n                         sampling_step=sampling_step, seed=seed)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n\n    if api_style == \"openai\":",
                   "why": "worker call_ollama sends the old hardcoded temp 0 / rep 1.1 / max_tokens 8192"}],
                 [{"mut": "ollama-worker.py",
                   "old": "    payload.update(_f)\n    if tools:\n        payload[\"tools\"] = TOOLS\n    url = f\"{host}/v1/chat/completions\"\n\n    # --- connect phase",
                   "new": "    payload.update({k: v for k, v in _f.items() if k not in (\"top_k\", \"chat_template_kwargs\")})\n    if tools:\n        payload[\"tools\"] = TOOLS\n    url = f\"{host}/v1/chat/completions\"\n\n    # --- connect phase",
                   "why": "streaming OpenAI path drops top_k and the thinking control again"}],
                 [{"mut": "ollama-worker.py",
                   "old": "**_profile_fields(model, \"review\", \"openai\", max_tokens=PREFLIGHT_MAX_TOKENS),",
                   "new": "",
                   "why": "preflight probe carries no profile (thinking left on for the cold-load request)"}],
                 [{"mut": "ollama-worker.py",
                   "old": "ap.add_argument(\"--temperature\", type=float, default=None,",
                   "new": "ap.add_argument(\"--temperature\", type=float, default=DEFAULT_TEMPERATURE,",
                   "why": "worker CLI temperature default hardcoded again"}],
                 [{'mut': 'model_profile.py', 'old': 'v[_k] = max(float(v[_k])', 'new': 'v[_k] = min(float(v[_k])', 'why': 'ladder step lowers temperature / repetition instead of raising it (exact step body)'}],
                 [{'mut': 'model_profile.py', 'old': '        if not need or need == have:', 'new': '        if True:', 'why': 'step 2 body carries thinking off without the hybrid arm'}],
                 [{'mut': 'ollama-worker.py', 'old': '        think=think, sampling_step=sampling_step, seed=seed)', 'new': '        think=think)', 'why': 'no sender puts the ladder step / seed on the wire'}],
                 [{'mut': 'ollama-worker.py', 'old': 'max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed)\n    payload = {"model": model, "messages": messages, "stream": True, "options"', 'new': 'max_tokens, repeat_penalty, think)\n    payload = {"model": model, "messages": messages, "stream": True, "options"', 'why': 'native streaming sender drops the ladder step / seed'}],
                 [{"mut": "darkbloom_chat.py",
                   "old": "    if think:\n        role = \"author\"\n",
                   "new": "    if think:\n        role = \"review\"\n",
                   "why": "darkbloom_chat think=True keeps non-thinking sampling"}],
                 [{"mut": "darkbloom_chat.py",
                   "old": "    max_tokens = body.get(\"max_tokens\")\n",
                   "new": "    body[\"temperature\"] = 0.0\n    max_tokens = body.get(\"max_tokens\")\n",
                   "why": "darkbloom_chat greedy temperature 0 re-added"}],
                 [{"mut": "model_profile.py",
                   "old": "    v = {k: ov.get(k, p[k]) for k in SAMPLING_KEYS}\n",
                   "new": "    v = {k: ov.get(k, p[k]) for k in SAMPLING_KEYS if k != \"top_k\"}\n",
                   "why": "request builder drops top_k"}],
                 [{"mut": "model_profile.py",
                   "old": "    _warn((\"alias\", model, a[\"to\"]),",
                   "new": "    (lambda *a_, **k_: None)((\"alias\", model, a[\"to\"]),",
                   "why": "alias applied silently (no log)"}],
                 [{"mut": "ollama-queue.py",
                   "old": "    if job.get(\"temperature_explicit\") and job.get(\"temperature\") is not None:\n",
                   "new": "    if job.get(\"temperature\") is not None:\n",
                   "why": "queue forwards legacy hardcoded temperature 0 rows"}],
                 [{"mut": "ollama-queue.py",
                   "old": "e.add_argument(\"--temperature\", type=float, default=None,",
                   "new": "e.add_argument(\"--temperature\", type=float, default=0,",
                   "why": "enqueue --temperature default 0 again"}],
                 [{"mut": "ollama-queue.py",
                   "old": "        return resolved\n    if \":\" in model:",
                   "new": "        return model\n    if \":\" in model:",
                   "why": "queue ignores the profile alias"}]]),
    dict(id='w3baseline', timeout=300,
         name='slicer forwards the chain tip as --baseline-ref to AUTO (deterministic base; scaffold records base_sha)',
         tool='test-slice-wave3.py', marker='all passed', proof_fail='baseline-ref',
         proofs=[[{"mut": 'ollama-dispatch-slice', "old": '            "--max-rounds", "4", *_baseline_args(cwt), *_lane_args_auto(st),', "new": '            "--max-rounds", "4", *_lane_args_auto(st),', "why": 'AUTO launch argv without --baseline-ref'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '            return ["--baseline-ref", sha]', "new": '            return []', "why": 'baseline args never resolved'}]]),
    dict(id='w3specdefect', timeout=300,
         name='slicer: SPEC_DEFECT escalations are a known kind, deterministic, re-specced once on first sight, then escalated; already-satisfied parks',
         tool='test-slice-wave3.py', marker='all passed',
         proofs=[[{"mut": 'ollama-dispatch-slice', "old": '    (r"SPEC_DEFECT: ", "spec defect', "new": '    (r"SPEC_DEFECT_NEVER: ", "spec defect', "why": 'SPEC_DEFECT kind dropped from _ESC_KINDS'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    re.compile(r"^ERROR: SPEC_DEFECT: "),', "new": '', "why": 'SPEC_DEFECT not deterministic'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    if is_spec_defect(sig) and respec_on_cap(s, sig, streak,', "new": '    if False and respec_on_cap(s, sig, streak,', "why": 'no re-spec on a spec defect'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    if is_spec_defect(sig):\n        # the one re-spec is spent', "new": '    if False:\n        # the one re-spec is spent', "why": 'spent re-spec not escalated'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '    if is_spec_defect(sig) and "already satisfied at base" in str(sig):', "new": '    if False:', "why": 'already-satisfied SPEC_DEFECT not parked'}]]),
    dict(id='w3lane', timeout=300,
         name="--lane: plumbed to AUTO's argv and to the coding enqueue as --mode only when the queue accepts it; open lane adds nothing",
         tool='test-slice-wave3.py', marker='all passed',
         proofs=[[{"mut": 'ollama-dispatch-slice', "old": '    return [] if lane == "open" else ["--lane", lane]', "new": '    return []', "why": '--lane never forwarded to AUTO'}],
                 [{"mut": 'ollama-dispatch-slice', "old": '            "--max-rounds", "4", *_baseline_args(cwt), *_lane_args_auto(st),', "new": '            "--max-rounds", "4", *_baseline_args(cwt),', "why": 'lane dropped from AUTO argv'}]]),
    dict(id='w3nonattempt', timeout=300,
         name='auto: a force_stopped/cancelled/operator job is not a failed attempt (record_attempt, park_visible, should_escalate)',
         tool='test-auto-staged-authoring.py', marker='all passed',
         proofs=[[{"mut": 'ollama-dispatch-auto', "old": '    if last_job_non_attempt(a, job):\n        return None     # operator stop', "new": '    if False:\n        return None     # operator stop', "why": 'record_attempt counts operator stops'}],
                 [{"mut": 'ollama-dispatch-auto', "old": 'NON_ATTEMPT_CLASSES = frozenset({"operator"})', "new": 'NON_ATTEMPT_CLASSES = frozenset()', "why": 'operator failure_class counted'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    if last_job_non_attempt(a, job):\n        # The last authoring job was stopped', "new": '    if False:\n        # The last authoring job was stopped', "why": 'park_visible parks operator stops'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    hist = counted_attempts(hist)\n', "new": '    hist = hist\n', "why": 'should_escalate counts interrupted'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '            a._last_job_outcome = {"job": job, "status": st,', "new": '            a._last_job_outcome_x = {"job": job, "status": st,', "why": 'poller never records the job outcome'}]]),
    dict(id='w3staged', timeout=300,
         name='auto: staged authoring -- TASK.md, fixture, refimpl one job each, stage checks, no-write re-prompt guard, operator stops not re-prompted, ladder fallback',
         tool='test-auto-staged-authoring.py', marker='all passed',
         proofs=[[{"mut": 'ollama-dispatch-auto', "old": 'STAGE_ORDER = ("task", "fixture", "refimpl")', "new": 'STAGE_ORDER = ("refimpl", "fixture", "task")', "why": 'stage order reversed'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '            if wrote_nothing(before, after) or runaway:', "new": '            if False:', "why": 'no-write guard disarmed'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '            if last_job_non_attempt(a):\n                return False, (f"{why} -- stopped by', "new": '            if False:\n                return False, (f"{why} -- stopped by', "why": 'operator stop re-prompted'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    return _author_with_continuations(a, wt, target, verify_cmd, resumed_why=why)\n\n\ndef stage_check_cli', "new": '    return False, why\n\n\ndef stage_check_cli', "why": 'no fallback to the ladder'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '    return str(getattr(a, "staged", "on") or "on").lower() != "off"', "new": '    return True', "why": '--staged off ignored'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        if "SCAFFOLD_INCOMPLETE" in txt or "SCAFFOLD: cases not yet authored" in txt:', "new": '        if False:', "why": 'scaffold guard accepted as an authored fixture'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '        head += _reprompt_text(stage, files[0], reprompt_n, runaway)', "new": '        pass', "why": 're-prompt carries no file-writing instruction'}]]),
    dict(id='w3taskclass', timeout=300,
         name='task-class router: class recorded in .dispatch-harness.json, per-class budgets, lane resolution (auto -> fixed only for MODIFY)',
         tool='test-task-class.py', marker='all passed',
         proofs=[[{"mut": 'task_class.py', "old": '    elif _PORT_RE.search(text):', "new": '    elif False:', "why": 'port class never detected'}],
                 [{"mut": 'task_class.py', "old": '        return "fixed" if cls == "modify" else "open"', "new": '        return "fixed"', "why": 'auto lane always fixed'}],
                 [{"mut": 'task_class.py', "old": '    if (lane or "open").lower() == "open":\n        return []\n    sup', "new": '    if False:\n        return []\n    sup', "why": 'open lane emits --mode'}]]),
    dict(id='w3classrecord', timeout=300,
         name='auto: the task class + lane are recorded in .dispatch-harness.json (per-class budgets read back by the staged flow)',
         tool='test-auto-staged-authoring.py', marker='all passed',
         proofs=[[{"mut": 'ollama-dispatch-auto', "old": '        m.record_task_class(wt, rec, lane=m.resolve_lane(lane, rec["class"]))', "new": '        pass', "why": 'class never recorded in the manifest'}]]),
    dict(id='w3revalidate', timeout=420,
         name='queue-pop re-check (gate E): a changed blob is detected as drift, an unchanged tree runs no verify, base-moved-and-green is NOP_GREEN, creation over a real implementation is SPEC_DEFECT',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'harness_audit_gates.py', 'old': '    if not drift:\n        return {"status": "fresh"', 'new': '    if True:\n        return {"status": "fresh"', 'why': 'drift never detected'}], [{'mut': 'harness_audit_gates.py', 'old': '    if rc == 0 or "VERIFY_OK" in out:\n        return {"status": "NOP_GREEN"', 'new': '    if False:\n        return {"status": "NOP_GREEN"', 'why': 'green untouched tree not flagged'}], [{'mut': 'harness_audit_gates.py', 'old': '    if findings:\n        return {"status": "SPEC_DEFECT"', 'new': '    if False:\n        return {"status": "SPEC_DEFECT"', 'why': 'creation-over-implementation not flagged'}]]),
    dict(id='w3determinism', timeout=420,
         name='verify determinism + flake lint (gate C): differing verdicts across identical runs and time/random/unordered sources are flagged, flake-ok exempts',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'harness_audit_gates.py', 'old': '    if len(set(sigs)) <= 1:\n        return []', 'new': '    if True:\n        return []', 'why': 'flaky verify accepted'}], [{'mut': 'harness_audit_gates.py', 'old': '        if "flake-ok" in ln or ln.strip().startswith(("#", "//", "*")):\n            continue', 'new': '        if True:\n            continue', 'why': 'flake lint never fires'}], [{'mut': 'harness_audit_gates.py', 'old': 'tuple(sorted(names)))', 'new': 'tuple())', 'why': 'failing-case names ignored (same rc, different failure)'}]]),
    dict(id='w3traceability', timeout=420,
         name='static spec traceability (gate A, WARN): invented imports/strings/numbers in the fixture that TASK.md and the base never mention are flagged, spec-ok exempts',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'harness_audit_gates.py', 'old': '        return tok in task or tok in base or tok.lower() in task.lower()', 'new': '        return True', 'why': 'strings always traced'}], [{'mut': 'harness_audit_gates.py', 'old': '    return re.search(r"(?<![\\w$])" + re.escape(tok) + r"(?![\\w$])", task) is not None or \\', 'new': '    return True or re.search(r"(?<![\\w$])" + re.escape(tok) + r"(?![\\w$])", task) is not None or \\', 'why': 'names always traced'}]]),
    dict(id='w3trivial', timeout=420,
         name='trivial-solution battery (gate D): touch-only/literals-only/return-None/no-op variants must be RED; a grep-only verify draws a finding; the target is always restored',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'harness_audit_gates.py', 'old': '            if rc == 0 or "VERIFY_OK" in (o or ""):\n                out.append({"code": "TRIVIAL_SOLUTION_PASSES"', 'new': '            if False:\n                out.append({"code": "TRIVIAL_SOLUTION_PASSES"', 'why': 'green trivial variant accepted'}], [{'mut': 'harness_audit_gates.py', 'old': '        else:\n            p.write_bytes(orig)\n    return out', 'new': '        else:\n            pass\n    return out', 'why': 'target not restored after the battery'}], [{'mut': 'ollama-dispatch-preflight', 'old': '                        warn("trivial-solution", gm.run_trivial_battery(', 'new': '                        (lambda *_: None)("trivial-solution", gm.run_trivial_battery(', 'why': 'preflight never reports the battery'}]]),
    dict(id='w3constants', timeout=420,
         name='prose vs fixture constants (gate B, refine trigger only): same unit, disjoint values -> CONSTANT_CONFLICT',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'harness_audit_gates.py', 'old': '        if not (spec[u] & fx[u]):', 'new': '        if False:', 'why': 'conflicting constants accepted'}]]),
    dict(id='w3auditwire', timeout=420,
         name='preflight wiring: audit rows are WARN-only, audit findings never block',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'ollama-dispatch-preflight', 'old': '            self.add(cid, WARN, f"{len(fs)} finding(s)', 'new': '            self.add(cid, FAIL, f"{len(fs)} finding(s)', 'why': 'audit findings block'}]]),
    dict(id='w3auditjournal', timeout=420,
         name='preflight: the trivial-solution battery is journaled so a SIGKILL mid-battery is recovered, not stranded in the target',
         tool='test-preflight-refimpl-journal.py', marker='ALL PASS',
         proofs=[[{'mut': 'ollama-dispatch-preflight', 'old': '                        self._journal_open()\n                        try:\n                            warn("trivial-solution"', 'new': '                        try:\n                            warn("trivial-solution"', 'why': 'trivial battery rewrites the target with no journal (SIGKILL strands a degenerate target)'}]]),
    dict(id='w3auditsafe', timeout=420,
         name='preflight: an unreadable fixture / audit bug never takes the gate down (same verdict as without the audit)',
         tool='test-harness-audit-gates.py', marker='ALL PASS',
         proofs=[[{'mut': 'ollama-dispatch-preflight', 'old': '        try:\n            fx = (self.wt / fx_name).read_text(errors="replace") if fx_name else ""\n        except OSError:                              # unreadable fixture: check_draft_confirmed owns that verdict\n            fx = ""\n', 'new': '        fx = (self.wt / fx_name).read_text(errors="replace") if fx_name else ""\n', 'why': 'unreadable fixture crashes the audit'}, {'mut': 'ollama-dispatch-preflight', 'old': '        try:\n            return self._check_spec_audit()\n        except Exception as e:\n            self._audit = {"error": f"{type(e).__name__}: {e}"[:200]}\n', 'new': '        return self._check_spec_audit()\n', 'why': 'audit exception takes the gate down'}]]),
    dict(id="fixedlane", timeout=240,
         name="fixed repair-and-validate lane (standalone): eligibility router, SEARCH/REPLACE + whole-file parse/apply "
              "(ambiguous/no-match refused), syntax/lint-delta guard, reflection <=2, fresh K-sample restart, "
              "verify-pass-then-smallest-diff selection, exhaustion handoff, NO tools in the request, thinking "
              "policy in the body, runaway stream cut",
         tool="test-fixed-lane.py", marker="ALL PASS",
         proofs=[[{"mut": 'fixed_lane.py', "old": '          "presence_penalty": cfg.get("presence_penalty"),\n', "new": '', "why": 'lane no longer overrides the verbatim-hostile presence_penalty'}],
                 [{"mut": 'fixed_lane.py', "old": '    for banned in ("tools", "tool_choice", "functions", "function_call"):\n        assert banned not in body, f"fixed lane must never send {banned}"\n', "new": '    body["tools"] = []\n', "why": 'request body carries tools'}],
                 [{"mut": 'fixed_lane.py', "old": '    if think == "low":\n        body["reasoning_effort"] = "low"\n', "new": '', "why": 'low thinking policy sends no reasoning_effort'}],
                 [{"mut": 'fixed_lane.py', "old": '        if len(hits) > 1:\n            return "ambiguous", None, how, 0, len(hits)\n\n    def uniform', "new": '        if len(hits) > 1:\n            return "ok", hits[0], how, 0, 1\n\n    def uniform', "why": 'ambiguous SEARCH silently edits the first match'}],
                 [{"mut": 'fixed_lane.py', "old": '        return (0, s["diff_lines"], s["hunks"], s["tokens"], s["index"])', "new": '        return (0, -s["diff_lines"], s["hunks"], s["tokens"], s["index"])', "why": 'selection prefers the BIGGEST diff'}],
                 [{"mut": 'fixed_lane.py', "old": '    ws.restore()\n    t0 = clock()\n', "new": '    t0 = clock()\n', "why": 'fresh sample does not restart from the baseline'}],
                 [{"mut": 'fixed_lane.py', "old": '        if r.ok:\n            ok[p] = after', "new": '        if True:\n            ok[p] = after', "why": 'edit guard verdict ignored'}],
                 [{"mut": 'edit_guard.py', "old": '    if s_after and not s_before:', "new": '    if False:', "why": 'syntax regression no longer rejected'}],
                 [{"mut": 'fixed_lane.py', "old": '            if think != "off":\n                think = "off"        # the thinking IS the problem', "new": '            if False:\n                think = "off"        # the thinking IS the problem', "why": 'cut thinking turn retried with thinking still on'}],
                 [{"mut": 'fixed_lane.py', "old": '        ws.restore()\n        if keep_state == "best" and best', "new": '        if keep_state == "best" and best', "why": 'failed lane leaves its attempt in the worktree'}],
                 [{"mut": 'fixed_lane.py', "old": '    nrounds = 1 + int(cfg["reflections"])', "new": '    nrounds = 1 + int(cfg["reflections"]) + 1', "why": 'reflection budget off by one'}],
                 [{"mut": 'fixed_lane.py', "old": '"exhausted", FIXED_LANE_EXHAUSTED', "new": '"exhausted", FIXED_LANE_APPLY_FAILED', "why": 'exhaustion reported as apply_failed'}],
                 [{"mut": 'fixed_lane.py', "old": '            if len(self.content) > self.cc:\n                return "content_cap"', "new": '            if False:\n                return "content_cap"', "why": 'stream-forever no longer cut by the content cap'}],
                 [{"mut": 'fixed_lane.py', "old": '        os.replace(tmp, full)\n        _drop_pyc(full)', "new": '        os.replace(tmp, full)', "why": 'stale bytecode not dropped after a write'}]]),
    dict(id='obsmask',
         name='worker: old bulky tool outputs are masked in the SENT prompt (placeholder keeps call + first line + re-read hint), recent turns / ERROR feedback / the latest verify output never masked, prefix advances in strides, kill switch',
         tool="test-worker-obs-mask-lane.py", marker="ALL PASS", timeout=300,
         proofs=[[{"mut": 'ollama-worker.py', "old": '    _mask_on = _wr.obs_mask_enabled(_rb)', "new": '    _mask_on = False', "why": 'masking never armed'}],
                 [{"mut": 'worker_robust.py', "old": 'if idx >= boundary or idx == last_verify or len(text) < min_chars:', "new": 'if idx >= boundary or len(text) < min_chars:', "why": 'latest verify output masked'}],
                 [{"mut": 'worker_robust.py', "old": '    if rank - state.get("rank", 0) >= stride:', "new": '    if True:', "why": 'prefix rewritten every turn (cache-hostile)'}],
                 [{"mut": 'worker_robust.py', "old": '        if text.lstrip().startswith("ERROR"):\n            continue\n', "new": '', "why": 'parse-error feedback masked'}],
                 [{"mut": 'ollama-worker.py', "old": 'resp = call_ollama(host, model, _view(), _eff_temperature, num_ctx, timeout=chat_timeout,\n                                        tools', "new": 'resp = call_ollama(host, model, messages, _eff_temperature, num_ctx, timeout=chat_timeout,\n                                        tools', "why": 'masked view never sent'}]]),
    dict(id='extraiters',
         name='worker: --extra-iterations raises the iteration ceiling for one round (additive, capped)',
         tool="test-worker-obs-mask-lane.py", marker="ALL PASS", timeout=300, share='obsmask',
         proofs=[[{"mut": 'ollama-worker.py', "old": '    total_iters, _extra_applied = apply_extra_iterations(total_iters, extra_iterations)', "new": '    _extra_applied = 0', "why": 'flag ignored'}],
                 [{"mut": 'ollama-worker.py', "old": '    n = max(0, min(n, EXTRA_ITERATIONS_MAX))', "new": '    n = max(0, n)', "why": 'unbounded grant'}]]),
    dict(id='fixedmode',
         name='worker --mode fixed: lane pass finishes via the normal verify path with no open-loop turns; failure injects seed diff + last verify output, applies the seed, and ends with a fixed_lane_* terminal reason',
         tool="test-worker-obs-mask-lane.py", marker="ALL PASS", timeout=300, share='obsmask',
         proofs=[[{"mut": 'ollama-worker.py', "old": '    if mode in ("auto", "fixed"):', "new": '    if False:', "why": 'lane never tried'}],
                 [{"mut": 'ollama-worker.py', "old": '            total_iters = i      # nothing left', "new": '            pass      # nothing left', "why": 'open loop still runs after a lane pass'}],
                 [{"mut": 'ollama-worker.py', "old": '        messages[1]["content"] = str(messages[1].get("content") or "") + fixed_lane_handoff_text(handoff)', "new": '        pass', "why": 'handoff not injected'}],
                 [{"mut": 'ollama-worker.py', "old": '        if terminal_reason == "nonconvergence" and _fixed_lane_reason:', "new": '        if False:', "why": 'lane failure reason dropped'}],
                 [{"mut": 'ollama-worker.py', "old": '    handoff["seed_applied"] = _apply_fixed_lane_seed(cwd, handoff)', "new": '    handoff["seed_applied"] = False', "why": 'seed diff not applied'}],
                 [{"mut": 'ollama-worker.py', "old": '    elif api_style != "openai":', "new": '    elif False:', "why": 'lane runs on a non-OpenAI lane'}]]),
    dict(id='stalepyc',
         name='worker: verify/shell runs never write bytecode and file writes drop cached .pyc (same-size same-second edit verified against old bytecode)',
         tool="test-worker-obs-mask-lane.py", marker="ALL PASS", timeout=300, share='obsmask',
         proofs=[[{"mut": 'ollama-worker.py', "old": '    env["PYTHONDONTWRITEBYTECODE"] = "1"\n', "new": '', "why": 'shells write bytecode again'}],
                 [{"mut": 'ollama-worker.py', "old": '    p.write_text(content)\n    _drop_stale_pyc(p)\n', "new": '    p.write_text(content)\n', "why": 'write_file leaves stale pyc'}],
                 [{"mut": 'ollama-worker.py', "old": '    p.write_text(new_content)\n    _drop_stale_pyc(p)\n    return f"OK: replaced 1 occurrence in {args[\'path\']} ({len(new_content)} bytes total)"', "new": '    p.write_text(new_content)\n    return f"OK: replaced 1 occurrence in {args[\'path\']} ({len(new_content)} bytes total)"', "why": 'edit_file leaves stale pyc'}]]),
    dict(id='turnlog',
         name='worker metrics carry a per-turn log (thinking chars, parse outcome, finish_reason, tokens)',
         tool="test-worker-obs-mask-lane.py", marker="ALL PASS", timeout=300, share='obsmask',
         proofs=[[{"mut": 'ollama-worker.py', "old": '        if len(_dispatch_metrics["turn_log"]) < TURN_LOG_MAX:', "new": '        if False:', "why": 'turn log never written'}],
                 [{"mut": 'ollama-worker.py', "old": '        _turn_rec["parse"] = "ok" if tool_calls else "no_tool_call"\n', "new": '', "why": 'parse outcome not recorded'}],
                 [{"mut": 'ollama-worker.py', "old": '    inline = content.split("</think>", 1)[0] if "</think>" in content else ""', "new": '    inline = ""', "why": 'inline thinking not counted'}]]),
    dict(id="modelcarddrift",
         name="model-card drift: every profile cites an existing card whose sha256 is unchanged, and each mode's "
              "temperature/top_p/top_k/penalties are stated together in that card",
         tool="test-model-profile-conformance.py", args=["--drift"], marker="CONFORMANT",
         proofs=[[{"mut": "model_profiles.yaml",
                   "old": "{temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.0, max_tokens: 32768, ctx: 131072, enable_thinking: true, preserve_thinking: true",
                   "new": "{temperature: 0.15, top_p: 0.95, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.0, max_tokens: 32768, ctx: 131072, enable_thinking: true, preserve_thinking: true",
                   "why": "profile temperature edited away from the card"}],
                 [{"mut": "model_profiles.yaml",
                   "old": "    source: \"card L663-665 (sampling)",
                   "new": "    sourcex: \"card L663-665 (sampling)",
                   "why": "profile loses its card citation"}],
                 [{"mut": "model_profiles.yaml",
                   "old": "card: models/qwen3.5-9b-modelcard.md",
                   "new": "card: models/qwen3.5-9b-NOPE.md",
                   "why": "cited card file does not exist"}],
                 [{"mut": "model_profile.py",
                   "old": "            if want != got:",
                   "new": "            if False:",
                   "why": "card-changed (sha256) check disabled"}],
                 [{"mut": "model_profiles.yaml",
                   "old": "{temperature: 0.7, top_p: 0.8, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.0, max_tokens: 32768, ctx: 32768, enable_thinking: false, preserve_thinking: null, preserve_reasoning: false, stop: []}\n\n  gemma",
                   "new": "{temperature: 0.9, top_p: 0.8, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.0, max_tokens: 32768, ctx: 32768, enable_thinking: false, preserve_thinking: null, preserve_reasoning: false, stop: []}\n\n  gemma",
                   "why": "qwen3:8b review temperature drifts from the card"}]]),

    dict(id="slicelabel",
         name="ollama-dispatch-slice accepts a BARE plan label (resolves slice-plans/<label>.slices.json); an unknown "
              "label is a clear error naming both candidate paths, never a FileNotFoundError traceback",
         tool="test-slice-plan-label.py", marker="ALL PASS", timeout=300,
         proofs=[["swap:ollama-dispatch-slice=ollama-dispatch-slice.bak-20261008-planlabel"],
                 [{"mut": "ollama-dispatch-slice", "old": "        a.plan = resolve_plan_arg(a.plan)", "new": "        pass",
                   "why": "bare label no longer resolved"}],
                 [{"mut": "ollama-dispatch-slice", "old": "neither {cands[0]} nor {cands[1]} exists",
                   "new": "neither {cands[0]} exists", "why": "error names only one candidate"}]]),
    dict(id="qctlretire",
         name="qctl retire BUNDLE --reason: supersede + plan --cancel + resolve finished rows; refuses a pending/running "
              "job, an awaiting_signoff row, an empty reason or an unreachable run-status API; records the reason; "
              "idempotent; leaves paused rows and other bundles alone",
         tool="test-qctl.py", marker="ALL PASS", timeout=300,
         proofs=[["swap:qctl=qctl.bak-20261008-retire"],
                 [{"mut": "qctl", "old": '    live = [j for j in rows if j.get("status") in RETIRE_LIVE]', "new": '    live = []',
                   "why": "retire proceeds with a live job"}],
                 [{"mut": "qctl", "old": '    held = [j["id"] for j in rows if j.get("id") in set(signoff_ids)]', "new": '    held = []',
                   "why": "retire ignores awaiting_signoff"}],
                 [{"mut": "qctl", "old": '    if own:\n        print("  cancel: plan already', "new": '    if False:\n        print("  cancel: plan already',
                   "why": "retire is not idempotent (re-cancels)"}]]),
    dict(id="retiredqueue",
         name="dashboard: a human-cancelled (plan_cancel) or qctl-superseded bundle with no pending/running job is retired "
              "-- gone from the live Queue panel (views AND grouped rows) and from Needs attention; a live job keeps it",
         tool="test-bundle-retired-queue.py", args=["--api", "{bin}/ollama-queue-api.py"],
         marker="ALL PASSED", timeout=300,
         proofs=[[{"mut": "ollama-queue-api.py", "old": "        if k in sup or canc:\n            out.add(k)",
                   "new": "        if False:\n            out.add(k)", "why": "markers ignored"}],
                 [{"mut": "ollama-queue-api.py", "old": "        if k in live:\n            continue\n", "new": "",
                   "why": "a marker hides a bundle with a live job"}],
                 [{"mut": "ollama-queue-api.py", "old": "        if k in _retired:\n            continue\n        prog = _load_plan_progress",
                   "new": "        prog = _load_plan_progress", "why": "retired plan's done rows rebuilt into 'pending X/Y'"}],
                 [{"mut": "ollama-queue-api.py", "old": "        if gk and gk in _retired:\n            continue\n", "new": "",
                   "why": "superseded bundle still raises Needs attention"}]]),

    dict(id="healnonterm",
         name="dashboard: a job whose heal/escalation ladder still has a pending/held/running esc-review row is "
              "NON-terminal (escalation-review, not red failed, headline failed count agrees); failed only when no heal row "
              "is outstanding; a waiting row's blocker reads 'waiting for job <id> (<label>; another bundle: <key>) to finish'",
         tool="test-heal-nonterminal.py", args=["--api", "{bin}/ollama-queue-api.py"],
         marker="ALL PASSED", timeout=300,
         proofs=[[{"mut": "bundle_view.py", "old": "        elif heal_wait:\n", "new": "        elif False:\n",
                   "why": "pending heal row still renders red failed"}],
                 [{"mut": "bundle_view.py", "old": "            if esc_review_ref(j.get(\"label\")) and j.get(\"status\") in _LIVE]",
                   "new": "            if esc_review_ref(j.get(\"label\")) and j.get(\"status\") == \"running\"]",
                   "why": "only a RUNNING heal row counts (pending still failed)"}],
                 [{"mut": "ollama-queue-api.py", "old": "        whose = f\"another bundle: {bk}\"", "new": "        whose = \"\"",
                   "why": "blocker no longer says whose job it is"}],
                 [{"mut": "ollama-queue-api.py", "old": "        return (_busy_job_phrase(r, busy[0])",
                   "new": "        return (f\"held on running job {busy[0].get('id')} ({busy[0].get('label')})\"",
                   "why": "old ambiguous held-on-running-job text"}]]),

    dict(id="relnecessity",
         name="relevance: every hunk of the reference solution must be NECESSARY (revert one -> verify red); "
              "an untested hunk is LOW naming file:start-end; doc-only / single-hunk / Must-contain exemptions explicit",
         tool="test-relevance-necessity-bank.py", marker="ALL PASS", timeout=600,
         proofs=[[{"mut": "verify-relevance.py",
                   "old": "    if necessity:\n        cands: list[Mutant] = []",
                   "new": "    if False:\n        cands: list[Mutant] = []", "why": "per-hunk necessity gate disabled"}],
                 [{"mut": "verify-relevance.py", "old": "    if reasons:\n        rec[\"verdict\"] = \"low\"",
                   "new": "    if reasons:\n        pass", "why": "necessity / bank findings no longer fail the verdict"}],
                 [{"mut": "verify-relevance.py", "old": "                if not _lit_ok(m, file_lits):\n                    nec[\"exempt\"]",
                   "new": "                if False:\n                    nec[\"exempt\"]", "why": "Must-contain exemption removed"}]]),
    dict(id="relbank",
         name="relevance: the WRONG-SOLUTION BANK (boundary / swapped compare / dropped guard / wrong default / "
              "early return) must be rejected by the fixture; a surviving plausible-wrong variant is LOW",
         tool="test-relevance-necessity-bank.py", marker="ALL PASS", timeout=600,
         proofs=[[{"mut": "verify-relevance.py", "old": "    if bank:\n        pool = [m for m in objs",
                   "new": "    if False:\n        pool = [m for m in objs", "why": "wrong-solution bank disabled"}],
                 [{"mut": "verify-relevance.py",
                   "old": "    if not rel.endswith(\".py\"):\n        return []\n    try:\n        tree = ast.parse(fixed)\n    except SyntaxError:\n        return []\n    annotated, _ = optout_lines(fixed)\n    src = Src(fixed)",
                   "new": "    return []\n    annotated, _ = optout_lines(fixed)\n    src = Src(fixed)",
                   "why": "early-return / wrong-default variants no longer generated"}],
                 [{"mut": "verify-relevance.py", "old": "    elif untested_msgs and rec.get(\"verdict\") == \"relevant\":",
                   "new": "    elif False:", "why": "unrun necessity/bank checks no longer fail closed"}]]),
    dict(id="mutfb",
         name="refine: surviving mutants are fed back for at most 3 rounds, stopping early on a round with no new "
              "kills; per-round kill counts logged and carried in the escalation",
         tool="test-refine-mutation-feedback.py", marker="ALL PASS", timeout=600,
         proofs=[[{"mut": "ollama-dispatch-auto", "old": "    if fed >= mx:\n", "new": "    if False:\n",
                   "why": "survivor-feedback round cap removed"}],
                 [{"mut": "ollama-dispatch-auto",
                   "old": "    if len(history) >= 2 and not mutfb_progress(history[-2], history[-1]):",
                   "new": "    if False:", "why": "no early stop on a round without new kills"}]]),
    dict(id="envfixture",
         name="sealed environment fixture: dispatch-env.ts/.py dropped read-only by the scaffold, restored by the "
              "self-check, comma selectors work; hand-written fake page/window/document/selector engine is bounced",
         tool="test-dispatch-env-fixture.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'dispatch-templates/dispatch-env.ts', "old": "splitTopLevel(sel, ',')", "new": "splitTopLevel(sel, '\\u0001')", "why": 'comma selector lists no longer split (the smoke-bbafd0efed38 failure)'}],
                 [{"mut": 'dispatch_env_fixture.py', "old": '        for m in _CLASS_RE.finditer(src):\n', "new": '        for m in []:\n', "why": 'class FakePage not linted'}],
                 [{"mut": 'dispatch_env_fixture.py', "old": '            same = dst.read_bytes() == data\n', "new": '            same = True\n', "why": 'tampered sealed file reported ok, not restored'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '_ef_bad = _ef.lint_fake_env(WT, _ef_files) if (WT / _ef.JS_NAME).is_file() else []', "new": '_ef_bad = []', "why": 'self-check never runs the fake-DOM lint'}],
                 [{"mut": 'ollama-dispatch-auto', "old": 'for _en, _es in _ef.ensure(WT, _ef_names, bin_dir=_ef_p.parent):', "new": 'for _en, _es in []:', "why": 'self-check no longer restores the sealed fixture'}],
                 [{"mut": 'ollama-dispatch-scaffold', "old": 'a._env_fixtures = _drop_env_fixtures(root, a)', "new": 'a._env_fixtures = []', "why": 'scaffold stops dropping the sealed fixture'}]]),
    dict(id="passinvert",
         name="pass-then-invert authoring flow: the author prompt and the baseline-green self-check message carry it",
         tool="test-dispatch-env-fixture.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'ollama-dispatch-auto', "old": '+ ("\\nPASS-THEN-INVERT: this green', "new": '+ ("" and "\\nPASS-THEN-INVERT: this green', "why": 'baseline-green message loses the invert move'}],
                 [{"mut": 'ollama-dispatch-auto', "old": '\n{PASS_THEN_INVERT_TEXT}\n', "new": '\n', "why": 'author prompt loses the pass-first-then-invert flow'}]]),
    dict(id="scafbase",
         name="scaffold --repo: base resolved once under the per-repo fetch lock, creation decided from the base tree, "
              "base_sha recorded; explicit --baseline-ref = no fetch",
         tool="test-scaffold-base-resolution.py", marker="ALL PASS", timeout=420,
         proofs=[[{"mut": 'ollama-dispatch-scaffold', "old": '        _in_base = sh(["git", "-C", str(repo), "cat-file", "-e", f"{_base_obj}:{a.target}"])[0] == 0\n', "new": '        _in_base = (repo / a.target).exists()\n', "why": 'creation decided from the source working copy, not the resolved base'}],
                 [{"mut": 'ollama-dispatch-scaffold', "old": '        _recorded_base = (base_sha, _base_note)', "new": '        _recorded_base = (None, "")', "why": 'base_sha no longer recorded in the manifest'}],
                 [{"mut": 'ollama-dispatch-scaffold', "old": '                fcntl.flock(_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n', "new": '                pass\n', "why": 'base fetch no longer takes the per-repo lock'}],
                 [{"mut": 'ollama-dispatch-scaffold', "old": '        base_ref = a.baseline_ref\n        if base_ref:', "new": '        base_ref = None\n        if base_ref:', "why": 'explicit --baseline-ref ignored (fetches/re-bases)'}]]),
    dict(id='landground',
         name='land: reviewer findings must cite file:line + a verbatim quote that exists; ungrounded findings dropped and counted',
         tool='test-land-ground.py',
         marker='ALL PASS',
         timeout=120,
         proofs=[[{'mut': 'land_ground.py', 'old': '            return False, f"quote {q!r} does not exist at {rel}:{a}-{b}", f', 'new': '            pass', 'why': 'quote-exists check removed'}], [{'mut': 'land_ground.py', 'old': '        f["blocking"] = False\n        f["demoted"] = "blocking finding without an executable repro is advisory"', 'new': '        pass', 'why': 'blocking finding without repro stays blocking'}], [{'mut': 'land_ground.py', 'old': '        about_removal = (', 'new': '        about_removal = True or (', 'why': 'removed lines ground any finding'}]],
         proof_fail=''),
    dict(id='landgo',
         name='land: HARNESS GO runs confirm/seal/enqueue only on a measured, clean contract; otherwise back to re-author (never a human)',
         tool='test-land-go.py',
         marker='ALL PASS',
         timeout=120,
         proofs=[[{'mut': 'land_go.py', 'old': '    if nec is None:\n', 'new': '    if False:\n', 'why': 'necessity not-measured accepted as clean'}], [{'mut': 'land_go.py', 'old': '        if score < floor:\n', 'new': '        if False:\n', 'why': 'relevance threshold ignored'}], [{'mut': 'land_go.py', 'old': '    elif bank.get("survivors"):', 'new': '    elif False:', 'why': 'wrong-solution bank survivors ignored'}], [{'mut': 'land_go.py', 'old': '        if _count_recent(ledger, label, "reauthor") >= REAUTHOR_CAP_PER_DAY:', 'new': '        if False:', 'why': 'no re-author cap'}]]),
    dict(id='landpacket',
         name='land: packet facts (tests weakened, secrets, deps, scope), repo checks vs baseline, risk tier, precomputed revert',
         tool='test-land-packet.py',
         marker='ALL PASS',
         timeout=180,
         proofs=[[{'mut': 'land_packet.py', 'old': '            if ra > aa:', 'new': '            if False:', 'why': 'removed assertions not detected'}], [{'mut': 'land_packet.py', 'old': '    if cs == "fail":', 'new': '    if False:', 'why': 'a red repo check does not block'}], [{'mut': 'land_packet.py', 'old': '                    if brc != 0:\n                        row["status"] = "preexisting"', 'new': '                    if False:\n                        row["status"] = "preexisting"', 'why': 'pre-existing red blamed on the change'}]]),
    dict(id='landadjudicate',
         name='land: CONCERNS adjudication cannot accept over a mechanical blocker, re-run->re-spec on the same diff, budgets, refusal != accept',
         tool='test-land-adjudicate.py',
         marker='ALL PASS',
         timeout=120,
         proofs=[[{'mut': 'land_adjudicate.py', 'old': '    if rec["decision"] == "accept" and (packet.get("blockers") and any(', 'new': '    if False and (packet.get("blockers") and any(', 'why': 'accept overrides a mechanical blocker'}], [{'mut': 'land_adjudicate.py', 'old': '    if rec["decision"] == "re-run" and same:', 'new': '    if False and same:', 'why': 'same diff re-run forever'}], [{'mut': 'land_adjudicate.py', 'old': '        m = re.match(r"^\\s*VERDICT:\\s*(\\{.*\\})\\s*$", ln)', 'new': '        m = re.match(r"^\\s*(?:VERDICT:)?\\s*(\\{.*\\})\\s*$", ln)', 'why': 'bare JSON without the VERDICT marker parsed as an answer'}]]),
    dict(id='landmode',
         name='land: REVIEW is the default and fail-safe; REVIEW never lands by itself (two independent guards)',
         tool='test-ollama-land.py',
         marker='ALL PASS',
         timeout=240,
         proofs=[[{'mut': 'ollama-land', 'old': '    return "AUTO" if t == "AUTO" else "REVIEW"', 'new': '    return "AUTO"', 'why': 'any mode text means AUTO'}], [{'mut': 'ollama-land', 'old': '            if mode == "AUTO" and P["state"] == "awaiting_final_review":', 'new': '            if P["state"] == "awaiting_final_review":', 'why': 'scan lands in REVIEW mode (guard 1)'}, {'mut': 'ollama-land', 'old': '            if get_mode() != "AUTO":', 'new': '            if False:', 'why': 'approve(auto) ignores the mode (guard 2)'}]]),
    dict(id='landpostland',
         name='land: post-land regression (green on base, red on landed after a flake rerun) auto-reverts; pre-existing red does not',
         tool='test-ollama-land.py',
         marker='ALL PASS',
         share='landmode',
         timeout=240,
         proofs=[[{'mut': 'ollama-land', 'old': '        if res["status"] == "fail":', 'new': '        if False:', 'why': 'post-land regression not reverted'}]]),
    dict(id='landbreaker',
         name='land: two consecutive post-land reverts open the per-repo circuit breaker and approve refuses',
         tool='test-ollama-land.py',
         marker='ALL PASS',
         share='landmode',
         timeout=240,
         proofs=[[{'mut': 'ollama-land', 'old': '    if b["consecutive_reverts"] >= 2:', 'new': '    if False:', 'why': 'breaker never opens'}]]),
    dict(id='landauto',
         name='land: AUTO needs kill-switch file absent, tier <= auto_max_tier, shadow agreements, a PASS verdict (LLM adjudication never auto-approves)',
         tool='test-ollama-land.py',
         marker='ALL PASS',
         share='landmode',
         timeout=240,
         proofs=[[{'mut': 'ollama-land', 'old': '    if (kill if kill is not None else KILL_FILE.exists()):', 'new': '    if False:', 'why': 'kill switch ignored'}], [{'mut': 'ollama-land', 'old': '    if TIER_ORDER.get(r.get("tier"), 9) > TIER_ORDER.get(cfg["auto_max_tier"], 0):', 'new': '    if False:', 'why': 'tier cap ignored'}], [{'mut': 'ollama-land', 'old': '    if (P.get("gate") or {}).get("verdict") != "pass":', 'new': '    if False:', 'why': 'CONCERNS chain auto-eligible'}], [{'mut': 'ollama-land', 'old': '    if agree < cfg["min_shadow_agreements"]:', 'new': '    if False:', 'why': 'shadow period ignored'}]]),
    dict(id='landstale',
         name='land: approve refuses (and lands nothing) when main or the staged branch moved since the packet',
         tool='test-ollama-land.py',
         marker='ALL PASS',
         share='landmode',
         timeout=240,
         proofs=[[{'mut': 'ollama-land', 'old': '        if cur != P["base_tip"] or tip != P["branch_tip"]:', 'new': '        if False:', 'why': 'stale packet can still land'}]]),
    dict(id='landingest',
         name='gate hands a landable standalone dispatch to ollama-land ingest (and only that)',
         tool='test-gate-land-ingest.py',
         marker='ALL PASS',
         timeout=120,
         proofs=[[{'mut': 'gate-on-complete.py', 'old': '    if str(payload.get("verdict")) not in ("pass", "concerns"):\n        return\n    tool = BIN', 'new': '    tool = BIN', 'why': 'every verdict is ingested'}], [{'mut': 'gate-on-complete.py', 'old': '                land_ingest_consider(a.job_id, payload)\n', 'new': '                pass\n', 'why': 'gate never calls ingest'}]]),
    dict(id='escverdict',
         name='esc: a review with no VERDICT line still resolves to a verdict (mechanical class > structured > inferred > reason > default b); cached, signature-masked, bigger model from config',
         tool='test-escalation-actions.py',
         args=['--heal', '{bin}/dispatch-self-heal.py', '--watcher', '{bin}/dispatch-escalation-watcher.py', '--verdict', '{bin}/escalation_verdict.py', '--handoff', '{bin}/handoff-autoclear.py'],
         marker='checks passed',
         timeout=240,
         proofs=[[{'mut': 'escalation_verdict.py', 'old': '    out = {"verdict": "b", "source": "default",', 'new': '    out = {"verdict": None, "source": "default",', 'why': 'mutant 0 of escalation_verdict.py'}], [{'mut': 'escalation_verdict.py', 'old': '    if fc in _STRICT_CLASSES:', 'new': '    if False:', 'why': 'mutant 1 of escalation_verdict.py'}], [{'mut': 'escalation_verdict.py', 'old': '    if cached:\n        return dict(cached, cached=True)', 'new': '    if False:\n        return dict(cached, cached=True)', 'why': 'mutant 2 of escalation_verdict.py'}], [{'mut': 'escalation_verdict.py', 'old': '_NOISE = re.compile(r"0x[0-9a-f]+|\\b[0-9a-f]{7,}\\b|\\d+")', 'new': '_NOISE = re.compile(r"NEVERMATCHZZ")', 'why': 'mutant 3 of escalation_verdict.py'}], [{'mut': 'escalation_verdict.py', 'old': 're.compile(r"\\b(?:my |the |final )?verdict\\s+(?:is|=)', 'new': 're.compile(r"\\bXXverdict\\s+(?:is|=)', 'why': 'mutant 4 of escalation_verdict.py'}], [{'mut': 'escalation_verdict.py', 'old': '    if env.get("ESC_BIGGER_MODEL"):\n        cfg["bigger_model"] = env["ESC_BIGGER_MODEL"]', 'new': '    pass', 'why': 'mutant 5 of escalation_verdict.py'}]]),
    dict(id='escheal',
         name='esc: the verdict DRIVES an action -- needs-opus-auto rows eligible, operator->resume, same signature twice->re-spec, (d)->configured bigger model, kill switch, slice abandon-after-N',
         tool='test-escalation-actions.py',
         args=['--heal', '{bin}/dispatch-self-heal.py', '--watcher', '{bin}/dispatch-escalation-watcher.py', '--verdict', '{bin}/escalation_verdict.py', '--handoff', '{bin}/handoff-autoclear.py'],
         marker='checks passed',
         timeout=240,
         share='escverdict',
         proofs=[[{'mut': 'dispatch-self-heal.py', 'old': '            if line.startswith("- [ ] ") and jid in line:', 'new': '            if False:', 'why': 'acted outcome does not tick the rows'}], [{'mut': 'dispatch-self-heal.py', 'old': 'AUTHOR_LABEL_PREFIXES = ("auto-author-", "needs-opus-auto-")', 'new': 'AUTHOR_LABEL_PREFIXES = ("auto-author-",)', 'why': 'mutant 0 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if repeat:\n        return respec_or_park(', 'new': '    if False:\n        return respec_or_park(', 'why': 'mutant 1 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '        if source == "mechanical":', 'new': '        if False:', 'why': 'mutant 2 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '        if bm and has_model and job.get("model") != bm and not tried:', 'new': '        if False:', 'why': 'mutant 3 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if ev.actions_killed():\n        return "skip:ACTIONS-OFF kill switch set"', 'new': '    if False:\n        return "skip:ACTIONS-OFF kill switch set"', 'why': 'mutant 4 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '        if okc:\n            try:', 'new': '        if True:\n            try:', 'why': 'mutant 5 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if (_n_same >= int(_cfg["abandon_same_signature_n"]) or used >= MAX_ATTEMPTS):', 'new': '    if False:', 'why': 'mutant 6 of dispatch-self-heal.py'}], [{'mut': 'dispatch-self-heal.py', 'old': '    can_respec = respecs < int(cfg.get("respec_max_per_chain") or 1)', 'new': '    can_respec = True', 'why': 'mutant 7 of dispatch-self-heal.py'}]]),
    dict(id='escwatch',
         name='esc: watcher re-runs a verdict-less review once then machine-resolves; acted rows close [x]; prompt demands ESC_RESULT',
         tool='test-escalation-actions.py',
         args=['--heal', '{bin}/dispatch-self-heal.py', '--watcher', '{bin}/dispatch-escalation-watcher.py', '--verdict', '{bin}/escalation_verdict.py', '--handoff', '{bin}/handoff-autoclear.py'],
         marker='checks passed',
         timeout=240,
         share='escverdict',
         proofs=[[{'mut': 'dispatch-escalation-watcher.py', 'old': '        if is_review_blocked(review) or ev.has_verdict(review) or ev.is_no_verdict_review(review):', 'new': '        if True:', 'why': 'mutant 0 of dispatch-escalation-watcher.py'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': 'ACTED_PREFIXES = ("skip-verified:", "closed:", "respec:",', 'new': 'ACTED_PREFIXES = ("skip-verified:", "closed:",', 'why': 'mutant 1 of dispatch-escalation-watcher.py'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': 'ESC_RESULT: {{"verdict": "<a|b|c|d>", "confidence": "<high|medium|low>", "why": "<one sentence', 'new': 'ESC_RES: {{"verdict": "<a|b|c|d>", "confidence": "<high|medium|low>", "why": "<one sentence', 'why': 'mutant 2 of dispatch-escalation-watcher.py'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '        if int(rec.get("verdict_reruns") or 0) < int(cfg["rerun_review_max"]):', 'new': '        if True:', 'why': 'mutant 3 of dispatch-escalation-watcher.py'}]]),
    dict(id='eschandoff',
         name='handoff autoclear: sign-off/live/landable guards beat every rule and any model answer; Sonnet batch cached + rate limited; kill switch',
         tool='test-escalation-actions.py',
         args=['--heal', '{bin}/dispatch-self-heal.py', '--watcher', '{bin}/dispatch-escalation-watcher.py', '--verdict', '{bin}/escalation_verdict.py', '--handoff', '{bin}/handoff-autoclear.py'],
         marker='checks passed',
         timeout=240,
         share='escverdict',
         proofs=[[{'mut': 'handoff-autoclear.py', 'old': '    if row.get("awaiting_signoff"):\n        return "awaiting sign-off"', 'new': '    if False:\n        return "awaiting sign-off"', 'why': 'mutant 0 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if lj is not None:', 'new': '    if False:', 'why': 'mutant 1 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if cwd and live_cwds.get(cwd) and live_cwds[cwd] - {jid}:', 'new': '    if False:', 'why': 'mutant 2 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if row.get("code_high"):', 'new': '    if False:', 'why': 'mutant 3 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if (row.get("status") == "done" and (row.get("gate") or "") == "PASS"', 'new': '    if (False and (row.get("gate") or "") == "PASS"', 'why': 'mutant 4 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '        g = guard(row, live_by_id, live_cwds, handlocked=handlocked)\n        if g:', 'new': '        g = guard(row, live_by_id, live_cwds, handlocked=handlocked)\n        if False:', 'why': 'mutant 5 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if calls and now - max(calls) < LLM_MIN_GAP_S:', 'new': '    if False:', 'why': 'mutant 6 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    if h in cache:', 'new': '    if False:', 'why': 'mutant 7 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    assert not any(w in reason.lower() for w in MERGE_WORDS), reason', 'new': '    pass', 'why': 'mutant 8 of handoff-autoclear.py'}], [{'mut': 'handoff-autoclear.py', 'old': '    return str(env.get("HANDOFF_AUTOCLEAR", "on")).lower() == "off" or Path(kill or KILL).exists()', 'new': '    return False', 'why': 'mutant 9 of handoff-autoclear.py'}]]),
    dict(id="simsched",
         name="seeded deterministic simulation of the commit/park/next-step state machine (real "
              "bundle_commit_step/status + settle_next_steps vs a modelled world): single owner, no idle "
              "lane hold, no dead-declaration hold, no cpu-wait hold, bounded transitions, no starvation, "
              "restart equivalence (backlog H3)",
         tool="test-queue-scheduler-sim.py", args=["--queue", "{bin}/ollama-queue.py", "--seeds", "120"],
         marker="ALL PASS", timeout=420,
         proofs=[[{"mut": "ollama-queue.py",
                   "old": "            if any(j.get(\"id\") == d.get(\"job\") and j.get(\"status\") == ESCALATION_STATUS for j in jobs):\n",
                   "new": "            if False:\n", "why": "2026-10-09 needs_opus declaration keeps holding the lane"}],
                 [{"mut": "ollama-queue.py",
                   "old": "    if cpu_wait:\n        return \"waiting\", (f\"waiting on {len(cpu_wait)} CPU stage(s) \"\n"
                          "                           f\"({', '.join(str(x)[:12] for x in list(cpu_wait)[:3])}) -- holds no GPU lane\"), False\n",
                   "new": "", "why": "cpu_wait branch removed from its slot"},
                  {"mut": "ollama-queue.py",
                   "old": "    if plan.get(\"marker\"):\n        return \"working\", \"a completion event is waiting for the slicer\", False\n",
                   "new": "    if cpu_wait:\n        return \"waiting\", (f\"waiting on {len(cpu_wait)} CPU stage(s)\"), False\n"
                          "    if plan.get(\"marker\"):\n        return \"working\", \"a completion event is waiting for the slicer\", False\n",
                   "why": "cpu_wait checked AFTER chain.waiting/driver_live (older ordering bug)"}],
                 [{"mut": "ollama-queue.py", "old": "BUNDLE_IDLE_SOFT = 10 * 60.0", "new": "BUNDLE_IDLE_SOFT = 9000 * 60.0",
                   "why": "idle ceiling stays 45 min when another bundle waits"},
                  {"mut": "ollama-queue.py", "old": "BUNDLE_LEASE_S = float(os.environ.get(\"QUEUE_COMMIT_LEASE_S\", \"360\") or 0)",
                   "new": "BUNDLE_LEASE_S = 0.0", "why": "(lease off too: it is the tighter cap)"}],
                 [{"mut": "ollama-queue.py", "old": "                    if _pst == \"working\" and (_pmv or parked[_p].get(\"kind\") != \"stalled\"):\n",
                   "new": "                    if False:\n", "why": "sim seed 3: parked-runnable bundle not counted as waiting"},
                  {"mut": "ollama-queue.py", "old": "BUNDLE_LEASE_S = float(os.environ.get(\"QUEUE_COMMIT_LEASE_S\", \"360\") or 0)",
                   "new": "BUNDLE_LEASE_S = 0.0", "why": "(lease off too)"}],
                 [{"mut": "ollama-queue.py", "old": "            events.append((\"resume\", pk_, why))\n            return {\"key\": pk_, \"since\": now, \"empty_since\": None,\n                    \"idle_since\": None if moving else _carry_idle}, events",
                   "new": "            events.append((\"resume\", pk_, why))\n            return {\"key\": pk_, \"since\": now, \"empty_since\": None,\n                    \"idle_since\": None}, events",
                   "why": "sim seed 84: idle clock reset per holder"},
                  {"mut": "ollama-queue.py", "old": "BUNDLE_LEASE_S = float(os.environ.get(\"QUEUE_COMMIT_LEASE_S\", \"360\") or 0)",
                   "new": "BUNDLE_LEASE_S = 0.0", "why": "(lease off too)"}]]),
    dict(id="deadman",
         name="external dead-man's switch: stalled-by-symptom / daemon heartbeat / Darkbloom health roll up to "
              "one de-duped line per cause cluster, TTL DEADMAN.md, kill switch, read-only (backlog H2)",
         tool="test-queue-deadman.py", args=["--tool", "{bin}/queue-deadman.py"], marker="ALL PASS",
         proofs=[[{"mut": "queue-deadman.py", "old": "        elif now - c[\"logged\"] >= a.remind_s:\n",
                   "new": "        elif True:\n", "why": "no de-dupe: every poll logs"}],
                 [{"mut": "queue-deadman.py", "old": "        if now - s[\"since\"] > a.stall_min * 60:\n",
                   "new": "        if True:\n", "why": "no stall window: a momentary gap pages"}],
                 [{"mut": "queue-deadman.py", "old": "    if (dd / \"deadman.disabled\").exists():\n        return 0\n",
                   "new": "    if False:\n        return 0\n", "why": "kill switch ignored"}],
                 [{"mut": "queue-deadman.py", "old": "    if jobs is not None and not bloom_hold:\n",
                   "new": "    if jobs is not None:\n", "why": "intentional bloom hold reported as a stall"}],
                 [{"mut": "queue-deadman.py", "old": "        if mem[\"bad_health\"] >= 2:\n",
                   "new": "        if True:\n", "why": "single /health blip pages"}]]),
    dict(id="qhlease",
         name="worker identity (pid+start) + runner heartbeat lease: a recycled pid is not adopted; a wrapper runner whose driver is gone / crashed (unbound variable, silent) is declared dead and settles failed driver_crashed (backlog H1, 2026-10-09 dfee823972cb)",
         tool="test-queue-hardening.py", args=["--queue", "{bin}/ollama-queue.py", "--group", "lease,ident"],
         marker="ALL PASS", timeout=240,
         proofs=[[{"mut": "ollama-queue.py", "old": "    return (not cur) or cur == pid_start\n", "new": "    return True\n", "why": "recycled pid adopted as our worker"}],
                 [{"mut": "ollama-queue.py", "old": "    if now - since >= RUNNER_NO_DRIVER_GRACE_S:\n", "new": "    if False:\n", "why": "runner alive + driver gone stays running"}],
                 [{"mut": "ollama-queue.py", "old": "        if m and log_idle_s is not None and log_idle_s >= RUNNER_FATAL_IDLE_S:\n", "new": "        if False:\n", "why": "crashed driver (unbound variable) stays running"}],
                 [{"mut": "ollama-queue.py", "old": "            if now - float(dd.get(\"ts\") or now) >= RUNNER_KILL_ESCALATE_S and not dd.get(\"killed9\"):\n", "new": "            if False:\n", "why": "SIGTERM-resistant runner never SIGKILLed"}],
                 [{"mut": "ollama-queue.py", "old": "                _dead_leases, _lease_dirty = _enforce_runner_leases(state, active, time.time())\n", "new": "                _dead_leases, _lease_dirty = [], False\n", "why": "daemon never enforces leases"}],
                 [{"mut": "ollama-queue.py", "old": "                elif job.get(\"driver_dead\"):\n", "new": "                elif False:\n", "why": "lapsed runner settles as pause/other, not driver_crashed"}],
                 [{"mut": "ollama-queue.py", "old": "            _alive = proc_identity_alive(pid, j.get(\"pid_start\"))", "new": "            _alive = _pid_alive(pid)", "why": "startup adoption trusts bare kill(0)"}]]),
    dict(id="qhsched",
         name="commit/park hardening: parked-runnable counts as waiting, idle clock survives handoffs, H4 lease, H5 dwell+flap breaker, H7 degraded accounting, H6 attempts ledger, no lanes for a bundle without a runnable member, live heal continuation is pending",
         tool="test-queue-hardening.py", args=["--queue", "{bin}/ollama-queue.py", "--group", "commit,flap,degraded,attempts,status"],
         marker="ALL PASS", timeout=240,
         proofs=[[{"mut": "ollama-queue.py", "old": "                    if _pst == \"working\" and (_pmv or parked[_p].get(\"kind\") != \"stalled\"):\n", "new": "                    if False:\n", "why": "parked runnable bundles ignored as 'waiting' (sim seed 3)"}],
                 [{"mut": "ollama-queue.py", "old": "            events.append((\"resume\", pk_, why))\n            return {\"key\": pk_, \"since\": now, \"empty_since\": None,\n                    \"idle_since\": None if moving else _carry_idle}, events", "new": "            events.append((\"resume\", pk_, why))\n            return {\"key\": pk_, \"since\": now, \"empty_since\": None,\n                    \"idle_since\": None}, events", "why": "idle clock reset per holder (sim seed 84)"}],
                 [{"mut": "ollama-queue.py", "old": "            if _lease_cap:\n                _ceil = _ls\n", "new": "", "why": "H4 lease never caps a non-moving holder"}],
                 [{"mut": "ollama-queue.py", "old": "        if degraded and degraded():\n            return commit, events          # H7: never act on a degraded read\n", "new": "", "why": "H7 degraded read parks the committed bundle"}],
                 [{"mut": "ollama-queue.py", "old": "            if degraded and degraded():\n                continue                   # H7: a degraded read must not drop a park\n", "new": "", "why": "H7 degraded read drops a park"}],
                 [{"mut": "ollama-queue.py", "old": "                if len([t for t in (hist.get(pk_) or []) if now - t < BUNDLE_FLAP_WINDOW_S]) >= BUNDLE_FLAP_MAX:\n", "new": "                if False:\n", "why": "H5 flap breaker disabled"}],
                 [{"mut": "ollama-queue.py", "old": "                if (dwell_s and now - float(parked[pk_].get(\"since\") or 0) < dwell_s\n                        and not moving):\n", "new": "                if False:\n", "why": "H5 dwell disabled"}],
                 [{"mut": "ollama-queue.py", "old": "    live = [j for j in live if j.get(\"status\") != \"pending\" or row_runnable_in_principle(j, _by_id)]\n", "new": "", "why": "unrunnable pending rows keep a bundle 'working'"}],
                 [{"mut": "ollama-queue.py", "old": "        if _es is None and heal_continuation_live(j, _by_id):\n            _es = \"pending\"        # a live continuation / re-spec of it exists: not stuck\n", "new": "", "why": "live heal continuation parks the bundle needs-the owner"}],
                 [{"mut": "ollama-queue.py", "old": "            _degraded_note(\"esc_state\", _e)        # H7: flagged degraded, never a silent \"stuck\"\n", "new": "            pass\n", "why": "esc_state swallow is silent"}],
                 [{"mut": "ollama-queue.py", "old": "        _DEGRADED_TICK.add(name)\n", "new": "", "why": "swallowed input does not flag the tick"}],
                 [{"mut": "ollama-queue.py", "old": "            delta = 1 if _kind in ATTEMPT_BUDGET_KINDS else 0\n", "new": "            delta = 1\n", "why": "external pauses consume the retry budget"}],
                 [{"mut": "ollama-queue.py", "old": "chain = soft(chain_run_progress, \"chain_run_progress\", {}, k, runs_dir=chain_dir, now=now)", "new": "chain = chain_run_progress(k, runs_dir=chain_dir, now=now)", "why": "chain_run_progress failure is uncounted"}]]),
    dict(id="qhverbs",
         name="CLI post-condition receipts (H8), enqueue --mode/--fixed-target passthrough, revalidate-at-pop E gate",
         tool="test-queue-hardening.py", args=["--queue", "{bin}/ollama-queue.py", "--group", "receipts,mode,reval"],
         marker="ALL PASS", timeout=240,
         proofs=[[{"mut": "ollama-queue.py", "old": "    if not rc[\"ok\"]:\n        print(f\"[queue] NO-OP: {rc['reason']}\", file=sys.stderr)\n        sys.exit(RECEIPT_NOOP_RC)\n", "new": "", "why": "no-op verbs exit 0"}],
                 [{"mut": "ollama-queue.py", "old": "        elif not changed:\n            ok, why = False, f\"row was already {frm}; nothing to resume\"\n", "new": "", "why": "resume on a settled row reports success"}],
                 [{"mut": "ollama-queue.py", "old": "    if job.get(\"mode\") and _worker_supports_flag(\"--mode\"):\n", "new": "    if False:\n", "why": "--mode never reaches the worker"}],
                 [{"mut": "ollama-queue.py", "old": "            for _ft in job.get(\"fixed_targets\") or []:\n", "new": "            for _ft in []:\n", "why": "--fixed-target never reaches the worker"}],
                 [{"mut": "ollama-queue.py", "old": "        if rc == 3:\n            return \"park\", out.strip()[-1500:] or \"SPEC_DEFECT/NOP_GREEN (no detail)\"\n", "new": "", "why": "SPEC_DEFECT launches anyway"}],
                 [{"mut": "ollama-queue.py", "old": "        if not cwd or not Path(cwd, \".preflight-state.json\").is_file():\n            return \"launch\", \"no preflight snapshot\"\n", "new": "", "why": "no-snapshot worktree is not skipped cleanly"}],
                 [{"mut": "ollama-queue.py", "old": "        return \"launch\", f\"revalidate skipped ({type(e).__name__}: {e})\"", "new": "        raise", "why": "revalidate trouble blocks the launch"}],
                 [{"mut": "ollama-queue.py", "old": "    return _cascade_blocked(jobs)\n\n\n\ndef _build_cmd", "new": "    return []\n\n\n\ndef _build_cmd", "why": "parked spec-defect leaves downstream runnable"}],
                 [{"mut": "ollama-queue.py", "old": "_rv_act, _rv_detail = revalidate_at_pop(job)\n", "new": "_rv_act, _rv_detail = \"launch\", \"\"\n", "why": "launch loop never consults the E gate"}]]),
    dict(id='budgetpolicy', name='worker budget policy progress_extend: ONE extension at the cap when a write landed in the last 3 iterations and verify output changed; refused on stall / no write / past 1.5x ceiling; default (no policy) byte-identical', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'worker_budget.py', 'old': '        if not any(w > i - WRITE_RECENCY for w in self.write_iters):\n            return 0, f"no write/edit in the last {WRITE_RECENCY} iterations"\n', 'new': '', 'why': 'grants without a recent write'}], [{'mut': 'worker_budget.py', 'old': '        room = self._ceiling() - total_iters\n', 'new': '        room = 99\n', 'why': '1.5x hard ceiling not enforced'}], [{'mut': 'ollama-worker.py', 'old': 'while i < total_iters or _bp_ext():', 'new': 'while i < total_iters:', 'why': 'granted extension never reaches the loop'}], [{'mut': 'worker_budget.py', 'old': '        if len(tails) >= STALL_WINDOW and len(set(tails[-STALL_WINDOW:])) == 1:', 'new': '        if False:', 'why': 'identical-verify stall not refused (no-change rule is the only guard)'}, {'mut': 'worker_budget.py', 'old': '        if len(tails) >= 2 and tails[-1] == tails[-2]:', 'new': '        if False:', 'why': 'no-change rule off too'}]]),
    dict(id='budgetvisible', name='worker budget policy budget_visible: a one-line remaining-budget notice every turn', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'ollama-worker.py', 'old': 'if _bpr["budget_line"]:', 'new': 'if False:', 'why': 'notice never delivered'}]]),
    dict(id='budgetreadwindow', name='worker budget policy read_window: read_file pages show at least 100 lines', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'ollama-worker.py', 'old': '    if _READ_WINDOW[0] and (ln is None or ln < _READ_WINDOW[0]):\n        ln = _READ_WINDOW[0]\n', 'new': '', 'why': 'window ignored'}]]),
    dict(id='budgetmask', name='worker budget policy mask_stale_reads: old unedited read_file results become one-line stubs in the prompt copy', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'ollama-worker.py', 'old': '                    base = _bp.mask_view(base)\n', 'new': '                    pass\n', 'why': 'masking never applied to the sent prompt'}], [{'mut': 'worker_budget.py', 'old': '            if self.edit_seq.get(r["path"], 0) >= r["it"]:\n                continue        # edited since (or in that same turn): leave it alone\n', 'new': '', 'why': 'edited-since reads masked too'}]]),
    dict(id='budgetprefetch', name='worker budget policy prefetch_excerpt: target head/outline or named line range injected into the first prompt', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'ollama-worker.py', 'old': 'messages[1]["content"] = task + _pf_text', 'new': 'pass', 'why': 'excerpt built but never injected'}]]),
    dict(id='budgetrevert', name='worker budget policy checkpoint_revert: 3 consecutive worse edit rounds restore the best-so-far file', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'worker_budget.py', 'old': 'restored = self.restore_best(cwd or getattr(self, "cwd", None))', 'new': 'restored = []', 'why': 'revert never restores'}]]),
    dict(id='budgetstreak', name='worker budget policy read_streak_nudge: 5 read-only turns with no write -> write-a-first-draft nudge', share='budgetpolicy', tool='test-worker-budget-policy.py', marker='ALL PASS', timeout=600, proofs=[[{'mut': 'worker_budget.py', 'old': '                self.read_streak += 1\n', 'new': '                pass\n', 'why': 'streak never counted'}]]),
    dict(id='slicergap', name='A slicer-advance CPU/driver gap (no running/live row, no gate settling) holds no GPU lane past SLICER_GAP_YIELD_S: parked cpu_wait so other bundles backfill, resumes first when its next row exists; short gaps keep the lane', tool='test-slicer-gap-yield.py', args=['--bin', '{bin}'], marker='SLICER_GAP_YIELD_OK', timeout=240, proofs=[[{'mut': 'ollama-queue.py', 'old': '        if SLICER_GAP_YIELD_S > 0 and _since and _gap >= SLICER_GAP_YIELD_S:', 'new': '        if False:', 'why': 'a slicer-advance gap never yields the lane'}], [{'mut': 'ollama-queue.py', 'old': 'and _since and _gap >= SLICER_GAP_YIELD_S:', 'new': 'and _since:', 'why': 'a fresh advance (any age) yields: no grace, the model churns'}], [{'mut': 'ollama-queue.py', 'old': '            if _st is not None and (out.get("driver_since") is None or _st < out["driver_since"]):\n                out["driver_since"] = _st\n', 'new': '            pass\n', 'why': 'the advance start is never exposed to the status'}]]),
    dict(id='gatesettle', name='A committed bundle waiting ONLY on off-lane gate-on-complete hooks holds no GPU lane past SLICER_GAP_YIELD_S: parked cpu_wait so other bundles backfill, resumes first when the verdict enqueues a row; banner says what it waits for', tool='test-gate-settle-yield.py', args=['--bin', '{bin}'], marker='GATE_SETTLE_YIELD_OK', timeout=240, proofs=[[{'mut': 'ollama-queue.py', 'old': '        if SLICER_GAP_YIELD_S > 0 and _gs and _gg >= SLICER_GAP_YIELD_S:', 'new': '        if False:', 'why': 'a gate-settle gap never yields the lane'}], [{'mut': 'ollama-queue.py', 'old': '            _at = float(rec.get("at") or 0.0) or None\n            if _at and (_l.since is None or _at < _l.since):\n                _l.since = _at\n', 'new': '            pass\n', 'why': 'hook start never exposed, so the yield can never fire'}], [{'mut': 'ollama-queue.py', 'old': '    if why.startswith("complete") and not next_job:', 'new': '    if False:', 'why': 'banner keeps saying bare complete'}]]),
    dict(id='routingfb', name='auto: no-clean-decomposition falls back to the LLM planner (gated plan, bundle kept, idempotent, small target excluded); new-test-file warning suggests drop/--edit-file; worker exits spec_defect_repeat_abort -> SPEC_DEFECT path, sampling_ladder_exhausted -> re-spec', tool='test-auto-routing-fallback.py', args=['--bin', '{bin}'], marker='ROUTING_FALLBACK_OK', timeout=240, proofs=[[{'mut': 'ollama-dispatch-auto', 'old': '    if (action == "cannot-slice-escalate"\n            and not omnibus_slice', 'new': '    if (False and action == "cannot-slice-escalate"\n            and not omnibus_slice', 'why': 'no planner fallback on no-clean-decomposition'}], [{'mut': 'ollama-dispatch-auto', 'old': '            and not omnibus_slice.target_is_small(Path(a.repo) / target)[0]\n', 'new': '', 'why': 'a small target is sent to the planner'}], [{'mut': 'omnibus_slice.py', 'old': '    if pth.exists():\n        print(f"[auto] PLANNER FALLBACK ({reason}): reusing', 'new': '    if False:\n        print(f"[auto] PLANNER FALLBACK ({reason}): reusing', 'why': 'the planner regenerates a plan on every failure'}], [{'mut': 'omnibus_slice.py', 'old': '           "--auto-confirm-plan"]\n    if bundle:', 'new': '           ]\n    if bundle:', 'why': 'the planner stops for a human review nobody sees'}], [{'mut': 'omnibus_slice.py', 'old': '        cmd += ["--bundle", str(bundle)]\n    return cmd', 'new': '    return cmd', 'why': 'the planner is not told the bundle'}], [{'mut': 'ollama-dispatch-plan', 'old': '    if getattr(a, "bundle", None):\n        plan["bundle"] = str(a.bundle)', 'new': '    if False:\n        plan["bundle"] = str(a.bundle)', 'why': 'the generated plan loses its bundle'}], [{'mut': 'ollama-dispatch-auto', 'old': '    if tr == WORKER_EXIT_SPEC_DEFECT:', 'new': '    if False:', 'why': 'spec_defect_repeat_abort is not routed'}], [{'mut': 'ollama-dispatch-auto', 'old': '    if tr == WORKER_EXIT_LADDER:', 'new': '    if False:', 'why': 'sampling_ladder_exhausted is not routed'}], [{'mut': 'ollama-dispatch-auto', 'old': '{SPEC_DEFECT_ROUTE_PREFIX}sampling ladder exhausted on this spec.', 'new': 'sampling ladder exhausted on this spec.', 'why': 'the slicer cannot recognise the ladder exit as a re-spec'}], [{'mut': 'ollama-dispatch-auto', 'old': '        _wx = worker_exit_failure(a, why)\n        if _wx:\n            log_decision("spec-defect", a, "worker exit reason names a spec problem", _wx[:300])\n            return False, _wx\n        print(', 'new': '        print(', 'why': 'continuation rounds still burn after a spec-naming exit'}], [{'mut': 'ollama-dispatch-auto', 'old': '    return False, (worker_exit_failure(a, why) or why)', 'new': '    return False, why', 'why': 'the final failure is not routed'}], [{'mut': 'ollama-dispatch-auto', 'old': '    _wx = worker_exit_failure(a, why)\n    if _wx:\n        log_decision("spec-defect", a, "worker exit reason names a spec problem", _wx[:300])\n        return False, _wx\n    print(f"\\n[auto] staged', 'new': '    print(f"\\n[auto] staged', 'why': 'a staged failure falls to the continuation ladder'}], [{'mut': 'ollama-dispatch-auto', 'old': '    tests = [f for f in mm if _is_test_file_name(f)]\n    if tests:', 'new': '    tests = []\n    if tests:', 'why': 'the new-test-file warning loses its advice'}], [{'mut': 'ollama-dispatch-auto', 'old': 'SPEC_DEFECT_ROUTE_PREFIX, RESPEC_ROUTE_PREFIX)) or a.drafter_cmd', 'new': 'SPEC_DEFECT_ROUTE_PREFIX)) or a.drafter_cmd', 'why': 'a respec failure is escalated to another model round'}]]),
    dict(id='escbounds', name="Escalation review is bounded and evidence-gated: a bare job row's context carries the evidence resolved from its label (chain record, worktree, driver log, literals), an evidence-less context spawns NO model review (NO EVIDENCE AVAILABLE + deterministic verdict, escalation open), the review job is enqueued with a small output-token cap, the worker ends a review on a repeated read / output-cap hit / huge list_files with a named exit + REVIEW CAPPED marker, and self-heal never acts on a capped or no-evidence review", tool='test-esc-review-bounds.py', args=['--bin', '{bin}'], marker='ESC_REVIEW_BOUNDS_TEST_OK', timeout=300, proofs=[[{'mut': 'dispatch-escalation-watcher.py', 'old': '        if handle_no_evidence(e, body, ctx, stem, render_failed=_render_failed):\n            continue', 'new': '        if False:\n            continue', 'why': 'run_once spawns a model review for an evidence-less context'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '    ok, _missing = context_has_evidence(body)\n    if ok:\n        return False', 'new': '    ok, _missing = True, ""\n    if ok:\n        return False', 'why': 'the evidence gate never fires'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '        if handle_no_verdict_review(e, ctx, review, INDEX_MD):\n            continue', 'new': '        if False:\n            continue', 'why': 'a capped review falls through to classify/patch/self-heal'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '                 "--role", ESC_REVIEW_ROLE, "--max-tokens", str(int(max_tokens))]', 'new': '                 ]', 'why': 'esc-review enqueued without the bounded role/token cap'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': 'ESC_REVIEW_MAX_TOKENS = 6144', 'new': 'ESC_REVIEW_MAX_TOKENS = 32768', 'why': 'the review output cap is the unbounded profile default'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '        if rec:\n            out["chain_file"] = str(chain_file)', 'new': '        if False:\n            out["chain_file"] = str(chain_file)', 'why': 'the driver chain record is never resolved from the label'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': '        _jev = evidence if evidence is not None else resolve_job_evidence(e)\n        L += driver_evidence_lines(_jev)', 'new': '        _jev = {}\n        L += driver_evidence_lines(_jev)', 'why': 'a bare job row context omits the driver evidence (stub again)'}], [{'mut': 'dispatch-escalation-watcher.py', 'old': 'ev.has_verdict(review) or ev.is_no_verdict_review(review):', 'new': 'ev.has_verdict(review):', 'why': 'ensure_verdict re-classifies / machine-resolves a capped review'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if ev.is_no_verdict_review(review_text):\n        return "skip:no model verdict', 'new': '    if False:\n        return "skip:no model verdict', 'why': '_heal_job acts on a capped review'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if _ev().is_no_verdict_review(review_text):\n        return "skip:no model verdict', 'new': '    if False:\n        return "skip:no model verdict', 'why': 'slice heal acts on a capped review'}], [{'mut': 'dispatch-self-heal.py', 'old': '    if _ev().is_no_verdict_review(review):\n        return False\n    for line in', 'new': '    for line in', 'why': 'a capped review with an (a) line counts as already satisfied'}], [{'mut': 'escalation_verdict.py', 'old': 'NO_VERDICT_MARKERS = ("REVIEW CAPPED", "NO EVIDENCE AVAILABLE")', 'new': 'NO_VERDICT_MARKERS = ("REVIEW CAPPED",)', 'why': 'the no-evidence marker is not recognised'}], [{'mut': 'worker_robust.py', 'old': '        if self.run >= self.consecutive:', 'new': '        if False:', 'why': 'same read twice in a row is not stopped'}], [{'mut': 'worker_robust.py', 'old': '        if self.counts[key] >= self.total:', 'new': '        if False:', 'why': 'A,B,A,B,A re-reading evades the guard'}], [{'mut': 'ollama-worker.py', 'old': '            if _rev_bounds and _rev_abort is None:\n', 'new': '            if False:\n', 'why': 'repeat-call guard never observes a tool call'}], [{'mut': 'ollama-worker.py', 'old': '        if _rev_abort:\n            log(f"[worker] EARLY ABORT at iteration', 'new': '        if False:\n            log(f"[worker] EARLY ABORT at iteration', 'why': 'a tripped repeat-call guard does not end the run'}], [{'mut': 'ollama-worker.py', 'old': '        if (_rev_bounds and not tool_calls and (', 'new': '        if (False and not tool_calls and (', 'why': 'a review turn hitting the output cap is not a named exit'}], [{'mut': 'ollama-worker.py', 'old': '        if _rev_bounds and _rev_abort and not converged and not _cap.exists():', 'new': '        if False:', 'why': 'a capped review captures the runaway text, not the marker'}], [{'mut': 'ollama-worker.py', 'old': '        if len(_all) > _wr.LIST_FILES_MAX_ENTRIES:', 'new': '        if False:', 'why': 'list_files lists a huge directory'}]]),
    dict(id='sampladder', name='Sampling escalation ladder (A/B arm, DEFAULT OFF, declared in model_profiles.yaml): arm off leaves request bodies untouched; abort 1 -> one-turn temp>=0.8 + rep 1.2 + fresh seed, abort 2 -> presence 1.0 + thinking off only under the hybrid thinking arm, abort 3 -> sampling_ladder_exhausted; two aborts on the same failing check -> spec_defect_repeat_abort (spec path first); a new seed per take; the worker applies it per turn and feeds it the failing-check signature', tool='test-sampling-ladder.py', args=['--bin', '{bin}'], marker='SAMPLING_LADDER_TEST_OK', timeout=120, proofs=[[{'mut': 'worker_robust.py', 'old': '            same = self.by_check[self.last_check] >= 2', 'new': '            same = False', 'why': 'two aborts on the same failing check keep sampling instead of the spec-defect exit'}], [{'mut': 'worker_robust.py', 'old': '        if self.aborts >= 3:', 'new': '        if False:', 'why': 'a third abort never exits (the ladder loops forever)'}], [{'mut': 'worker_robust.py', 'old': '        self.pending = self.aborts\n        return ("step", self.aborts)', 'new': '        self.pending = 0\n        return ("step", self.aborts)', 'why': 'an abort never schedules the next-turn step'}], [{'mut': 'worker_robust.py', 'old': '        if not self.enabled:\n            return ("off", None)\n        self.aborts += 1', 'new': '        self.aborts += 1', 'why': 'a disabled ladder still counts aborts / exits'}], [{'mut': 'worker_robust.py', 'old': 'self.takes * 7919', 'new': '0', 'why': 'every retry reuses the same seed'}], [{'mut': 'worker_robust.py', 'old': '        if self.enabled:\n            self.aborts = 0\n', 'new': '        if self.enabled:\n            pass\n', 'why': 'a good turn no longer resets the consecutive abort count'}], [{'mut': 'worker_robust.py', 'old': '                    REASON_SPEC_DEFECT_REPEAT, REASON_LADDER_EXHAUSTED)', 'new': '                    )', 'why': 'the ladder exit reasons are not registered terminal reasons'}], [{'mut': 'model_profile.py', 'old': '    if sampling_arm_of(sampling_arm, data) != "ladder":\n        return {}\n    cfg', 'new': '    cfg', 'why': 'the ladder applies with the arm OFF'}], [{'mut': 'model_profile.py', 'old': '    if cfg.get("arm", "off") not in ("off", False):', 'new': '    if False:', 'why': 'a shipped arm=ladder passes check_profiles'}], [{'mut': 'model_profile.py', 'old': '        if not need or need == have:', 'new': '        if True:', 'why': 'step 2 turns thinking off outside the hybrid thinking arm'}], [{'mut': 'model_profile.py', 'old': 'v[_k] = max(float(v[_k])', 'new': 'v[_k] = min(float(v[_k])', 'why': 'the step lowers sampling values instead of raising them'}], [{'mut': 'model_profile.py', 'old': '            if _so.get("fresh_seed") and seed is not None:', 'new': '            if False:', 'why': 'no fresh seed on the wire'}], [{'mut': 'model_profile.py', 'old': '    if str(role or "author") not in (cfg.get("roles") or []):\n        return {}\n    st =', 'new': '    st =', 'why': 'roles outside sampling_escalation.roles are touched'}], [{'mut': 'model_profiles.yaml', 'old': '  arm: "off"   # quoted', 'new': '  arm: "ladder"   # quoted', 'why': 'the shipped profile has the ladder ON'}], [{'mut': 'ollama-worker.py', 'old': '        enabled=_mp.sampling_ladder_enabled(role), seed_base=int(time.time()) % 100000)', 'new': '        enabled=False, seed_base=int(time.time()) % 100000)', 'why': 'the worker never enables the ladder'}], [{'mut': 'ollama-worker.py', 'old': '        _turn_sstep, _turn_seed = _ladder.take()\n        if _turn_sstep:\n            _dispatch_metrics', 'new': '        _turn_sstep, _turn_seed = 0, None\n        if _turn_sstep:\n            _dispatch_metrics', 'why': 'the pending step is never applied to the next turn'}], [{'mut': 'ollama-worker.py', 'old': '            _lad = _ladder.on_abort("cutoff")', 'new': '            _lad = ("off", None)', 'why': 'an output-cap cut never advances the ladder'}], [{'mut': 'ollama-worker.py', 'old': '                _lad = _ladder.on_abort("reasoning_runaway")', 'new': '                _lad = ("off", None)', 'why': 'a reasoning runaway never advances the ladder'}], [{'mut': 'ollama-worker.py', 'old': '            _cap_cut_streak = 0\n            _ladder.on_ok()', 'new': '            _cap_cut_streak = 0', 'why': 'a tool-calling turn never resets the ladder'}], [{'mut': 'ollama-worker.py', 'old': '\n                _ladder.note_check(_verify_failure_signature(v_out) if not v_ok else None)\n                _did_work', 'new': '\n                _did_work', 'why': 'the task_complete-gate verify does not feed the failing-check signature'}], [{'mut': 'ollama-worker.py', 'old': '        think=think, sampling_step=sampling_step, seed=seed)', 'new': '        think=think)', 'why': 'the request builder never receives the ladder step / seed'}]]),
)
# Named hunk selections (resolved at proof time by content, so they survive edits):
# @SCOPELINE = the hunk(s) of the revert diff that touch the scope-line rule.
HUNK_TAGS = {"SCOPELINE": "HARNESS FILES ON THE SCOPE LINE"}
COVERAGE_PATH = REAL_HOME / ".ollama-dispatch" / "canary" / "coverage.json"


def _resolve_hunk_tag(spec: str, src_bin: Path) -> str:
    """FILE=OLD:NEW@TAG -> FILE=OLD:NEW@<hunk numbers whose text contains the tag>."""
    head, at, tag = spec.rpartition("@")
    if not at or tag not in HUNK_TAGS:
        return spec
    name, _, pair = head.partition("=")
    old, _, new = pair.partition(":")
    d = subprocess.run(["diff", "-u", str(_resolve_bak(src_bin, name, new)),
                        str(_resolve_bak(src_bin, name, old))], capture_output=True, text=True).stdout
    parts = re.split(r"(?m)^(?=@@ )", d)[1:]
    idx = [str(i) for i, h in enumerate(parts, 1) if HUNK_TAGS[tag] in h]
    return f"{head}@{','.join(idx) or '0'}"


def seam_home(base: Path, sid: str, bin_dir: Path) -> Path:
    """A private HOME per replay (its own ~/.ollama-dispatch) whose bin/ IS the
    sandbox bin, so replays never share state with each other or the e2e run."""
    h = base / f"seam-{sid}"
    h.mkdir(parents=True, exist_ok=True)
    if not (h / "bin").exists():
        (h / "bin").symlink_to(bin_dir)
    (h / ".ollama-dispatch").mkdir(exist_ok=True)
    return h


def run_seam(seam, bin_dir: Path, env: dict, home: Path):
    tool = Path(bin_dir) / seam["tool"]
    if not tool.exists():
        return False, f"{seam['tool']} missing", 0.0
    args = [a.replace("{bin}", str(bin_dir)) for a in seam.get("args", [])]
    e = dict(env, HOME=str(home))
    e.update({k: v.replace("{bin}", str(bin_dir)) for k, v in (seam.get("env") or {}).items()})
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, str(tool), *args], cwd=str(home), env=e,
                           capture_output=True, text=True, timeout=seam.get("timeout", 300))
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    ok = p.returncode == 0 and seam["marker"] in p.stdout
    fails = [ln.strip() for ln in p.stdout.splitlines() if re.match(r"\s*(FAIL|not ok)", ln)]
    note = " | ".join(fails)[:600] or (p.stdout + p.stderr)[-400:]
    return ok, note, time.time() - t0


class SeamRunner:
    """Runs every seam replay in the background (a small pool) while the e2e run
    goes; check_invariants() joins it."""

    def __init__(self, sb, workers=2):
        import concurrent.futures as cf
        self.pool = cf.ThreadPoolExecutor(max_workers=workers)
        base = sb.root / "seam-homes"
        self.futs = {}
        for s in sorted(SEAMS, key=lambda s: -s.get("timeout", 300)):   # long poles first
            if s.get("share"):
                continue          # same replay as another seam: reuse its result
            self.futs[s["id"]] = self.pool.submit(run_seam, s, sb.bin, sb.seam_env,
                                                  seam_home(base, s["id"], sb.bin))
        for s in SEAMS:
            if s.get("share"):
                self.futs[s["id"]] = self.futs[s["share"]]

    def join(self):
        out = {}
        for sid, f in self.futs.items():
            try:
                out[sid] = f.result(timeout=900)
            except Exception as e:
                out[sid] = (False, f"replay crashed: {e!r}"[:300], 0.0)
        self.pool.shutdown(wait=False)
        return out


def _proof_file(spec) -> str:
    if isinstance(spec, dict):
        return spec["mut"]
    if spec.startswith("swap:"):
        spec = spec[5:]
    return spec.partition("=")[0]


def apply_proof(bin_dir: Path, spec, src_bin: Path):
    """One step of a revert proof. Three kinds, strongest first:
      "FILE=OLD:NEW[@hunks|@TAG]"  un-apply the fix's own backup diff (apply_revert)
      "swap:FILE=BACKUP"           the whole pre-fix file (for tests built to take the
                                   .bak, e.g. --queue <bak>; later fixes are lost too,
                                   so pair it with proof_fail)
      {"mut": FILE, "old": s, "new": s, "why": ...}
                                   disable the fix at its core line, when later edits
                                   overlap the fix's hunks so the backup diff no longer
                                   applies (anchor must occur exactly once)
    Returns (status, note) like apply_revert."""
    _detach_symlink(bin_dir / _proof_file(spec))
    if isinstance(spec, dict):
        t = bin_dir / spec["mut"]
        txt = t.read_text()
        n = txt.count(spec["old"])
        if n != 1:
            return "partial", f"{spec['mut']}: mutation anchor found {n}x [PARTIAL]"
        t.write_text(txt.replace(spec["old"], spec["new"]))
        return "clean", f"{spec['mut']}: mutated ({spec.get('why', 'fix disabled')})"
    if spec.startswith("swap:"):
        name, _, bak = spec[5:].partition("=")
        b = _resolve_bak(src_bin, name, bak)
        if not b.is_file():
            return "partial", f"{name}: swap source {bak} missing [PARTIAL]"
        shutil.copy2(b, bin_dir / name)
        return "clean", f"{name} <- {b.name} (whole pre-fix file)"
    return apply_revert(bin_dir, _resolve_hunk_tag(spec, src_bin), src_bin)


def _seam_files(seam):
    files = {seam["tool"]}
    for proof in seam.get("proofs") or []:
        for spec in proof:
            files.add(_proof_file(spec))
    return sorted(files)


def seams_only(args) -> int:
    """--seams: just the replays, against a scratch copy of --bin (fast)."""
    src = Path(args.bin).resolve()
    root = Path(tempfile.mkdtemp(prefix="pipeline-canary-seams-")).resolve()
    home = root / "home"
    home.mkdir(parents=True)
    copy_bin(src, home / "bin")
    for rv in args.revert or []:
        print("  " + apply_revert(home / "bin", _resolve_hunk_tag(rv, src), src)[1])
    env = base_env(root, home, make_shims(root))
    sel = [s for s in SEAMS if not args.only or s["id"] in args.only]
    bad = 0
    for s in sel:
        ok, note, secs = run_seam(s, home / "bin", env, seam_home(root, s["id"], home / "bin"))
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  [seam {s['id']}] {s['name']} ({secs:.0f}s)"
              + ("" if ok else f"  -- {note[:300]}"), flush=True)
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    print(f"SEAMS {len(sel) - bad}/{len(sel)} green")
    return 1 if bad else 0


def prove(args) -> int:
    """--prove: for every seam, green on the unmodified copy AND red with each of its
    historical fixes un-applied. Records coverage.json for the live ~/bin."""
    src = Path(args.bin).resolve()
    root = Path(tempfile.mkdtemp(prefix="pipeline-canary-prove-")).resolve()
    home = root / "home"
    home.mkdir(parents=True)
    bin_dir = home / "bin"
    copy_bin(src, bin_dir)
    env = base_env(root, home, make_shims(root))
    sel = [s for s in SEAMS if not args.only or s["id"] in args.only]
    results = {}
    print(f"{'seam':15} {'now':5} {'revert proofs':40}")
    for s in sel:
        h = seam_home(root, s["id"], bin_dir)
        ok_now, note_now, _ = run_seam(s, bin_dir, env, h)
        reds = []
        for proof in s.get("proofs") or []:
            saved = {}
            notes, partial = [], False
            for spec in proof:
                name = _proof_file(spec)
                saved.setdefault(name, (bin_dir / name).read_bytes())
                st, n = apply_proof(bin_dir, spec, src)
                notes.append(n)
                partial |= st == "partial"
            ok_rev, note_rev, _ = run_seam(s, bin_dir, env, h)
            for name, b in saved.items():
                (bin_dir / name).write_bytes(b)
            _pf = s.get("proof_fail")
            right = (not _pf) or any(x in note_rev for x in ([_pf] if isinstance(_pf, str) else _pf))
            reds.append({"revert": proof, "applied": notes, "partial": partial,
                         "red": (not ok_rev) and right, "note": note_rev[:300]})
        proven = ok_now and bool(reds) and all(r["red"] and not r["partial"] for r in reds)
        results[s["id"]] = {"name": s["name"], "historical": bool(s.get("historical")),
                            "green_now": ok_now, "proofs": reds, "proven": proven,
                            "hashes": {f: _h(src / f) for f in _seam_files(s)},
                            "note_now": "" if ok_now else note_now[:300]}
        desc = ", ".join(("RED" if r["red"] else "green!") + ("(partial)" if r["partial"] else "")
                         for r in reds)
        print(f"{s['id']:15} {'ok' if ok_now else 'FAIL':5} {desc:40} {'PROVEN' if proven else 'NOT PROVEN'}",
              flush=True)
    shutil.rmtree(root, ignore_errors=True)
    official = src == (REAL_HOME / "bin").resolve() and not args.revert
    if official:
        prev = {}
        try:
            prev = json.loads(COVERAGE_PATH.read_text()).get("seams") or {}
        except Exception:
            pass
        prev.update(results)
        try:
            COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = COVERAGE_PATH.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "seams": prev}, indent=1))
            os.replace(tmp, COVERAGE_PATH)
        except OSError as e:
            print(f"  note: could not record coverage: {e}")
    n_ok = sum(1 for r in results.values() if r["proven"])
    print(f"PROVE {n_ok}/{len(results)} seams proven (green now, red on every revert)")
    return 0 if n_ok == len(results) else 1


def _h(p: Path):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def coverage_report():
    """(stale, line) for --status: N seam checks, M historical fixes proven."""
    n = len(SEAMS)
    hist = [s for s in SEAMS if s.get("historical")]
    try:
        cov = json.loads(COVERAGE_PATH.read_text())
    except Exception:
        return True, (f"COVERAGE: {n} seam checks; revert proofs NEVER RUN -- "
                      "python3 ~/bin/pipeline-canary.py --prove")
    seams = cov.get("seams") or {}
    proven, stale, missing = [], [], []
    for s in SEAMS:
        r = seams.get(s["id"])
        if not r:
            missing.append(s["id"])
            continue
        if not r.get("proven"):
            continue
        now = {f: _h(REAL_HOME / "bin" / f) for f in _seam_files(s)}
        if now != (r.get("hashes") or {}):
            stale.append(s["id"])     # the test or a guarded file changed since the proof
        proven.append(s["id"])
    hp = [s["id"] for s in hist if s["id"] in proven]
    age = int((time.time() - float(cov.get("ts") or 0)) // 60)
    line = (f"COVERAGE: {n} seam checks, {len(hp)}/{len(hist)} historical fixes proven "
            f"red-on-revert ({len(proven)}/{n} seams proven, last --prove {age} min ago)")
    unproven = [s["id"] for s in SEAMS if s["id"] not in proven and s["id"] not in missing]
    if unproven:
        line += f"; NOT proven: {', '.join(unproven)}"
    if missing:
        line += f"; never proven: {', '.join(missing)}"
    if stale:
        line += f"; re-prove (files changed): {', '.join(stale)}"
    return bool(unproven or missing), line


# ---------------------------------------------------------------------------
# e2e freshness: the last canary run against the LIVE ~/bin, so a pipeline edit
# with no canary run after it is visible ("e2e skipped") instead of silent.
# ---------------------------------------------------------------------------
PIPELINE_FILES = (
    "ollama-queue.py", "ollama-worker.py", "darkbloom_chat.py", "ollama-dispatch-auto",
    "ollama-dispatch-scaffold", "ollama-dispatch-plan", "ollama-dispatch-slice",
    "ollama-dispatch-preflight", "ollama-dispatch-draft", "gate-on-complete.py",
    "dispatch-self-heal.py", "code-review-agent.py", "handoff-emit.py",
    "escalation_index_janitor.py", "dispatch-escalation-watcher.py", "verify-relevance.py",
    "dispatch-ack-reconcile.py", "pipeline-canary.py")
STATUS_PATH = REAL_HOME / ".ollama-dispatch" / "canary" / "last.json"


def pipeline_hashes(bin_dir=None):
    b = Path(bin_dir or (REAL_HOME / "bin"))
    out = {}
    for f in PIPELINE_FILES:
        try:
            out[f] = hashlib.sha256((b / f).read_bytes()).hexdigest()[:16]
        except OSError:
            out[f] = None
    return out


def record_status(ok, bad, hashes, elapsed):
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        prev = {}
        try:
            prev = json.loads(STATUS_PATH.read_text())
        except Exception:
            pass
        doc = {"ts": time.time(), "ok": bool(ok), "failed": bad, "hashes": hashes,
               "elapsed_s": round(elapsed),
               "last_green": ({"ts": time.time(), "hashes": hashes} if ok
                              else prev.get("last_green"))}
        tmp = STATUS_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        os.replace(tmp, STATUS_PATH)
    except OSError as e:
        print(f"  note: could not record canary status: {e}")


def status_report():
    """(code, line): 0 GREEN, 1 FAIL (last run red), 2 STALE (pipeline files
    changed since the last green run, or never run)."""
    try:
        d = json.loads(STATUS_PATH.read_text())
    except Exception:
        return 2, "PIPELINE E2E: NEVER RUN -- run python3 ~/bin/pipeline-canary.py"
    now = pipeline_hashes()
    age = int((time.time() - float(d.get("ts") or 0)) // 60)
    if not d.get("ok"):
        return 1, (f"PIPELINE E2E: FAIL ({age} min ago): {', '.join(d.get('failed') or [])[:300]}"
                   " -- fix it, then re-run python3 ~/bin/pipeline-canary.py")
    green = (d.get("last_green") or {}).get("hashes") or {}
    changed = sorted(f for f in PIPELINE_FILES if now.get(f) != green.get(f))
    if changed:
        return 2, (f"PIPELINE E2E: SKIPPED -- {len(changed)} pipeline file(s) changed since the "
                   f"last green canary ({age} min ago): {', '.join(changed)}. "
                   "Run python3 ~/bin/pipeline-canary.py before calling the change done.")
    return 0, f"PIPELINE E2E: GREEN ({age} min ago, every pipeline file unchanged since)"


def prune_old_sandboxes(keep=4, min_age_s=3600):
    """Failed runs keep their sandbox for triage; keep only the newest few."""
    tmp = Path(tempfile.gettempdir()).resolve()
    dirs = sorted(tmp.glob("pipeline-canary-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs[keep:]:
        try:
            if time.time() - d.stat().st_mtime > min_age_s:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


SOAK_PATH = REAL_HOME / ".ollama-dispatch" / "canary" / "soak.json"
# SAFETY invariants must hold under ANY fault; the rest are liveness (the pipeline
# should recover from a transient fault, and a failure there is a finding too).
SAFETY_MARKS = ("FAILED while", "torn", "escaped the sandbox", "unattributable", "orphaned",
                "records consistent", "handoff rows consistent", "did not take the lane",
                "exactly one drove", "without a traceback", "fault-injector")


def _reap_sandbox(root):
    """SIGKILL every process whose command line names this sandbox root (and only
    those: other canaries / live drivers never contain it)."""
    root = str(root)
    if "pipeline-canary-" not in root:
        return
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    except Exception:
        return
    for line in ps.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and root in cmd and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def soak(a):
    """Run --soak N seeds, each a fresh sandboxed canary with its own fault schedule
    (no seam replays), and summarise which checks failed under which seeds."""
    seeds = list(range(a.soak_start, a.soak_start + a.soak))
    # A range that does not start at seed 1 gets its OWN file: two soaks over
    # different ranges (e.g. 1-25 and 26-50 run side by side) both wrote soak.json
    # after every seed, so each clobbered the other's results.
    soak_path = (SOAK_PATH if a.soak_start == 1 else
                 SOAK_PATH.with_name(f"soak-{seeds[0]}-{seeds[-1]}.json"))
    out = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "bin": a.bin,
           "runs": []}
    for sd in seeds:
        t0 = time.time()
        cmd = [sys.executable, str(Path(__file__).resolve()), "--seed", str(sd), "--json",
               "--no-seams", "--bin", a.bin, "--timeout", str(a.timeout)]
        outf = tempfile.TemporaryFile("w+")
        p = subprocess.Popen(cmd, stdout=outf, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
        try:
            p.wait(timeout=a.timeout + 600)
            outf.seek(0)
            txt = outf.read()
            last = txt.strip().splitlines()[-1] if txt.strip() else "{}"
            res = json.loads(last) if last.startswith("{") else {"ok": False, "failed": ["no json"]}
        except subprocess.TimeoutExpired:
            # a HUNG seed is a liveness failure; reap it AND its sandbox's daemon /
            # stubs / drivers (their own sessions) by the sandbox root it printed
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            outf.seek(0)
            m = re.search(r"^sandbox: (\S+)", outf.read(), re.M)
            if m:
                _reap_sandbox(m.group(1))
            res = {"ok": False, "failed": [f"seed hung > {a.timeout + 600}s (reaped)"],
                   "sandbox": m.group(1) if m else None}
        except Exception as e:
            res = {"ok": False, "failed": [f"harness: {type(e).__name__}"]}
        finally:
            outf.close()
        res = {k: res.get(k) for k in ("ok", "failed", "fault", "injected", "shapes", "sandbox")}
        res["seed"], res["secs"] = sd, round(time.time() - t0)
        out["runs"].append(res)
        print(f"seed {sd}: {'OK  ' if res.get('ok') else 'FAIL'} {res['secs']}s "
              f"injected={res.get('injected')} shapes={res.get('shapes')} "
              f"failed={res.get('failed')}", flush=True)
        try:
            soak_path.parent.mkdir(parents=True, exist_ok=True)
            soak_path.write_text(json.dumps(out, indent=1))
        except OSError:
            pass
    by = {}
    for r in out["runs"]:
        for f in r.get("failed") or []:
            by.setdefault(f, []).append(r["seed"])
    print(f"\n=== soak: {sum(1 for r in out['runs'] if r.get('ok'))}/{len(seeds)} seeds clean ===")
    for f, sds in sorted(by.items(), key=lambda kv: -len(kv[1])):
        kind = "SAFETY " if any(m in f for m in SAFETY_MARKS) else "liveness"
        print(f"  [{kind}] {len(sds):3d} seed(s) {sds[:12]}: {f}")
    out["summary"] = {f: sds for f, sds in by.items()}
    try:
        soak_path.write_text(json.dumps(out, indent=1))
    except OSError:
        pass
    return 0 if not by else 1


def main():
    os.environ.setdefault("CPU_LANE_LOG_OFF", "1")      # canary sandboxes must not pollute ~/.ollama-dispatch/cpu-lane.log
    if len(sys.argv) >= 4 and sys.argv[1] == "--draft":
        return draft(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 2 and sys.argv[1] == "--status":
        code, line = status_report()
        print(line)
        stale, cov = coverage_report()
        print(cov)
        return code if code else (2 if stale else 0)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bin", default=str(HERE))
    ap.add_argument("--swap", action="append", metavar="FILE=SRC")
    ap.add_argument("--revert", action="append", metavar="FILE=OLD:NEW",
                    help="un-apply the OLD->NEW change on top of the current FILE (revert proof)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--load", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-chaos", action="store_true")
    ap.add_argument("--no-decoy", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--no-422", action="store_true")
    ap.add_argument("--no-integrate-wait", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-seams", action="store_true", help="skip the seam replays")
    ap.add_argument("--seams", action="store_true",
                    help="run ONLY the seam replays (fast; honours --revert/--bin/--only)")
    ap.add_argument("--prove", action="store_true",
                    help="revert-prove every seam (green now, red with its fix un-applied); "
                         "records coverage.json for the live ~/bin")
    ap.add_argument("--only", action="append", metavar="SEAM_ID")
    ap.add_argument("--seed", type=int, default=None,
                    help="inject ONE seeded fault schedule (driver kill+resume, daemon restart, "
                         "Darkbloom restart timing, model failure shapes, chaos periods)")
    ap.add_argument("--soak", type=int, default=0, metavar="N",
                    help="run N seeded fault-injection runs (sandboxed) and summarise "
                         "invariant violations by seed; writes canary/soak.json")
    ap.add_argument("--soak-start", type=int, default=1)
    ap.add_argument("--hand-land", action="store_true",
                    help="land the staged chain with a plain git merge instead of --land-integration")
    a = ap.parse_args()
    if a.soak:
        return soak(a)
    if a.seams:
        return seams_only(a)
    if a.prove:
        return prove(a)
    t0 = time.time()
    official = (Path(a.bin).resolve() == (REAL_HOME / "bin").resolve() and not a.swap
                and not a.revert and a.seed is None and not any(getattr(a, k) for k in (
                    "no_chaos", "no_decoy", "no_restart", "no_422", "no_integrate_wait", "no_probe",
                    "no_seams")))
    # --hand-land is still the live code (only the landing step differs), so it records
    hashes = pipeline_hashes() if official else None
    R, sb = canary(a)
    bad = [c[0] for c in R.checks if not c[1]]
    print("\n=== pipeline canary ===")
    print(f"  {len(R.checks) - len(bad)}/{len(R.checks)} checks passed in {time.time() - t0:.0f}s")
    for b in bad:
        print(f"  FAILED: {b}")
    if a.keep or not R.ok:
        print(f"  sandbox kept: {sb.root}")
    else:
        shutil.rmtree(sb.root, ignore_errors=True)
    if official:
        # hashes taken at START: an edit made while the canary ran stays "skipped"
        record_status(R.ok, bad, hashes, time.time() - t0)
    prune_old_sandboxes()
    print("PIPELINE_CANARY_OK" if R.ok else "PIPELINE_CANARY_FAIL")
    if a.json:
        print(json.dumps({"ok": R.ok, "failed": bad, "sandbox": str(sb.root),
                          "fault": sb.fault.describe() if sb.fault else None,
                          "injected": (sb.injector.done if getattr(sb, "injector", None) else []),
                          "shapes": [r.get("shape") for r in sb.obs.find("fault-shape")],
                          "checks": [{"name": c[0], "ok": c[1], "note": c[2]} for c in R.checks]}))
    return 0 if R.ok else 1


if __name__ == "__main__":
    sys.exit(main())
