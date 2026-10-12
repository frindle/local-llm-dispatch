#!/usr/bin/env python3
"""BASELINE HYGIENE (2026-10-11, arr-webhook 8557454d853b / auto-fix r1 950dffa5697f): the queue's round seal committed the
model's partial target diff into HEAD, so later rounds measured "baseline" against base + the model's own work. Now:
  * the ORIGINAL clean base is recorded at the first seal (scaffold --seal-baseline and queue seal_prev_round_baseline),
    idempotent, in the git dir (outside the tree);
  * a seal that carries the declared target's diff is reported (BASE NOW CARRIES ...) and flagged at admission as
    BASE_POISONED_BY_SEAL (preflight spec-defect, harness_lint);
  * auto-harness-check's baseline reset uses clean_target_ref (the pre-poison commit), never the poisoned HEAD.
  test-orig-base-hygiene.py                # run -> ORIG_BASE_HYGIENE_OK
  test-orig-base-hygiene.py --revert-check # disable each rule in turn; every one must go RED
HG_SRC / OQ_SRC / SC_SRC / AU_SRC / PF_SRC / HL_SRC env override the files under test (used by --revert-check).
"""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = {k: Path(os.environ.get(f"{k}_SRC") or HERE / n) for k, n in
       (("HG", "dispatch_harness_gates.py"), ("OQ", "ollama-queue.py"), ("SC", "ollama-dispatch-scaffold"),
        ("AU", "ollama-dispatch-auto"), ("PF", "ollama-dispatch-preflight"), ("HL", "harness_lint.py"), ("HAG", "harness_audit_gates.py"))}
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:400]))
    if not ok:
        FAILS.append(name)


def git(cwd, *a):
    r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null", *a],
                       cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    sys.modules[name] = m
    ld.exec_module(m)
    return m


TARGET_BASE = "def f():\n    return 0\n"
TASK = "# TASK\n## Required change\nMake f() return 1 in `t.py`.\n\nOnly edit `t.py`.\n\n## Must contain\n\n- `return 1`\n"


