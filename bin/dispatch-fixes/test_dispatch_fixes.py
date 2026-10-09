#!/usr/bin/env python3
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from dispatch_fixes import (safe_label, capture_decision, annotate_results,
                            iters_flag, autocalc_iters, autocalc_num_ctx,
                            WORKER_DEFAULT_MAX_ITERS)

P=F=0
def ck(name, cond, detail=""):
    global P,F
    if cond: P+=1; print(f"  PASS  {name}")
    else:    F+=1; print(f"  FAIL  {name}  {detail}")

REAL_HF = "hf.co/unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M"

print("\n== 3a: label sanitisation ==")
# First: prove the CURRENT code actually crashes, so we know the bug is real
# and not a story about the bug. This is the exact expression at queue.py:683.
with tempfile.TemporaryDirectory() as td:
    d = pathlib.Path(td)
    try:
        (d / f"job7-{REAL_HF}.livelog").write_text("x")
        ck("baseline reproduces the crash", False, "unsanitised path unexpectedly WORKED")
    except (FileNotFoundError, NotADirectoryError, OSError) as e:
        ck("baseline reproduces the crash", True, "")
    # Now the fix must actually create a file on a real filesystem.
    p = d / f"job7-{safe_label(REAL_HF)}.livelog"
    p.write_text("x")
    ck("sanitised path writes for real", p.exists() and p.read_text() == "x")

ck("no separators survive", not any(c in safe_label(REAL_HF) for c in "/:\\ "))
ck("ordinary label unharmed", safe_label("review-ev-dashboard") == "review-ev-dashboard")
ck("empty -> job", safe_label("") == "job")
ck("dots-only -> job", safe_label("..") == "job")
ck("no traversal", ".." not in safe_label("../../etc/passwd"))
ck("length capped", len(safe_label("x" * 500)) <= 80)

print("\n== 3b: capture on a paused run ==")
w, txt, tag = capture_decision("REVIEW.md", False, "iteration budget exhausted", "the answer")
ck("PAUSED run now captures (was the bug)", w is True, f"got {tag}")
ck("partial is marked, not silent", "PARTIAL" in txt and "the answer" in txt)
w2, t2, tag2 = capture_decision("REVIEW.md", False, None, "clean answer")
ck("clean run captures unmarked", w2 and t2 == "clean answer" and tag2 == "capture=fallback")
ck("never clobbers model's file", capture_decision("R.md", True, None, "x")[0] is False)
ck("no text -> no empty file", capture_decision("R.md", False, "paused", "   ")[0] is False)
ck("no target -> noop", capture_decision(None, False, None, "x")[0] is False)

print("\n== 3c: engine health on the SUCCESS path ==")
data = {"unresponsive_engines": [["google cse", "CAPTCHA"], ["startpage", "timeout"],
                                 ["brave", "suspended"], ["bing", "suspended"]]}
out = annotate_results("- a hit\n  http://x", ["r"], data, 6)
ck("warns when engines are down mid-success", "COVERAGE WARNING" in out)
ck("states live engine count", "only 2 engine(s)" in out, out[-120:])
ck("blocks the false-negative inference", "NOT evidence" in out)
ck("silent when all healthy", annotate_results("body", ["r"], {}, 6) == "body")
ck("original results preserved", out.startswith("- a hit\n  http://x"))

print("\n== 2a: iteration single source of truth ==")
ck("unset omits the flag (worker default governs)", iters_flag(None) == [])
ck("explicit still honoured", iters_flag(45) == ["--max-iters", "45"])
ck("floor is the worker constant", autocalc_iters() == WORKER_DEFAULT_MAX_ITERS == 30)
ck("never below worker default", autocalc_iters(0, 0) >= 30)
ck("scales with files", autocalc_iters(n_files=4) == 50)
ck("verify checks counted", autocalc_iters(n_files=1, n_verify_checks=2) == 41)
ck("capped at 60", autocalc_iters(n_files=99, n_verify_checks=99) == 60)

print("\n== 2b: context sizing ==")
ck("small task -> small ctx", autocalc_num_ctx(20_000)[0] == 16384)
ck("mid task steps up", autocalc_num_ctx(120_000)[0] == 65536)
n, r = autocalc_num_ctx(2_000_000, largest_file_bytes=2_000_000)
ck("one oversized file REFUSES", n is None and "single file" in r, str(r))
n2, r2 = autocalc_num_ctx(900_000, largest_file_bytes=50_000)
ck("many files -> split advice", n2 is None and "split into" in r2, str(r2))
ck("headroom is real (not exact fit)", autocalc_num_ctx(30_000)[0] > 30_000/4)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
