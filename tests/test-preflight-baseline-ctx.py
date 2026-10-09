#!/usr/bin/env python3
"""Regression test for the Rivian s1/s2 auto-authoring stall (2026-10-01):
"no progress for 3 consecutive refine rounds (verdict NO-GO, blockers
['baseline-clean', 'ctx-budget'] unchanged)".

Three defects, each asserted by BEHAVIOUR:
  1. ollama-dispatch-preflight check_baseline_clean NO-GO'd a TS repo that tracks
     tsconfig.tsbuildinfo (incremental tsc rewrites it) -> must skip *.tsbuildinfo
     like ollama-dispatch-scaffold, but still FAIL on a real dirty file.
  2. ollama-dispatch-auto refine_prompt read blocker["msg"]; preflight emits
     "message" -> every blocker reached the model as "<check>: None"/empty.
  3. The Darkbloom ctx ceiling fell back to 65536 (0.9.14 reports no window) while
     the task needed ~87069 -> derive it from KV/token (model config.json) x live
     usable memory (`darkbloom status`) / slot cap = 131072; and auto adopts
     preflight's ctx recommendation so the two estimates agree.

Run: python3 test-preflight-baseline-ctx.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PRE = Path(os.environ.get("PRE_SRC") or HERE / "ollama-dispatch-preflight")
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def sh(*cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


SAMPLE_STATUS = """darkbloom 0.9.14
Provider: darkbloom-mac16-9
Serving concurrency: Default operator cap 4; unknown profiles keep cap 4; model overrides: qwen3.5-9b=2, qwen3.6-35b-a3b-vl-mtp-mxfp8=4; higher widths require ...
Daemon: running (pid 71309, up 34m)
Model switch: serving; 3 unfinished request(s)
Warm models: Qwen3.5-9B, qwen3.6-35b-a3b-vl-mtp-mxfp8
Live load memory: 21.1 GB usable now (no eviction)
Requests served: 2  |  tokens: 1248
"""
QWEN36_CFG = {"text_config": {"num_hidden_layers": 40, "num_key_value_heads": 2, "head_dim": 256,
                              "hidden_size": 2048, "num_attention_heads": 16,
                              "max_position_embeddings": 262144, "full_attention_interval": 4,
                              "layer_types": ["linear_attention"] * 3 + ["full_attention"]} }
QWEN36_CFG["text_config"]["layer_types"] = (["linear_attention"] * 3 + ["full_attention"]) * 10


def main():
    # ---- 1. baseline-clean skips *.tsbuildinfo --------------------------------
    pre = load(PRE, "pf_t")
    repo = Path(tempfile.mkdtemp(prefix="pf-tsbi-"))
    sh("git", "init", "-q", cwd=repo)
    sh("git", "config", "user.email", "t@t", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    (repo / "tsconfig.tsbuildinfo").write_text('{"v":1}')
    (repo / "lib.ts").write_text("export const x = 1;\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-qm", "base", cwd=repo)
    (repo / "tsconfig.tsbuildinfo").write_text('{"v":2}')        # tsc --noEmit rewrote it

    def pf():
        p = pre.Preflight.__new__(pre.Preflight)
        p.a = SimpleNamespace(auto_seal=True)
        p.wt = repo
        p.results = []
        p.is_wt = False
        return p
    p = pf()
    st = p.check_baseline_clean()
    check("tracked tsbuildinfo rewritten by tsc -> baseline-clean PASS", st, pre.PASS)
    (repo / "lib.ts").write_text("export const x = 2;\n")
    p = pf()
    st = p.check_baseline_clean()
    check("a real dirty product file still FAILs", st, pre.FAIL)
    msg = p.results[-1].msg
    check("FAIL names the dirty file, not the tsbuildinfo", ("lib.ts" in msg, "tsbuildinfo" in msg), (True, False))
    blocker = p.results[-1].as_dict()

    # ---- 2. refine prompt carries the blocker message --------------------------
    auto = load(AUTO, "oda_t")
    ctxb = {"check": "ctx-budget", "status": "FAIL", "message": "task needs ~87069 ctx tokens, host ceiling is 65536",
            "detail": "task 9000B + named files 60000B", "fix": "split it"}
    rp = auto.refine_prompt("lib/rivian.ts", [], [blocker, ctxb], lang="typescript")
    check("refine prompt carries the baseline-clean message", "lib.ts" in rp and "already modified" in rp, True)
    check("refine prompt carries the ctx-budget message", "~87069 ctx tokens" in rp, True)
    check("refine prompt never says ': None'", ": None" in rp, False)
    check("refine prompt carries the blocker detail", "named files 60000B" in rp, True)
    empty = auto.refine_prompt("lib/rivian.ts", [], [{"check": "baseline-clean", "message": ""}], lang="typescript")
    check("no dirty file named -> no false 'allowed to change' claim", "allowed to change" in empty, False)

    # ---- 3. derived Darkbloom ctx ceiling --------------------------------------
    q = load(QUEUE, "q_t")
    st = q.parse_darkbloom_status(SAMPLE_STATUS)
    check("status: unfinished requests", st.get("unfinished"), 3)
    check("status: usable GB", st.get("usable_gb"), 21.1)
    check("status: per-model slot cap", q.darkbloom_slot_cap(st, "qwen3.6-35b-a3b-vl-mtp-mxfp8"), 4)
    check("status: 9B slot cap", q.darkbloom_slot_cap(st, "Qwen3.5-9B"), 2)
    check("status: garbage -> {}", q.parse_darkbloom_status("boom"), {})
    check("kv bytes/token = 2*10 full-attn layers*2 kv heads*256*2B", q.kv_bytes_per_token(QWEN36_CFG), 20480)
    check("derived ceiling at 21.1 GB / 4 slots = 131072", q.derive_ctx_ceiling(QWEN36_CFG, 21.1, 4), 131072)
    check("derived ceiling at 25.3 GB = 131072 (capped at top bucket)", q.derive_ctx_ceiling(QWEN36_CFG, 25.3, 4), 131072)
    check("derived ceiling at 6 GB shrinks", q.derive_ctx_ceiling(QWEN36_CFG, 6.0, 4), 49152)
    check("no memory figure -> None (fallback path)", q.derive_ctx_ceiling(QWEN36_CFG, None, 4), None)
    os.environ.pop("DARKBLOOM_CTX", None)
    got = q.darkbloom_ctx_ceiling("m", now=0, probe=lambda m: None, cache={},
                                  derive=lambda m: q.derive_ctx_ceiling(QWEN36_CFG, 21.1, 4))
    check("ceiling uses the derivation when the endpoint reports none", got, 131072)
    got = q.darkbloom_ctx_ceiling("m", now=0, probe=lambda m: None, cache={}, derive=lambda m: 49152)
    check("a low derivation is floored at the fallback", got, q.DARKBLOOM_CTX_FALLBACK)
    got = q.darkbloom_ctx_ceiling("m", now=0, probe=lambda m: None, cache={}, derive=lambda m: None)
    check("no derivation -> fallback", got, q.DARKBLOOM_CTX_FALLBACK)
    calls = []
    cache = {}
    run = lambda: (calls.append(1), SAMPLE_STATUS)[1]
    q.darkbloom_status(now=100, run=run, cache=cache)
    q.darkbloom_status(now=105, run=run, cache=cache)
    check("status cached ~10s", len(calls), 1)
    q.darkbloom_status(now=111, run=run, cache=cache)
    check("status re-read after TTL", len(calls), 2)
    check("status CLI failure degrades to {}", q.darkbloom_status(now=0, run=lambda: 1 / 0, cache={}), {})
    check("load line", q.darkbloom_load_line(st, "qwen3.6-35b-a3b-vl-mtp-mxfp8").startswith(
        f"Darkbloom {q.DARKBLOOM_LANE}: 3/4 slots busy"), True)
    check("load line degrades", q.darkbloom_load_line({}).endswith("status unavailable"), True)

    # ---- 4. auto adopts preflight's ctx recommendation ------------------------
    a = SimpleNamespace(num_ctx=65536, _num_ctx_computed=True)
    auto.adopt_preflight_ctx(a, {"ctx_recommended": 98304})
    check("computed num_ctx raised to preflight's recommendation", a.num_ctx, 98304)
    a = SimpleNamespace(num_ctx=65536, _num_ctx_computed=False)
    auto.adopt_preflight_ctx(a, {"ctx_recommended": 98304})
    check("explicit --num-ctx is never overridden", a.num_ctx, 65536)
    src = PRE.read_text()
    check("preflight JSON exposes ctx_recommended", '"ctx_recommended": self._ctx_recommended' in src, True)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("PRE_SRC", "tsbuildinfo skip removed", '            if path.endswith(".tsbuildinfo"):', '            if False:'),
    ("AUTO_SRC", "refine prompt back to b['msg']", '    return str(b.get("message") or b.get("msg") or "").strip()',
     '    return str(b.get("msg") or "").strip()'),
    ("QUEUE_SRC", "ceiling derivation removed", "    if not ctx and not os.environ.get(\"DARKBLOOM_CTX_NO_DERIVE\"):",
     "    if False:"),
    ("QUEUE_SRC", "linear-attention layers counted as KV", '        layers = sum(1 for x in lt if "linear" not in str(x)) or layers',
     '        pass'),
    ("AUTO_SRC", "auto ignores preflight ctx", "            and int(rec) > int(getattr(a, \"num_ctx\", 0) or 0)):",
     "            and False):"),
]


def revert_check():
    bad = 0
    srcs = {"PRE_SRC": PRE, "AUTO_SRC": AUTO, "QUEUE_SRC": QUEUE}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
