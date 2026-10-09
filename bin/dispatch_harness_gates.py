#!/usr/bin/env python3
"""dispatch_harness_gates.py -- PURE harness/spec correctness gates (Phase 2, 2026-10-08).

WHY
---
Of 158 diagnosed escalations, 46% were unsatisfiable specs, 37% harness defects, 15%
already-satisfied and 3% model. Almost every one was a defect our own tooling could see
BEFORE a GPU slot was spent. This module holds the decidable checks as small pure
functions so preflight, the slicer, auto and the gate share ONE definition:

  placeholder_findings / harness_complete   a harness still carrying scaffold
        placeholders (TODO -- ..., assert.fail('SCAFFOLD_INCOMPLETE'), refimpl
        OLD = "TODO ...") or lacking a real must_contain literal / a refimpl anchor
        that exists in the target must never reach a coding dispatch
        (rt-egift-link-s1-s4-api-route).
  is_stub_text                             `export {};`, an empty module or a comment-only
        file is a BASELINE, not a modification (5b6b052149a7).
  refimpl_declared_test_paths              product test files the refimpl writes (they are
        sealed by queue "seal round baseline" commits and must not read as dirt).
  literal_findings                         every spec literal must be present in the target
        or derivable (named in the intent / introduced by the refimpl) (8a4941aa8521,
        a580c8793aa7).
  contract_status                          fail-before/pass-after read off preflight's checks.
  RefineBudget helpers                     bounded harness-refine counting (relevance
        survivors with VERIFY_OK go back to test authoring, not to a park).
  log_auto_decision                        one clearly logged record per automatic decision.

Run `python3 dispatch_harness_gates.py check <worktree>` for a standalone harness-complete
report (exit 1 on findings).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. harness-complete
# ---------------------------------------------------------------------------
# A placeholder in the SPEC: the scaffold's `TODO -- ...` fields and its marker.
_TASK_PLACEHOLDER_RE = re.compile(r"\bTODO\s*(?:--|:|—)|SCAFFOLD_INCOMPLETE|<PLACEHOLDER>|\bTBD\s*--")
# A placeholder in CODE (refimpl): the scaffold's OLD/NEW strings.
_REFIMPL_PLACEHOLDER_RE = re.compile(
    r"TODO\s*(?:--|:|—)|SCAFFOLD_INCOMPLETE|the exact text to replace|<PLACEHOLDER>")
# A placeholder in a FIXTURE: the guard case the generator emits. The marker word also
# appears in the fixture's header COMMENT and in verify.sh's own error strings, so only
# NON-comment fixture lines count.
_FIXTURE_PLACEHOLDER_RE = re.compile(
    r"SCAFFOLD_INCOMPLETE|SCAFFOLD:\s*cases not yet authored|TODO\s*--\s*(?:add|author|write)")
_FIXTURE_NAMES = ("verify.test.ts", "verify.test.mts", "test_fixture.py", "verify_impl.mts",
                  "verify_impl.mjs", "verify_impl.js", "Tests/verifyTests.swift",
                  "DispatchTests/VerifyImpl.cs")
_COMMENT_LINE_RE = re.compile(r"^\s*(?://|#|\*|/\*)")
_ANCHOR_NAME_RE = re.compile(r"^(?:OLD\w*|\w*_OLD|old_\w+|ANCHOR\w*|anchor\w*|\w*_anchor)$")


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _manifest(wt: Path) -> dict:
    try:
        d = json.loads((wt / ".dispatch-harness.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def must_contain_literals(task_text: str):
    """[(file_or_None, literal)] of REAL literals from the `## Must contain` block.

    Same grammar as preflight.must_contain_pairs (only `- ` items; `in <path>:` pins), but
    placeholder literals (containing TODO) are dropped: they are not literals."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "", re.S | re.I)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        if not re.match(r"\s*-\s", line):
            continue
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        f = fm.group(1) if fm else None
        for lm in re.finditer(r"`([^`\n]{1,200})`", line):
            lit = lm.group(1)
            if _TASK_PLACEHOLDER_RE.search(lit) or not lit.strip():
                continue
            out.append((f, lit))
    return out


