#!/usr/bin/env python3
"""canary-staleness-check.py -- the canary's own alarm (pipeline-hardening design 4.6).

The canary is only worth anything while it is current and green. This hourly job (launchd,
Penn installs it; see LaunchAgents/com.penn.canary-staleness-check.plist) reads the canary's
bookkeeping and tells Penn through notify-penn.py when it is not.

RED  (notify now, dedupe-key canary-<class>, repeats every 6 h while true)
  e2e         the last default canary run failed
  anchor-lost a revert proof's anchor no longer occurs exactly once (the guarded code moved/vanished)
  selftest    the queue --self-test seam (queueselftest) is red now
  unproven    a seam is missing from coverage.json (never proven) more than 1 h after it was first seen
  prove-age   the last FULL --prove is older than 26 h
AMBER (one daily digest, dedupe-key canary-amber)
  stale       seams need a re-prove / green re-run and the change is older than --stale-hours (2)
  e2e-skipped pipeline files changed since the last green e2e canary for more than --stale-hours
  unmirrored  live ~/bin pipeline dependency files differ from machine-config HEAD

  canary-staleness-check.py [--stale-hours 2] [--dry-run] [--json] [--no-selftest] [--bin DIR]

Always writes ~/.ollama-dispatch/canary/staleness.json (the session panel can show it).
CANARY_STALE_ALARM=0 disables the whole job. Exit 0 = nothing RED, 1 = RED present.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE_DIR = HOME / ".ollama-dispatch" / "canary"
OUT_PATH = STATE_DIR / "staleness.json"
STATE_PATH = STATE_DIR / "staleness-state.json"
MC_REPO = HOME / "Desktop" / "GitHub Projects" / "machine-config"
PROVE_MAX_AGE_S = 26 * 3600
UNPROVEN_GRACE_S = 3600
RED_REPEAT_S = 6 * 3600
AMBER_REPEAT_S = 24 * 3600


def load_canary(bin_dir: Path):
    spec = importlib.util.spec_from_file_location("pipeline_canary_mod", str(bin_dir / "pipeline-canary.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_canary_mod"] = m
    spec.loader.exec_module(m)
    return m


def classify(inp: dict) -> dict:
    """Pure function: inputs -> {"red": [(class, text)], "amber": [(class, text)]}.

    inp keys: now, stale_hours, e2e (last.json dict or None), analysis (canary analyze_coverage),
    selftest_ok (True/False/None), first_seen {sid: ts}, full_prove_ts, changed_age_s (age in
    seconds of the OLDEST changed dependency, None = nothing changed), e2e_stale_age_s (same for
    the e2e file set), unmirrored [file]."""
    now = inp["now"]
    red, amber = [], []
    e2e = inp.get("e2e")
    if e2e is not None and not e2e.get("ok", True):
        red.append(("e2e", "last pipeline canary run FAILED: " + ", ".join(e2e.get("failed") or ["?"])[:300]))
    an = inp.get("analysis") or {}
    if an.get("anchor_lost"):
        red.append(("anchor-lost", "revert-proof anchor lost (guarded code moved/deleted): "
                    + ", ".join(sorted(an["anchor_lost"]))[:300]))
    if inp.get("selftest_ok") is False:
        red.append(("selftest", "queue --self-test seam (queueselftest) is RED"))
    fs = inp.get("first_seen") or {}
    old_missing = [s for s in an.get("missing") or [] if now - float(fs.get(s, now)) > UNPROVEN_GRACE_S]
    if old_missing:
        red.append(("unproven", "seams never proven (not in coverage.json) for > 1 h: "
                    + ", ".join(sorted(old_missing))[:300]))
    fp = inp.get("full_prove_ts")
    if fp is None or now - float(fp) > PROVE_MAX_AGE_S:
        age = "never" if fp is None else f"{(now - float(fp)) / 3600:.0f} h ago"
        red.append(("prove-age", f"last FULL --prove: {age} (limit 26 h): python3 ~/bin/pipeline-canary.py --prove"))
    sh = float(inp.get("stale_hours", 2)) * 3600
    stale = sorted(set(an.get("reprove") or {}) | set(an.get("recheck") or {}) | set(an.get("unproven") or []))
    ca = inp.get("changed_age_s")
    if stale and ca is not None and ca > sh:
        amber.append(("stale", f"{len(stale)} seam(s) stale for {ca / 3600:.1f} h (re-prove {len(an.get('reprove') or {})}, "
                      f"green re-run {len(an.get('recheck') or {})}); run: python3 ~/bin/pipeline-canary.py --affected"))
    ea = inp.get("e2e_stale_age_s")
    if ea is not None and ea > sh:
        amber.append(("e2e-skipped", f"pipeline files changed {ea / 3600:.1f} h ago and no green e2e canary since"))
    um = inp.get("unmirrored") or []
    if um:
        amber.append(("unmirrored", f"{len(um)} live ~/bin pipeline file(s) differ from machine-config HEAD: "
                      + ", ".join(um[:8])))
    return {"red": red, "amber": amber}


_NOISE_MARKERS = (".bak", ".retired-", ".severed-", ".orig", ".rej", ".swp", ".tmp", "-backup", ".pre-")
_STATE_SUFFIXES = (".json", ".jsonl", ".log", ".lock", ".pid", ".state", ".db", ".sqlite")


def is_noise(name: str, path=None) -> bool:
    """True for files the unmirrored check must ignore: dotfiles (runtime state, .DS_Store),
    backups / retired / severed copies, __pycache__ / .pyc, runtime state (*.json, *.log, ...),
    zero-byte stray files, and symlinks (their content is owned by the
    repo they point into, not machine-config)."""
    base = name.rsplit("/", 1)[-1]
    if base.startswith(".") or "__pycache__" in name.split("/") or base.endswith(".pyc"):
        return True
    if any(m in base for m in _NOISE_MARKERS):
        return True
    if base.endswith(_STATE_SUFFIXES):
        return True
    if path is not None:
        pp = Path(path)
        try:
            return pp.is_symlink() or pp.stat().st_size == 0
        except OSError:
            return False
    return False


def _git_head_bytes(path: str):
    r = subprocess.run(["git", "-C", str(MC_REPO), "show", f"HEAD:{path}"], capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def gather(cn, bin_dir: Path, run_selftest: bool, now: float) -> dict:
    cov = cn.load_coverage()
    an = cn.analyze_coverage(bin_dir, cov)
    try:
        e2e = json.loads(cn.STATUS_PATH.read_text())
    except Exception:
        e2e = None
    # age of the oldest changed dependency (vs what each proof / green run recorded)
    changed = set()
    for sid in list(an["reprove"]) + list(an["recheck"]):
        r = (cov.get("seams") or {}).get(sid) or {}
        for f, h in {**(r.get("hashes") or {}), **(r.get("deps") or {})}.items():
            if cn._h(bin_dir / f) != h:
                changed.add(f)
    mt = []
    for f in changed:
        try:
            mt.append((bin_dir / f).stat().st_mtime)
        except OSError:
            pass
    changed_age = (now - min(mt)) if mt else (0 if (an["reprove"] or an["recheck"]) else None)
    e2e_age = None
    if e2e is not None and e2e.get("ok"):
        green = (e2e.get("last_green") or {}).get("hashes") or {}
        em = []
        for f, h in cn.pipeline_hashes(bin_dir).items():
            if green.get(f) is not None and green.get(f) != h:      # a file newly added to the list is not an edit
                try:
                    em.append((bin_dir / f).stat().st_mtime)
                except OSError:
                    pass
        e2e_age = (now - min(em)) if em else None
    selftest_ok = None
    if run_selftest:
        try:
            p = subprocess.run([sys.executable, str(bin_dir / "pipeline-canary.py"), "--seams",
                                "--only", "queueselftest", "--bin", str(bin_dir), "--jobs", "1"],
                               capture_output=True, text=True, timeout=900)
            selftest_ok = p.returncode == 0 and "SEAMS 1/1 green" in p.stdout
        except subprocess.TimeoutExpired:
            selftest_ok = False
    try:
        st = json.loads(STATE_PATH.read_text())
    except Exception:
        st = {}
    fs = st.get("first_seen") or {}
    for sid in an["missing"]:
        fs.setdefault(sid, now)
    for sid in list(fs):
        if sid not in an["missing"]:
            fs.pop(sid)           # proven (or removed): forget
    st["first_seen"] = fs
    um = []
    if MC_REPO.is_dir():
        for f in cn.list_files():
            p = bin_dir / f
            if not p.is_file() or is_noise(f, p):
                continue
            head = _git_head_bytes(f"bin/{f}")
            if head is None or head != p.read_bytes():
                um.append(f)
    return {"now": now, "e2e": e2e, "analysis": an, "selftest_ok": selftest_ok, "first_seen": fs,
            "full_prove_ts": cov.get("full_prove_ts"), "changed_age_s": changed_age,
            "e2e_stale_age_s": e2e_age, "unmirrored": sorted(um), "_state": st}


def deliver(res: dict, notify, now: float) -> list:
    sent = []
    for cls, text in res["red"]:
        notify(f"Pipeline canary RED: {cls}", text, f"canary-{cls}", RED_REPEAT_S)
        sent.append(("red", cls))
    if res["amber"]:
        notify("Pipeline canary digest", "\n".join(f"{c}: {t}" for c, t in res["amber"]), "canary-amber",
               AMBER_REPEAT_S)
        sent.append(("amber", "digest"))
    return sent


def _notify(title, msg, key, dedupe_s):
    try:
        subprocess.run([sys.executable, str(HERE / "notify-penn.py"), title, msg[:900],
                        "--dedupe-key", key, "--dedupe-s", str(dedupe_s)], timeout=60,
                       capture_output=True)
    except Exception:       # noqa: BLE001  an alarm must never crash on its own delivery
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stale-hours", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-selftest", action="store_true")
    ap.add_argument("--bin", default=str(HOME / "bin"))
    a = ap.parse_args(argv)
    if os.environ.get("CANARY_STALE_ALARM", "1") == "0":
        print("CANARY_STALE_ALARM=0: disabled")
        return 0
    bin_dir = Path(a.bin).resolve()
    now = time.time()
    cn = load_canary(bin_dir)
    inp = gather(cn, bin_dir, not a.no_selftest, now)
    st = inp.pop("_state")
    inp["stale_hours"] = a.stale_hours
    res = classify(inp)
    out = {"ts": now, "red": res["red"], "amber": res["amber"],
           "stale_hours": a.stale_hours}
    if not a.dry_run:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(out, indent=1))
            STATE_PATH.write_text(json.dumps(st))
        except OSError:
            pass
        deliver(res, _notify, now)
    for c, t in res["red"]:
        print(f"RED   {c}: {t}")
    for c, t in res["amber"]:
        print(f"AMBER {c}: {t}")
    if not res["red"] and not res["amber"]:
        print("canary current and green")
    if a.json:
        print(json.dumps(out))
    return 1 if res["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