def mkrepo(root, harness_sealed=True):
    wt = Path(root)
    wt.mkdir(parents=True)
    git(wt, "init", "-q")
    (wt / "t.py").write_text(TARGET_BASE)
    (wt / "other.py").write_text("x = 1\n")
    git(wt, "add", "t.py", "other.py")
    git(wt, "commit", "-qm", "base")
    base = git(wt, "rev-parse", "HEAD")
    (wt / "TASK.md").write_text(TASK)
    (wt / "verify.sh").write_text("grep -q 'return 1' t.py && echo VERIFY_OK || { echo 'FAIL: f'; exit 1; }\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "t.py", "creation_task": False}))
    git(wt, "add", "TASK.md", "verify.sh", ".dispatch-harness.json")
    git(wt, "commit", "-qm", "seal dispatch harness (clean launch baseline)")
    return wt, base, git(wt, "rev-parse", "HEAD")


def main():
    hg = load("hg_t", SRC["HG"])
    oq = load("oq_t", SRC["OQ"])
    sc = load("sc_t", SRC["SC"])
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # ---------------- recording
        wt, base, sealed_head = mkrepo(root / "a")
        check("no record before any seal", hg.read_orig_base(wt) == "")
        wtq = root / "q"
        wtq.mkdir()
        git(wtq, "init", "-q")
        (wtq / "t.py").write_text(TARGET_BASE)
        git(wtq, "add", "t.py")
        git(wtq, "commit", "-qm", "base")
        qbase = git(wtq, "rev-parse", "HEAD")
        (wtq / "TASK.md").write_text(TASK)
        (wtq / "verify.sh").write_text("echo hi\n")
        (wtq / ".dispatch-harness.json").write_text(json.dumps({"target": "t.py", "authored": ["TASK.md", "verify.sh"]}))
        rcq = sc.seal_baseline(wtq)
        qhead = git(wtq, "rev-parse", "HEAD")
        check("REAL scaffold seal_baseline(): seals the harness and records the original base at that seal",
              rcq == 0 and qhead != qbase and hg.read_orig_base(wtq) == qhead and (wtq / "t.py").read_text() == TARGET_BASE,
              (rcq, qhead, qbase, hg.read_orig_base(wtq)))
        hg.orig_base_path(wtq).unlink()
        rcq2 = sc.seal_baseline(wtq)
        check("REAL scaffold seal_baseline(): re-running on an already-sealed tree keeps the record", rcq2 == 0 and hg.read_orig_base(wtq) == qhead)
        sc._record_orig_base_at_seal(wt)
        check("scaffold --seal-baseline records HEAD (harness sealed, target untouched) as the original base",
              hg.read_orig_base(wt) == sealed_head, hg.read_orig_base(wt))
        check("the record lives in the git dir -- NOT in the worktree (no dirt, model cannot edit it)",
              not (wt / "dispatch-orig-base").exists() and "dispatch-orig-base" not in git(wt, "status", "--porcelain"))
        (wt / "other.py").write_text("x = 2\n")
        git(wt, "commit", "-qam", "later harness seal")
        sc._record_orig_base_at_seal(wt)
        check("recording is idempotent: a later seal NEVER overwrites the original base", hg.read_orig_base(wt) == sealed_head)
        check("explicit sha is honoured only on first record",
              hg.record_orig_base(wt, "deadbeef" * 5) == sealed_head)

        # ---------------- queue seal over a coding continuation with the model's partial target diff
        (wt / "t.py").write_text("def f():\n    x = 1\n    y = 2\n    return 0\n")      # partial work in the TARGET
        job = {"id": "950dffa5697f", "label": "arr-webhook [auto-fix r1]", "auto_fix_round": 1, "cwd": str(wt)}
        pre = git(wt, "rev-parse", "HEAD")
        res = oq.seal_prev_round_baseline(job)
        check("coding auto-fix seal still happens (Cause 1: a dirty launch tree = UNTRUSTED) and reports the target",
              res and "t.py" in res["sealed"] and res.get("target_sealed") == "t.py", res)
        check("...and reports the original clean base", res and res.get("orig_base") == sealed_head, res)
        check("...HEAD advanced (the model's diff is now IN HEAD) but the recorded original base did not move",
              git(wt, "rev-parse", "HEAD") != pre and hg.read_orig_base(wt) == sealed_head)
        fs = hg.base_poison_findings(wt)
        check("admission: BASE_POISONED_BY_SEAL names the seal, the lines, the original base and the clean ref",
              len(fs) == 1 and fs[0]["code"] == hg.BASE_POISONED_BY_SEAL and "+2/-0" in fs[0]["message"]
              and sealed_head[:12] in fs[0]["message"] and "clean_target_ref" in fs[0]["message"], fs)
        check("clean_target_ref is the commit BEFORE the first poisoning seal", hg.clean_target_ref(wt, "t.py") == pre,
              (hg.clean_target_ref(wt, "t.py"), pre))
        check("the clean ref's target is the original text (failure counts measured there are not understated)",
              git(wt, "show", f"{hg.clean_target_ref(wt, 't.py')}:t.py") + "\n" == TARGET_BASE)
        sd = hg.spec_defects(wt, target="t.py", creation=False, task_text=TASK)
        check("spec_defects (preflight admission) carries it", any(f["code"] == hg.BASE_POISONED_BY_SEAL for f in sd))
        (wt / "t.py").write_text("def f():\n    return 1\n")
        res2 = oq.seal_prev_round_baseline({**job, "id": "j2", "label": "arr-webhook [auto-fix r2]", "auto_fix_round": 2})
        check("a second seal still reports the SAME original base (never the poisoned HEAD)",
              res2 and res2.get("orig_base") == sealed_head and hg.read_orig_base(wt) == sealed_head, res2)
        fs2 = hg.base_poison_findings(wt)
        check("two poisoning seals -> still one finding, counts both", len(fs2) == 1 and "2 'auto: seal round baseline'" in fs2[0]["message"], fs2)

        # ---------------- gate E (revalidate at queue pop) must NOT park a continuation round over the poison it just made
        hag = load("hag_t", SRC["HAG"])
        wtr, _br, _hr = mkrepo(root / "r")
        (wtr / "refimpl.py").write_text("print('x')\n")
        (wtr / ".preflight-state.json").write_text(json.dumps({"freshness": hag.freshness_snapshot(wtr, "t.py")}))
        (wtr / "t.py").write_text("def f():\n    return 'partial'\n")
        oq.seal_prev_round_baseline({"id": "jr", "label": "x [auto-fix r1]", "auto_fix_round": 1, "cwd": str(wtr)})
        rv = hag.revalidate(wtr, verify_cmd="echo FAIL; exit 1")
        check("gate E revalidate after a continuation seal (HEAD + target blob drifted, base poisoned) is NOT a SPEC_DEFECT park",
              rv["status"] != "SPEC_DEFECT" and rv["drift"] and not any("BASE_POISONED" in str(f) for f in rv["findings"]), rv)
        check("...while the admission lint itself still sees that tree as poisoned", len(hg.base_poison_findings(wtr)) == 1)

        # ---------------- harness_lint (stage entry) surfaces it too
        hl = load("hl_t", SRC["HL"])
        check("harness_lint.run_lint(quick) reports it", any(f["code"] == hg.BASE_POISONED_BY_SEAL for f in hl.run_lint(wt, quick=True, target="t.py")))

        # ---------------- negatives / near-misses
        wt3, _b, _h = mkrepo(root / "b")
        (wt3 / "other.py").write_text("x = 99\n")                                   # only a NON-target file carried forward
        r3 = oq.seal_prev_round_baseline({"id": "j3", "label": "x [auto-fix r1]", "auto_fix_round": 1, "cwd": str(wt3)})
        check("near-miss: a seal that carries only NON-target files is not poison",
              r3 and not r3.get("target_sealed") and hg.base_poison_findings(wt3) == [], r3)
        wt4, _b4, _h4 = mkrepo(root / "c")
        (wt4 / "t.py").write_text("def f():\n    return 1\n")
        git(wt4, "commit", "-qam", "upstream: fix f (a real commit, refresh_stale_base style)")
        check("near-miss: a legitimate non-seal commit touching the target (upstream refresh) is never poison",
              hg.base_poison_findings(wt4) == [])
        wt5, _b5, _h5 = mkrepo(root / "d")
        (wt5 / "t.py").write_text("def f():\n    return 'solved while authoring'\n")
        r5 = oq.seal_prev_round_baseline({"id": "j5", "label": "auto-refine-arr-r1", "auto_fix_round": 0, "cwd": str(wt5)})
        check("near-miss: an AUTHORING round's target edit is reset to HEAD before the seal (earlier fix kept) -> no poison",
              r5 and r5.get("target_reset") == "t.py" and hg.base_poison_findings(wt5) == []
              and (wt5 / "t.py").read_text() == TARGET_BASE, r5)
        wt6, _b6, _h6 = mkrepo(root / "e")
        check("near-miss: clean worktree, no seals -> no finding; clean_target_ref is HEAD",
              hg.base_poison_findings(wt6) == [] and hg.clean_target_ref(wt6, "t.py") == "HEAD")
        check("near-miss: no target declared -> silent", hg.base_poison_findings(wt6, target=None) == [])
        # legacy worktree: poisoned long ago, never recorded
        wt7, base7, _h7 = mkrepo(root / "f")
        (wt7 / "t.py").write_text("def f():\n    return 'partial'\n")
        git(wt7, "commit", "-qam", "auto: seal round baseline before legacy [auto_fix_round=1]")
        check("legacy poisoned worktree with NO record: still detected from the seal commits",
              len(hg.base_poison_findings(wt7)) == 1)
        check("legacy: the first record taken now is the PRE-poison commit, not the poisoned HEAD",
              hg.record_orig_base(wt7) == hg.clean_target_ref(wt7, "t.py") != git(wt7, "rev-parse", "HEAD"))

        # ---------------- the baseline measurement consumer (auto-harness-check template)
        au = SRC["AU"].read_text()
        m = re.search(r"def _clean_ref\(_target\):.*?\n\n\n(?=_target_reset = None)", au, re.S)
        check("auto-harness-check template defines _clean_ref and resets the target to it", bool(m) and
              'git("checkout", _clean_ref(_t), "--", _t)' in au and 'git("checkout", "HEAD", "--", _t)' not in au)
        if m:
            ns = {"sys": sys, "Path": Path, "WT": wt}
            exec(m.group(0), ns)
            check("template _clean_ref: poisoned worktree -> the pre-poison commit", ns["_clean_ref"]("t.py") == hg.clean_target_ref(wt, "t.py"))
            ns["WT"] = wt6
            check("template _clean_ref: clean worktree -> HEAD (no behaviour change for healthy harnesses)", ns["_clean_ref"]("t.py") == "HEAD")
            ns["WT"] = root / "does-not-exist"
            check("template _clean_ref: fail-open to HEAD on any error", ns["_clean_ref"]("t.py") == "HEAD")

        # ---------------- real preflight admission (end to end)
        (wt / "refimpl.py").write_text("print('noop')\n")
        r = subprocess.run([sys.executable, str(SRC["PF"]), str(wt), "--refimpl-cmd", "python3 refimpl.py", "--no-relevance",
                            "--target", "t.py", "--json"], capture_output=True, text=True, timeout=300, env={**os.environ, "HOME": d})
        try:
            dd = json.loads(r.stdout)
        except ValueError:
            dd = {}
        rows = {c.get("id") or c.get("check"): c for c in (dd.get("checks") or dd.get("results") or [])}
        sdr = rows.get("spec-defect") or {}
        check("preflight: spec-defect FAILs a worktree whose base carries a sealed model diff",
              sdr.get("status") == "FAIL" and "sealed diff" in (sdr.get("message") or sdr.get("msg") or str(sdr)),
              (sdr, r.stderr[-300:]))
        rows_h = {}
        wt8, _b8, _h8 = mkrepo(root / "g")
        (wt8 / "refimpl.py").write_text("print('noop')\n")
        r8 = subprocess.run([sys.executable, str(SRC["PF"]), str(wt8), "--refimpl-cmd", "python3 refimpl.py", "--no-relevance",
                             "--target", "t.py", "--json"], capture_output=True, text=True, timeout=300, env={**os.environ, "HOME": d})
        try:
            rows_h = {c.get("id") or c.get("check"): c for c in (json.loads(r8.stdout).get("checks") or [])}
        except ValueError:
            pass
        check("preflight near-miss: a healthy sealed worktree's spec-defect check is not tripped by the poison rule",
              "sealed diff" not in json.dumps(rows_h.get("spec-defect") or {}), rows_h.get("spec-defect"))

    q = SRC["OQ"].read_text()
    check("queue launch log carries the BASE NOW CARRIES warning", "BASE NOW CARRIES the model's diff of" in q)
    print("ORIG_BASE_HYGIENE_OK" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTATIONS = [
    ("HG", "record overwrites an existing base", "    cur = read_orig_base(wt)\n    if cur:\n        return cur\n", "    cur = ''\n"),
    ("HG", "record stored inside the worktree", "    return Path(r.stdout.decode().strip()) / \"dispatch-orig-base\"", "    return Path(str(wt)) / \"dispatch-orig-base\""),
    ("HG", "poison finding never emitted", "    sc = seal_commits_touching(wt, target)\n    if not sc:\n        return []\n    orig = read_orig_base(wt)", "    return []\n    orig = read_orig_base(wt)"),
    ("HG", "any commit (not only seals) counts as poison", "        if not subj.startswith(SEAL_SUBJECT_PREFIX):\n            continue\n", ""),
    ("HG", "clean ref ignores the seal", "    par = _rev(wt, sc[0][0] + \"^\")\n    return par or \"HEAD\"", "    return \"HEAD\""),
    ("HG", "legacy record takes poisoned HEAD", "    sha = sha or _rev(wt, clean_target_ref(wt, _manifest(Path(wt)).get(\"target\")))", "    sha = sha or _rev(wt, \"HEAD\")"),
    ("HG", "poison not wired into spec_defects", "    out += base_poison_findings(wt, target)\n    return out", "    return out"),
    ("HL", "harness_lint (stage entry) does not run the poison lint", "                   lambda: list(_hg().base_poison_findings(wt, target)),\n", ""),
    ("HAG", "gate E revalidate parks a continuation round on the poison finding", '("ALREADY_SATISFIED", "BASE_POISONED_BY_SEAL")]', '("ALREADY_SATISFIED",)]'),
    ("OQ", "queue seal never records the original base", "        orig_base = _record_orig_base(cwd)\n", "        orig_base = ''\n"),
    ("OQ", "queue seal does not flag the target", "        target_sealed = _tgt_decl if (_tgt_decl and _tgt_decl in paths) else None", "        target_sealed = None"),
    ("OQ", "queue record overwrites", "        if f.is_file():\n            cur = f.read_text().split()", "        if False:\n            cur = f.read_text().split()"),
    ("SC", "scaffold seal does not record", "    print(\"sealed harness into baseline (clean launch tree): \"\n          + \", \".join(present))\n    _record_orig_base_at_seal(root)\n",
     "    print(\"sealed harness into baseline (clean launch tree): \"\n          + \", \".join(present))\n"),
    ("SC", "scaffold already-sealed path does not record", "            print(\"harness already sealed (no changes to commit).\")\n            _record_orig_base_at_seal(root)\n", "            print(\"harness already sealed (no changes to commit).\")\n"),
    ("AU", "baseline reset still uses poisoned HEAD", "git(\"checkout\", _clean_ref(_t), \"--\", _t)", "git(\"checkout\", \"HEAD\", \"--\", _t)"),
    ("AU", "clean ref helper returns HEAD always", "        return _hgc.clean_target_ref(WT, _target) or \"HEAD\"", "        return \"HEAD\""),
]


def revert_check():
    bad = 0
    for k, name, old, new in MUTATIONS:
        src = SRC[k].read_text()
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False, dir=str(HERE)) as fh:
            fh.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, f"{k}_SRC": fh.name}, capture_output=True, text=True, timeout=900)
        os.unlink(fh.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