def placeholder_findings(task_text: str = "", refimpl_text: str = "",
                         fixture_texts: dict | None = None):
    """PURE. [str] -- one finding per placeholder family found. Empty = no placeholders."""
    out = []
    t = [ln.strip() for ln in (task_text or "").splitlines() if _TASK_PLACEHOLDER_RE.search(ln)]
    if t:
        out.append(f"TASK.md still carries {len(t)} scaffold placeholder line(s), e.g. {t[0][:100]!r}")
    r = [ln.strip() for ln in (refimpl_text or "").splitlines()
         if not _COMMENT_LINE_RE.match(ln) and _REFIMPL_PLACEHOLDER_RE.search(ln)]
    if r:
        out.append(f"refimpl.py still carries scaffold placeholder(s), e.g. {r[0][:100]!r}")
    for name, txt in (fixture_texts or {}).items():
        f = _fixture_guard_lines(txt)
        if f:
            out.append(f"{name} still carries the scaffold guard case, e.g. {f[0][:100]!r} "
                       f"(no adversarial cases authored)")
    return out


_CASES_FLAG_OFF_RE = re.compile(r"\bCASES_AUTHORED\b[^\n=]*=\s*(?:false|False|0|\"0\")\b")
_COND_RE = re.compile(r"^\s*(?:\}?\s*else\s+)?(?:if|elif|while|unless)\b|^\s*\[\[?\s")


def _fixture_guard_lines(txt: str):
    """Lines of a fixture that are a LIVE scaffold guard. The generator leaves a conditional
    guard (`if (!CASES_AUTHORED) {{ ...SCAFFOLD_INCOMPLETE... }}`, `if checks < 3`) in a
    finished fixture on purpose; that is not a placeholder. A marker is live when (a) the
    CASES_AUTHORED flag is still off, or (b) the marker line is not under any condition."""
    lines = (txt or "").splitlines()
    if any(not _COMMENT_LINE_RE.match(ln) and _CASES_FLAG_OFF_RE.search(ln) for ln in lines):
        return [ln.strip() for ln in lines
                if not _COMMENT_LINE_RE.match(ln) and (_CASES_FLAG_OFF_RE.search(ln)
                                                       or _FIXTURE_PLACEHOLDER_RE.search(ln))] or ["CASES_AUTHORED off"]
    out = []
    for i, ln in enumerate(lines):
        if _COMMENT_LINE_RE.match(ln) or not _FIXTURE_PLACEHOLDER_RE.search(ln):
            continue
        prev = [x for x in lines[max(0, i - 4):i] if x.strip()][-3:]
        if not any(_COND_RE.match(x) or "if " in x for x in prev):
            out.append(ln.strip())
    return out


def refimpl_anchors(refimpl_text: str):
    """[str] literal anchor strings the refimpl searches for (module-level `OLD = \"..\"`,
    `old_x = ..`, `ANCHOR = ..`). Parsed with ast; unparseable -> []."""
    try:
        tree = ast.parse(refimpl_text or "")
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tg in node.targets:
                if isinstance(tg, ast.Name) and _ANCHOR_NAME_RE.match(tg.id) \
                        and node.value.value.strip():
                    out.append(node.value.value)
    return out


def _path_constants(refimpl_text: str):
    try:
        tree = ast.parse(refimpl_text or "")
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if 0 < len(v) < 200 and not re.search(r"\s", v) and re.search(r"\.\w{1,6}$", v):
                out.append(v)
    return out


def _tracked_files(wt: Path):
    try:
        p = subprocess.run(["git", "-C", str(wt), "ls-files"], capture_output=True, text=True,
                           timeout=30)
        return [x for x in p.stdout.splitlines() if x] if p.returncode == 0 else []
    except Exception:
        return []


def refimpl_declared_test_paths(wt: Path):
    """Tracked TEST files (name contains .test./_test./test_) that refimpl.py names. The
    refimpl CREATES these; a queue 'seal round baseline' commit then tracks the previous
    attempt's version, and the refimpl's rewrite reads as a dirty tracked file."""
    consts = _path_constants(_read(wt / "refimpl.py"))
    if not consts:
        return []
    tracked = _tracked_files(wt)
    out = []
    for f in tracked:
        if not re.search(r"(?:\.test\.|\.spec\.|_test\.|(?:^|/)test_)", f):
            continue
        if any(f == c or f.endswith("/" + c) or c.endswith("/" + f) or Path(f).name == c
               for c in consts):
            out.append(f)
    return out


