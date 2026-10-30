# Card-bonus hardening: canary seam specs (2026-10-14)
pipeline-canary.py was NOT edited. Each seam = test file + marker + `--prove` reverts (exact old -> new). Each test also runs `--revert-check` itself (all mutants bite). Kill switches: MUTANT_SWEEP=off, VR_SWEEP_EQUIV_EXCLUDE=off, SPEC_CONFORMANCE=off|warn, DISPATCH_TRACE_TITLES=off, DISPATCH_TRACE_DECIMALS=off, AUTO_STAGE_GREEN_AFTER_FAIL=off, AUTO_INTENT_CHECK=off, MUTFB_NOOP_RETRIES (default 0 = off).

NOTE existing seam `mutfb` pins 'stop after the first no-new-kills round' incl. a no-edit round, so MUTFB_NOOP_RETRIES stays default 0. To enable the no-edit re-feed: change that seam's expectation (a no-edit round re-feeds once) then set the default to 1.

## seam `sweepequiv`
- test: `python3 ~/bin/test-mutant-sweep.py`, marker `ALL PASS`; revert-check `python3 ~/bin/test-mutant-sweep.py --revert-check`
- files: mutant_sweep.py / verify-relevance.py
- proofs (file: old -> new):
  - **equivalent sweep verdict not returned** in `mutant_sweep.py`
    - old: `'return {"status": "equivalent", "calls": calls,\n            "reason"'`
    - new: `'return {"status": "differs", "calls": calls, "witness": {},\n            "reason"'`
  - **a differing result is ignored** in `mutant_sweep.py`
    - old: `'    if (ra !== rb) {'`
    - new: `'    if (false) {'`
  - **impure modules are swept anyway** in `mutant_sweep.py`
    - old: `'if not ok:\n        return {"status": "unknown", "reason": f"not sweepable: {why}"}\n    ok, why = (py_eligible(mut_src) if is_py else js_eligible(mut_src))\n    if not ok:\n        return {"status": "unknown", "reason": f"mutant not sweepable: {why}"}'`
    - new: `'if False:\n        return {"status": "unknown"}'`
  - **helper-only line classified** in `mutant_sweep.py`
    - old: `'if enc is None:\n        return {"status": "unknown"'`
    - new: `'enc = enc or fns[0]\n    if False:\n        return {"status": "unknown"'`
  - **boundary literals not harvested** in `mutant_sweep.py`
    - old: `'        nums, strs = harvest_literals(orig_src + "\\n" + mut_src)'`
    - new: `'        nums, strs = [], []'`
  - **equivalents never excluded** in `verify-relevance.py`
    - old: `'            if _sweep_exclude_enabled():\n                excl.add(sv["id"])'`
    - new: `'            pass'`
  - **an open bank survivor no longer blocks the verdict flip** in `verify-relevance.py`
    - old: `'and not open_bank and not rec.get("untried_sites")'`
    - new: `'and not rec.get("untried_sites")'`
  - **witness not attached** in `verify-relevance.py`
    - old: `'            sv["witness"] = r.get("witness")'`
    - new: `'            pass'`

## seam `cardbonusfixes`
- test: `python3 ~/bin/test-auto-cardbonus-fixes.py`, marker `ALL PASS`; revert-check `python3 ~/bin/test-auto-cardbonus-fixes.py --revert-check`
- files: ollama-dispatch-auto
- proofs (file: old -> new):
  - **witness line dropped from the prompt** in `ollama-dispatch-auto`
    - old: `'            if _wl:\n                lines.append("  " + _wl)\n'`
    - new: `'            pass\n'`
  - **witness rule dropped** in `ollama-dispatch-auto`
    - old: `'            lines.append(SURVIVOR_WITNESS_RULE)\n'`
    - new: `'            pass\n'`
  - **no-edit round still stops** in `ollama-dispatch-auto`
    - old: `'if last_round_edited is False and noop_retries_left > 0:'`
    - new: `'if False:'`
  - **fingerprint ignores the fixture** in `ollama-dispatch-auto`
    - old: `'for n in (fixture_name_for(lang), "refimpl.py", "TASK.md"):'`
    - new: `'for n in ("refimpl.py", "TASK.md"):'`
  - **placeholder intent accepted** in `ollama-dispatch-auto`
    - old: `'    m = _INTENT_PLACEHOLDER_RE.search(t)\n    if m:'`
    - new: `'    m = None\n    if m:'`
  - **green-after-fail not accepted** in `ollama-dispatch-auto`
    - old: `'            if (not ok and stage != "refimpl" and stage_green_after_fail_enabled()\n                    and not wrote_nothing(before, after)):'`
    - new: `'            if False:'`
  - **retry of a green stage still dispatches** in `ollama-dispatch-auto`
    - old: `'            if attempt > 0 and stage != "refimpl" and stage_green_after_fail_enabled():'`
    - new: `'            if False:'`
  - **conformance block ignored** in `ollama-dispatch-auto`
    - old: `'        if v["status"] == "block":'`
    - new: `'        if False:'`

## seam `traceinvented`
- test: `python3 ~/bin/test-trace-invented-constants.py`, marker `ALL PASS`; revert-check `python3 ~/bin/test-trace-invented-constants.py --revert-check`
- files: harness_audit_gates.py / harness_admission.py / spec_conformance.py
- proofs (file: old -> new):
  - **test titles count as setup data again** in `harness_audit_gates.py`
    - old: `'and not (_titles and _TITLE_LINE_RE.match(l)))'`
    - new: `')'`
  - **decimals allowlisted again** in `harness_admission.py`
    - old: `'and re.fullmatch(r"-?\\d*\\.\\d+", tok)):\n        return False'`
    - new: `'and False):\n        return False'`
  - **thin-intent total-invention no longer blocks** in `spec_conformance.py`
    - old: `'if thin and len(named) >= 3 and len(inv) == len(named):'`
    - new: `'if False:'`
  - **a long intent can block** in `spec_conformance.py`
    - old: `'    thin = len((intent or "").split()) < 40'`
    - new: `'    thin = True'`
  - **invented constants not reported** in `spec_conformance.py`
    - old: `'if n not in refnums and n not in out'`
    - new: `'if False and n not in out'`