def refimpl_anchor_findings(wt: Path, target: str | None):
    """[str] anchors the refimpl needs that exist in NONE of the files it can touch.

    Candidate files: the target plus every tracked file named (by path or basename) by a
    path-like string constant in refimpl.py. Skipped for creation tasks (the refimpl writes
    the whole target) and when the refimpl declares no literal anchor."""
    man = _manifest(wt)
    if man.get("creation_task"):
        return []
    ref = _read(wt / "refimpl.py")
    anchors = refimpl_anchors(ref)
    if not anchors:
        return []
    texts = []
    if target:
        texts.append(_read(wt / target))
    consts = _path_constants(ref)
    if consts:
        for f in _tracked_files(wt):
            if any(f == c or f.endswith("/" + c) or Path(f).name == Path(c).name for c in consts):
                texts.append(_read(wt / f))
    blob = "\n".join(texts)
    out = []
    for a in anchors:
        if a not in blob:
            out.append(f"refimpl anchor {a[:80]!r} occurs in none of the files the refimpl edits "
                       f"(target {target!r}) -- 'assert OLD in t' can never pass")
    return out


def harness_complete(wt, target: str | None = None):
    """Whole static harness-complete decision for a worktree.

    Returns (ok, [findings]). Findings (each a blocker):
      * placeholders in TASK.md / refimpl.py / the fixture;
      * no REAL `## Must contain` literal (needs >= 1 non-TODO literal);
      * a refimpl anchor that is absent from every file it edits."""
    wt = Path(wt)
    man = _manifest(wt)
    target = target or man.get("target")
    fixtures = {}
    names = list(_FIXTURE_NAMES)
    fx = man.get("fixture")
    if isinstance(fx, str) and fx and fx not in names:
        names.append(fx)
    for n in names:
        p = wt / n
        if p.is_file():
            fixtures[n] = _read(p)
    task = _read(wt / "TASK.md")
    findings = []
    if not task.strip():
        findings.append("TASK.md is missing or empty")
    findings += placeholder_findings(task, _read(wt / "refimpl.py"), fixtures)
    if task.strip() and not must_contain_literals(task):
        findings.append("TASK.md has no REAL `## Must contain` literal (>= 1 non-placeholder "
                        "backtick literal in a `- ` bullet is required)")
    if not (wt / "refimpl.py").is_file() and not (wt / "fix.patch").is_file():
        findings.append("no refimpl.py / fix.patch: the task has never been shown satisfiable")
    findings += refimpl_anchor_findings(wt, target)
    return (not findings), findings


# ---------------------------------------------------------------------------
# 3. stubs
# ---------------------------------------------------------------------------
_STUB_BODIES = {"", "export{};", "export{}", "export{};export{};", "module.exports={};",
                "module.exports={}", "exportdefault{};", "'usestrict';", '"usestrict";',
                "pass", "package{}"}


def _strip_comments(text: str) -> str:
    t = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    t = re.sub(r"(?m)^\s*(?://|#).*$", "", t)
    t = re.sub(r'\A\s*(?:"""|\'\'\').*?(?:"""|\'\'\')', "", t, flags=re.S)
    t = re.sub(r"(?m)\s//.*$", "", t) if "export" in t else t
    return t


def is_stub_text(text: str) -> bool:
    """True when `text` has no behaviour: empty, whitespace/comment only, or only an
    `export {};` / `module.exports = {};` / `pass` body. These are what a scaffold or a
    creation task leaves as a target, and they count as BASELINE."""
    body = re.sub(r"\s+", "", _strip_comments(text))
    if body in _STUB_BODIES:
        return True
    return len(text or "") <= 600 and "implement per TASK.md" in (text or "") and len(body) < 40


# ---------------------------------------------------------------------------
# 4. spec satisfiability
# ---------------------------------------------------------------------------
def count_occurrences(lit: str, text: str) -> int:
    """Exact count, plus the opposite-quote spelling of a fully-quoted literal."""
    n = (text or "").count(lit)
    if len(lit) >= 2 and lit[0] in "\"'" and lit[-1] == lit[0] and lit[0] not in lit[1:-1]:
        alt = "'" if lit[0] == '"' else '"'
        n += (text or "").count(alt + lit[1:-1] + alt)
    return n


def literal_findings(lits, target_text: str, spec_text: str = "", introduced_text: str = ""):
    """PURE. Spec literals nobody can satisfy: [(lit, why)].

    A literal is SATISFIABLE when it (a) occurs in the target already, (b) is introduced by
    the reference impl, or (c) is NAMED in the spec text (intent / title / verify_shape /
    interface) the author works from -- so the author can know to write it. A literal that is
    in none of the three has 0 occurrences anywhere the author can see: unsatisfiable by
    construction (typo, wrong file, stale identifier)."""
    out = []
    for lit in lits or []:
        lit = str(lit)
        if not lit.strip():
            continue
        if count_occurrences(lit, target_text) or count_occurrences(lit, introduced_text) \
                or count_occurrences(lit, spec_text):
            continue
        out.append((lit, "0 occurrences in the target, not introduced by the refimpl, and not "
                         "named anywhere in the slice's intent/title/verify_shape"))
    return out


# ---------------------------------------------------------------------------
# 2. fail-before / pass-after contract
# ---------------------------------------------------------------------------
def contract_status(checks):
    """(status, why) over preflight check dicts/objects ({id,status}).

    PASS only when the verify FAILED on the untouched baseline (`baseline-fails` PASS) and
    PASSED with the refimpl applied (`refimpl-passes` PASS). FAIL when either is FAIL.
    UNPROVEN when either was never measured (waived refimpl, blocked earlier)."""
    by = {}
    for c in checks or []:
        cid = (c.get("id") or c.get("check")) if isinstance(c, dict) else getattr(c, "id", None)
        st = c.get("status") if isinstance(c, dict) else getattr(c, "status", None)
        by[cid] = st
    b, r = by.get("baseline-fails"), by.get("refimpl-passes")
    if b == "FAIL" or r == "FAIL":
        bad = [n for n, s in (("baseline-fails", b), ("refimpl-passes", r)) if s == "FAIL"]
        return "FAIL", "fail-before/pass-after broken: " + ", ".join(bad)
    if b == "PASS" and r == "PASS":
        return "PASS", "verify FAILS on the untouched baseline and PASSES with the refimpl"
    missing = [n for n, s in (("baseline-fails", b), ("refimpl-passes", r)) if s != "PASS"]
    return "UNPROVEN", "never measured: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# 5. bounded refine budget
# ---------------------------------------------------------------------------
HARNESS_REFINE_CAP = 2


def refine_budget(count, cap: int = HARNESS_REFINE_CAP):
    """('refine'|'respec', n_next): below the cap another harness refine; at the cap re-spec."""
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    return ("refine", n + 1) if n < cap else ("respec", n)


# ---------------------------------------------------------------------------
# 7. auto-decision log
# ---------------------------------------------------------------------------
def decisions_path() -> Path:
    home = os.environ.get("OLLAMA_DISPATCH_HOME")
    base = Path(home) if home else Path.home() / ".ollama-dispatch"
    return base / "auto-decisions.jsonl"


def log_auto_decision(kind: str, label: str, decision: str, detail: str = "", path=None,
                      **extra) -> dict:
    """Append one JSON line recording an automatic decision that used to be a human
    checkpoint. Never raises. Returns the record."""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind,
           "label": label, "decision": decision, "detail": str(detail)[:600], **extra}
    try:
        p = Path(path) if path else decisions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def main(argv=None):
    av = list(sys.argv[1:] if argv is None else argv)
    if len(av) == 2 and av[0] == "check":
        ok, fs = harness_complete(av[1])
        for f in fs:
            print("FINDING:", f)
        print("HARNESS COMPLETE" if ok else "HARNESS INCOMPLETE")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
