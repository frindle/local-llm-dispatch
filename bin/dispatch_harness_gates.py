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
  spec_defects / format_spec_defects       PRE-DISPATCH CONTRADICTION GATE (2026-10-09): a
        spec that contradicts the BASE commit (creation target already exists, every
        literal already present and the verify already green, refimpl anchor matching
        nothing) is a SPEC_DEFECT -- no model can satisfy it, so no GPU job is enqueued.
  classify_baseline_red                    assertion-failure vs crash/import/syntax at the
        baseline: only an assertion (or, for a creation/new-symbol task, the spec-named
        missing module/export) counts as 'fails before'.
  failure_signature                        stable fingerprint of a failure (first assertion
        line + failing case names) for the abandon-after-3-identical-failures rule.
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
    t = by.get("refimpl-tests")      # "no x->F": EVERY fixture case must pass under the refimpl
    if b == "FAIL" or r == "FAIL" or t == "FAIL":
        bad = [n for n, s in (("baseline-fails", b), ("refimpl-passes", r),
                              ("refimpl-tests", t)) if s == "FAIL"]
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
# 8. PRE-DISPATCH CONTRADICTION GATE (2026-10-09, smoke-order-mismatch)
# ---------------------------------------------------------------------------
# The replay task's INTENT said "create lib/payoutMismatch.ts" but the file ALREADY EXISTED at
# the base commit (in a later-evolved form). No refimpl can satisfy "create X" over a real X;
# the author model spiralled (~120K-char turns) and ~3 authoring jobs burned GPU. Every fact
# needed to see that is in git at the base commit, so this is decided BEFORE any job exists.
SPEC_DEFECT_PREFIX = "SPEC_DEFECT: "
CREATION_TARGET_EXISTS = "CREATION_TARGET_EXISTS"
ALREADY_SATISFIED = "ALREADY_SATISFIED"
REFIMPL_ANCHOR_UNMATCHED = "REFIMPL_ANCHOR_UNMATCHED"

_PATH_EXT = r"(?:tsx|ts|jsx|js|mjs|cjs|py|swift|sh|cs|go|rs|kt|java|css|html|json|sql|prisma)"
# `create [a|the] [new] [file|module|...] <path>` -- the path must follow the verb directly
# (plus filler words), so "create a test for lib/foo.ts" does NOT name foo.ts as created.
_CREATE_PATH_RE = re.compile(
    r"\b(?:create[sd]?|creating|add(?:s|ed|ing)?\s+a\s+new\s+file|new\s+file)\s+"
    r"(?:(?:a|an|the|new)\s+)*(?:(?:file|module|script|target|helper|component|test\s+file)\s+)?"
    r"(?:called\s+|named\s+)?['\"`]?((?:[\w.\[\]-]+/)*[\w.\[\]-]+\.%s)(?![\w/])" % _PATH_EXT, re.I)
_MODIFY_WORDS = (r"modif(?:y|ies|ied|ying)|extend(?:s|ed|ing)?|updat(?:e|es|ed|ing)|edit(?:s|ed|ing)?|"
                 r"rewrit(?:e|es|ing)|rewritten|overwrit(?:e|es|ing)|replac(?:e|es|ed|ing)|"
                 r"refactor(?:s|ed|ing)?|amend(?:s|ed|ing)?|existing|already\s+exists?")


def creation_paths_in_text(text: str):
    """[path] files the INTENT/TASK text says to CREATE (`create lib/x.ts ...`)."""
    out = []
    for m in _CREATE_PATH_RE.finditer(text or ""):
        p = m.group(1).strip("'\"`.,;:)")
        if p and p not in out:
            out.append(p)
    return out


def intent_allows_existing(text: str, path: str) -> bool:
    """True when the text EXPLICITLY says to modify/extend/update `path` (or that it already
    exists): a modify word within ~60 chars before the path, or 'exists' within ~60 after."""
    t = text or ""
    q = re.escape(path)
    return bool(re.search(r"\b(?:%s)\b[^.\n]{0,60}%s" % (_MODIFY_WORDS, q), t, re.I)
                or re.search(r"%s[^.\n]{0,60}\b(?:already\s+exists?|exists\s+already|is\s+already|"
                             r"(?:to\s+be\s+)?(?:modified|extended|updated|rewritten|replaced))\b"
                             % q, t, re.I))


def _git_run(wt, *args):
    try:
        return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, timeout=60)
    except Exception:
        return None


def blob_at(wt, path: str, ref: str = "HEAD"):
    """text of `path` at commit `ref` (git cat-file -e / show), or None when absent there."""
    p = path or ""
    while p.startswith("./"):
        p = p[2:]
    e = _git_run(wt, "cat-file", "-e", f"{ref}:{p}")
    if e is None or e.returncode != 0:
        return None
    r = _git_run(wt, "show", f"{ref}:{p}")
    return r.stdout.decode("utf-8", "replace") if r is not None and r.returncode == 0 else None


def _required_change_text(task_text: str) -> str:
    """The section the INTENT lives in; falls back to the whole text."""
    m = re.search(r"##+\s*Required change[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "", re.S | re.I)
    return m.group(1) if m else (task_text or "")


def _finding(code, reason, path, message):
    return {"code": code, "reason": reason, "path": path, "message": message}


def creation_target_findings(wt, target, creation: bool, intent_text: str, ref: str = "HEAD"):
    """[finding] -- (a) a file the task CREATES (the creation-task target, plus every path the
    intent says `create ...`) that already exists, as a real implementation, at `ref`.
    A stub at base (what a creation scaffold seeds and seals) is a baseline, not a defect.
    The INTENT saying modify/extend/update/'already exists' for that path exempts it."""
    paths = []
    if creation and target:
        paths.append(str(target))
    for p in creation_paths_in_text(intent_text):
        if p not in paths:
            paths.append(p)
    out = []
    for p in paths:
        body = blob_at(wt, p, ref)
        if body is None or is_stub_text(body):
            continue
        if intent_allows_existing(intent_text, p):
            continue
        out.append(_finding(
            CREATION_TARGET_EXISTS, "creation target exists at base", p,
            f"the task CREATES {p!r} but it already exists at base {ref} as a real "
            f"implementation ({len(body)} bytes, not a stub). No reference impl can create a "
            f"file that is there, and the author model spirals trying (smoke-order-mismatch). "
            f"Re-spec: make the intent modify/extend {p!r} (say so explicitly), or drop the "
            f"slice/task if the file already does what it asks."))
    return out


def already_satisfied_findings(wt, task_text: str, target, verify_green, ref: str = "HEAD"):
    """[finding] -- (b) every REAL `## Must contain` literal is ALREADY present in the file it
    is pinned to (default: the target) at `ref` AND the verify already passes there.
    `verify_green` is the measured baseline verdict (True = VERIFY_OK on the untouched tree);
    None/False -> no finding (a literal that is present proves nothing without a green verify)."""
    if verify_green is not True:
        return []
    pairs = must_contain_literals(task_text)
    if not pairs:
        return []
    for f, lit in pairs:
        body = blob_at(wt, f or target or "", ref) if (f or target) else None
        if body is None or count_occurrences(lit, body) == 0:
            return []
    return [_finding(
        ALREADY_SATISFIED, "already satisfied at base", target or "",
        f"all {len(pairs)} 'Must contain' literal(s) are already present at base {ref} and the "
        f"verify is already GREEN on the untouched tree: there is nothing left to build. "
        f"Skip the task (the property already holds) or restate it so it names something "
        f"the base lacks.")]


def refimpl_base_anchor_findings(wt, target, refimpl_text: str, ref: str = "HEAD"):
    """[finding] -- (c) refimpl OLD anchors that match NOTHING in the base content of the
    files the refimpl edits (target + tracked files its path constants name). Reads `ref`,
    not the working copy, so a clobbered/edited tree cannot hide a dead anchor. (The
    working-tree variant, refimpl_anchor_findings, skips creation tasks; this one does not,
    because a creation task over an existing file still runs `assert OLD in t`.)"""
    anchors = refimpl_anchors(refimpl_text)
    if not anchors:
        return []
    texts = []
    if target:
        b = blob_at(wt, target, ref)
        if b is not None:
            texts.append(b)
    consts = _path_constants(refimpl_text)
    if consts:
        r = _git_run(wt, "ls-tree", "-r", "--name-only", ref)
        files = r.stdout.decode("utf-8", "replace").splitlines() if r is not None and r.returncode == 0 else []
        for f in files:
            if any(f == c or f.endswith("/" + c) or Path(f).name == Path(c).name for c in consts):
                b = blob_at(wt, f, ref)
                if b is not None:
                    texts.append(b)
    blob = "\n".join(texts)
    return [_finding(REFIMPL_ANCHOR_UNMATCHED, "refimpl anchor matches nothing at base", target or "",
                     f"refimpl anchor {a[:80]!r} occurs in none of the base files the refimpl "
                     f"edits (target {target!r} @ {ref}) -- 'assert OLD in t' can never pass")
            for a in anchors if a not in blob]


# ---------------------------------------------------------------------------
# LITERAL LINT (2026-10-09, rt-walmart-cancel-import)
# ---------------------------------------------------------------------------
# A `## Must contain` literal that the base file spells slightly differently
# (`normalize(orderNumber)` vs the real `normalize(r.orderNumber)` /
# `normalize(s.orderNumber)`) can never be satisfied by a refimpl that edits the real
# call sites, and the authoring model burns whole rounds chasing it. Three pure gates:
#   (a) literal_near_miss / literal_near_miss_findings -- at authoring (s1) and on stage
#       entry: a literal ABSENT from the file at HEAD whose nearest existing line equals it
#       up to INSERTED/DROPPED characters (a qualifier like `r.`) is a spec defect, with a
#       "did you mean" message. Substitutions (`= true` vs `= false`) are NOT near-misses:
#       that is exactly the shape of a legitimate new literal.
#   (b) refimpl_literal_gap_findings -- with the refimpl applied, every literal must be
#       present; otherwise the stage failure is a spec defect, not nonconvergence.
#   (c) missing_literals_in_text / repeat_missing_literals -- two consecutive failed rounds
#       carrying the IDENTICAL non-empty missing-literal set: re-spec/park, no third round.
LITERAL_NEAR_MISS = "LITERAL_NEAR_MISS"
REFIMPL_LITERAL_ABSENT = "REFIMPL_LITERAL_ABSENT"
REPEAT_MISSING_LITERALS = "REPEAT_MISSING_LITERALS"
_NEAR_MISS_MIN_LEN = 8
_NEAR_MISS_MIN_RATIO = 0.8
_NEAR_MISS_MAX_EDIT = 16        # inserted/dropped chars tolerated between literal and line


def literal_near_miss(lit: str, body: str):
    """PURE. (line_no, line_text, window) of the existing line that contains `lit` up to a
    pure insertion/deletion of characters (never a substitution), else None. A literal that
    occurs verbatim is not a near miss. Short literals (<8 chars) never qualify: a short
    token is too likely to be a coincidental subsequence."""
    import difflib
    lit = str(lit or "")
    if len(lit.strip()) < _NEAR_MISS_MIN_LEN or not body or count_occurrences(lit, body):
        return None
    best = None
    for i, line in enumerate(body.splitlines(), 1):
        if len(line) < _NEAR_MISS_MIN_LEN // 2 or len(line) > 4000:
            continue
        # cheap prefilter: the literal's first and last significant char must occur
        if lit[0] not in line or lit[-1] not in line:
            continue
        sm = difflib.SequenceMatcher(None, lit, line, autojunk=False)
        # candidate windows start where the literal's opening run matches
        for a0 in sorted({b.b for b in sm.get_matching_blocks() if b.size >= 3})[:12]:
            win = line[a0:a0 + len(lit) + _NEAR_MISS_MAX_EDIT]
            w = difflib.SequenceMatcher(None, lit, win, autojunk=False)
            ops = [o for o in w.get_opcodes() if o[0] != "equal"]
            if not ops or any(o[0] == "replace" for o in ops):
                continue
            # trailing text of the window beyond the last match is not part of the miss
            last_eq = max((o[4] for o in w.get_opcodes() if o[0] == "equal"), default=0)
            first_eq = min((o[3] for o in w.get_opcodes() if o[0] == "equal"), default=0)
            used = win[first_eq:last_eq]
            if lit[0] != used[:1] or lit[-1] != used[-1:]:
                continue
            ed = sum(max(o[2] - o[1], o[4] - o[3]) for o in w.get_opcodes()
                     if o[0] in ("insert", "delete") and o[3] >= first_eq and o[4] <= last_eq)
            if ed == 0 or ed > _NEAR_MISS_MAX_EDIT:
                continue
            ratio = difflib.SequenceMatcher(None, lit, used, autojunk=False).ratio()
            if ratio < _NEAR_MISS_MIN_RATIO:
                continue
            # the literal must be an ordered subsequence of the used window (insert-only)
            it = iter(used)
            if not all(ch in it for ch in lit):
                if not (len(used) < len(lit)):
                    continue
            cand = (ratio, i, line.strip(), used)
            if best is None or cand[0] > best[0]:
                best = cand
    if best is None:
        return None
    return best[1], best[2], best[3]


def literal_near_miss_findings(wt, task_text: str, target, refimpl_text: str = "", ref: str = "HEAD"):
    """[finding] -- (a) every REAL `## Must contain` literal absent from its file at `ref`
    whose nearest existing line is a near miss. A literal the refimpl text itself spells
    (it introduces it on purpose) is skipped."""
    out = []
    for f, lit in must_contain_literals(task_text):
        path = f or target or ""
        if not path:
            continue
        body = blob_at(wt, path, ref)
        if body is None or count_occurrences(lit, body):
            continue
        if refimpl_text and lit in refimpl_text:
            continue
        nm = literal_near_miss(lit, body)
        if nm is None:
            continue
        ln, line, win = nm
        out.append(_finding(
            LITERAL_NEAR_MISS, "Must-contain literal near-misses the base code", path,
            f"`{lit}` occurs 0 times in {path} @ {ref}, but line {ln} has `{win}` -- did you "
            f"mean `{win}`? Fix the literal in TASK.md (the real call sites are spelled that "
            f"way) or drop it; no refimpl that edits the real code can add the "
            f"misspelled form. Line {ln}: {line[:140]}"))
    return out


def refimpl_literal_gap_findings(pairs, texts_by_file, default_text: str = ""):
    """[finding] -- (b) literals absent from the refimpl-APPLIED tree. `pairs` =
    must_contain_literals(); `texts_by_file` {path: applied text}; unpinned literals are
    checked against `default_text`."""
    gap = []
    for f, lit in pairs or []:
        body = texts_by_file.get(f, "") if f else default_text
        if not count_occurrences(lit, body or ""):
            gap.append((f, lit))
    if not gap:
        return []
    names = ", ".join(repr(l) for _f, l in gap[:6])
    return [_finding(
        REFIMPL_LITERAL_ABSENT, "Must-contain literal absent after the refimpl is applied", "TASK.md",
        f"{len(gap)} literal(s) are absent from the refimpl-applied tree: {names}. The spec and "
        f"the reference impl disagree; another model round cannot fix a literal the refimpl "
        f"never writes -- correct the spec (or the refimpl) deterministically.")]


_MISSING_LIT_RE = re.compile(r"MISSING literal in (\S+) \([^\n]*?\):\s*>>>(.*?)<<<")


def missing_literals_in_text(text: str):
    """PURE. frozenset of (file, literal) from check_literals.py's `MISSING literal in <file>
    (... >>>LIT<<<)` lines in a self-check / worker log."""
    return frozenset((m.group(1), m.group(2)) for m in _MISSING_LIT_RE.finditer(text or ""))


def repeat_missing_literals(prev, cur):
    """PURE. True when two consecutive failed rounds carry the identical NON-EMPTY missing-literal
    set: the same literal(s) defeated a whole model round twice -- stop spending rounds."""
    return bool(prev) and bool(cur) and frozenset(map(tuple, prev)) == frozenset(map(tuple, cur))


def format_repeat_missing(cur) -> str:
    names = ", ".join(repr(l) for _f, l in sorted(cur)[:6])
    return (f"{SPEC_DEFECT_PREFIX}Must-contain literal(s) never satisfied: two consecutive "
            f"rounds failed on the identical missing-literal set ({names}) -- re-spec or park; "
            f"another model round will not change it")


def spec_defects(wt, target=None, creation=None, intent_text="", task_text="", refimpl_text=None,
                 verify_green=None, ref: str = "HEAD"):
    """THE CONTRADICTION GATE. [finding] where each finding is
    {code, reason, path, message}; empty = the spec does not contradict the base.

    Static (no execution): (a) CREATION_TARGET_EXISTS, (c) REFIMPL_ANCHOR_UNMATCHED (only
    when a refimpl is given AND the task is a creation task -- edit tasks' anchors are
    harness-complete's job). Needs the measured verdict: (b) ALREADY_SATISFIED.
    `creation=None` reads the worktree manifest; `intent_text` defaults to TASK.md's
    `## Required change`."""
    wt = Path(wt)
    man = _manifest(wt)
    task = task_text or _read(wt / "TASK.md")
    if creation is None:
        creation = bool(man.get("creation_task"))
    target = target or man.get("target")
    intent = intent_text or _required_change_text(task)
    out = list(base_drift_findings(wt))
    out += creation_target_findings(wt, target, creation, intent, ref)
    if refimpl_text is None:
        refimpl_text = _read(wt / "refimpl.py")
    if creation and refimpl_text:
        out += refimpl_base_anchor_findings(wt, target, refimpl_text, ref)
    out += already_satisfied_findings(wt, task, target, verify_green, ref)
    out += literal_near_miss_findings(wt, task, target, refimpl_text or "", ref)
    return out


def format_spec_defects(findings) -> str:
    """One precise line a parked slice / log carries: `SPEC_DEFECT: <reason>: <path> -- <msg>`."""
    if not findings:
        return ""
    return "; ".join(f"{SPEC_DEFECT_PREFIX}{f['reason']}: {f['path']} -- {f['message']}"
                     for f in findings)


# ---------------------------------------------------------------------------
# 9. BASELINE-RED CLASSIFICATION (review steal #2)
# ---------------------------------------------------------------------------
# RED at the baseline must be red for the RIGHT reason. A verify that crashes on an import /
# syntax / unrelated TypeError "fails" for a reason that has nothing to do with the property,
# and would also fail for the refimpl-less model forever. What counts as red:
#   * an ASSERTION failure (AssertionError / ERR_ASSERTION / a verify.sh `FAIL:` line)  -> yes;
#   * a missing module / missing export / `x is not a function` -> yes ONLY when the task
#     CREATES the thing (a creation task, whose stub exports nothing) or the missing name is
#     NAMED in the spec text (the feature symbol the task adds);
#   * SyntaxError / TS compile error / ReferenceError / TypeError / ENOENT / command not found
#     / any other crash, or a missing name the spec never mentions                      -> NO;
#   * output with no recognisable signal at all (stderr discarded, bespoke runner)     -> unclassified, accepted
#     (never fail closed on text we cannot read).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ASSERT_RE = re.compile(r"AssertionError|ERR_ASSERTION|^\s*E\s+assert\b|^\s*(?:AssertionError|assertion failed)",
                        re.M | re.I)
_FAIL_LINE_RE = re.compile(r"^\s*FAIL(?:ED)?\s*[:\-]\s*(.+)$", re.M)
_FAIL_LINE_ENV = re.compile(r"npm |node_modules|install|bootstrap|not on PATH|PATH|tsc\b|prisma|"
                            r"command not found|No such file|cannot find|timed out", re.I)
_MISSING_EXPORT_RES = (
    re.compile(r"does not provide an export named ['\"`]?([\w$]+)"),
    re.compile(r"cannot import name ['\"`]?([\w$]+)", re.I),
    re.compile(r"has no attribute ['\"`]?([\w$]+)", re.I),
    re.compile(r"([\w$.]+) is not a function"),
    re.compile(r"([\w$.]+) is not (?:a constructor|callable|iterable)"),
    re.compile(r"TS(?:2305|2724|2614|2339)\b.*?['\"`]([\w$]+)['\"`]"),
    re.compile(r"No matching export in .*? for import ['\"`]([\w$]+)"),
)
_MISSING_MODULE_RES = (
    re.compile(r"ModuleNotFoundError: No module named ['\"`]([^'\"`]+)"),
    re.compile(r"Cannot find (?:module|package) ['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"ERR_MODULE_NOT_FOUND.*?['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"TS2307\b.*?['\"`]([^'\"`]+)['\"`]"),
    re.compile(r"ImportError: No module named ['\"`]?([\w.]+)"),
    re.compile(r"MODULE_NOT_FOUND"),
)
_BAD_CRASH_RES = (
    (re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b"), "syntax error"),
    (re.compile(r"\berror TS\d{4}\b"), "TypeScript compile error"),
    (re.compile(r"\bReferenceError\b|\bNameError\b"), "reference error"),
    (re.compile(r"\bTypeError\b|\bRangeError\b|\bKeyError\b|\bIndexError\b|\bValueError\b|"
                r"\bAttributeError\b|\bZeroDivisionError\b"), "runtime error"),
    # NB: a bare "Traceback (most recent call last)" header is NOT a signal -- the exception
    # line under it is, and it is classified above (missing symbol / assertion / error class).
    (re.compile(r"\bENOENT\b|command not found|Segmentation fault|UnhandledPromiseRejection"), "crash"),
)


def _ident_in(name: str, spec_text: str) -> bool:
    base = (name or "").split(".")[-1]
    base = re.sub(r"^.*/", "", base)
    base = re.sub(r"\.\w{1,5}$", "", base) if "/" in (name or "") else base
    return bool(base) and bool(re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(base), spec_text or ""))


def classify_baseline_red(out: str, creating: bool = False, spec_text: str = ""):
    """PURE. Classify a non-zero baseline verify run.

    Returns {"ok": bool, "kind": "assertion"|"expected-missing"|"crash"|"unclassified",
             "signals": [str]}. ok=False means the red is for the WRONG reason and must not
    count as 'fails before' (see the policy block above)."""
    t = _ANSI.sub("", out or "")
    if _ASSERT_RE.search(t):
        return {"ok": True, "kind": "assertion", "signals": ["assertion failure"]}
    for m in _FAIL_LINE_RE.finditer(t):
        if not _FAIL_LINE_ENV.search(m.group(1)):
            return {"ok": True, "kind": "assertion", "signals": ["verify FAIL: " + m.group(1).strip()[:80]]}
    signals, good, bad = [], [], []
    for ln in t.splitlines():
        hit = False
        for rx in _MISSING_EXPORT_RES:
            m = rx.search(ln)
            if m:
                name = m.group(1)
                (good if creating or _ident_in(name, spec_text) else bad).append(
                    f"missing symbol {name!r}" + ("" if creating else " (not named in the spec)"))
                hit = True
                break
        if hit:
            continue
        for rx in _MISSING_MODULE_RES:
            m = rx.search(ln)
            if m:
                name = m.group(1) if m.groups() else "?"
                (good if creating or _ident_in(name, spec_text) else bad).append(
                    f"missing module {name!r}" + ("" if creating else " (not named in the spec)"))
                hit = True
                break
        if hit:
            continue
        for rx, label in _BAD_CRASH_RES:
            if rx.search(ln):
                bad.append(f"{label}: {ln.strip()[:100]}")
                break
    if bad:
        return {"ok": False, "kind": "crash", "signals": (bad + good)[:5]}
    if good:
        return {"ok": True, "kind": "expected-missing", "signals": good[:5]}
    return {"ok": True, "kind": "unclassified", "signals": []}


# ---------------------------------------------------------------------------
# 10. FAILURE SIGNATURE (review steal #4: abandon after 3 identical failures)
# ---------------------------------------------------------------------------
_CASE_NAME_RES = (re.compile(r"^\s*not ok \d+ - (.+?)\s*(?:#.*)?$", re.M),
                  re.compile(r"^\s*(?:FAIL|FAILED)\s*[:\-]\s*(.+)$", re.M),
                  re.compile(r"^\s*(?:✖|✗|×)\s+(.+?)(?:\s+\([\d.]+ms\))?$", re.M))


def failure_signature(text: str) -> str:
    """Stable fingerprint of a failure: first assertion line + sorted failing case names, with
    numbers/addresses/paths-timestamps normalised so run-to-run noise does not change it.
    '' when `text` carries no failure evidence."""
    import hashlib
    t = _ANSI.sub("", text or "")
    first = ""
    for ln in t.splitlines():
        if re.search(r"AssertionError|ERR_ASSERTION|\bExpected\b|\bexpected\b.*\b(?:got|received|to)\b|"
                     r"^\s*E\s+assert", ln):
            first = ln.strip()
            break
    cases = sorted({c.strip() for rx in _CASE_NAME_RES for c in rx.findall(t)})
    if not first and not cases:
        return ""
    norm = re.sub(r"0x[0-9a-f]+|\b\d+(?:\.\d+)?ms\b|\b\d{4}-\d\d-\d\dT[\d:.]+Z?|(?:/[\w.-]+){2,}|:\d+(?::\d+)?\b", "#", first)
    return hashlib.sha1((norm + "\0" + "\0".join(cases)).encode()).hexdigest()[:16]


ABANDON_AFTER = 3


def repeat_failure_state(prev_sig, sig, count, cap: int = ABANDON_AFTER):
    """PURE. ('continue'|'hint'|'abandon', new_count). `count` = how many CONSECUTIVE rounds
    (including the previous one) already carried `prev_sig`. An empty sig never counts."""
    if not sig:
        return "continue", 0
    n = count + 1 if sig == prev_sig else 1
    if n >= cap:
        return "abandon", n
    return ("hint" if n >= 2 else "continue"), n


# ---------------------------------------------------------------------------
# 11. DETERMINISTIC BASE RESOLUTION (smoke report defect 2, 2026-10-09)
# ---------------------------------------------------------------------------
# The scaffold's _fresh_base_ref silently re-bases to origin/main when HEAD is behind, and its
# unlocked `git fetch origin` collided when 3 dispatches started at once (ref-lock error ->
# "cutting from HEAD" fallback): the same call resolved to DIFFERENT bases. Auto now resolves
# the base itself, ONCE, and hands the sha to the scaffold as --baseline-ref:
#   * explicit --baseline-ref  -> that commit, NO network fetch;
#   * default                  -> HEAD, unless HEAD is strictly behind origin's default branch,
#     decided after a fetch taken under a per-repo flock (collisions serialise, none fall back).
# The resolved sha is recorded in .dispatch-harness.json (`base_sha`) so a later gate can
# compare it with the tree it is looking at (base_drift_findings).
def _rev(repo, ref):
    r = _git_run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.decode().strip() if r is not None and r.returncode == 0 else ""


def resolve_base_sha(repo, explicit=None, fetch_timeout=90, lock_timeout=240):
    """(sha, note). Raises ValueError for an explicit ref that is not a commit."""
    repo = Path(repo)
    if explicit:
        sha = _rev(repo, explicit)
        if not sha:
            raise ValueError(f"--baseline-ref {explicit!r} is not a commit in {repo}")
        return sha, f"explicit --baseline-ref {explicit} -> {sha[:12]} (no fetch)"
    head = _rev(repo, "HEAD")
    if not head:
        raise ValueError(f"{repo} has no HEAD commit")
    import fcntl
    gd = _git_run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    lockdir = Path(gd.stdout.decode().strip()) if gd is not None and gd.returncode == 0 else repo
    fh = None
    try:
        fh = open(lockdir / "ods-base-fetch.lock", "a+")
        deadline = time.time() + lock_timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    return head, f"fetch lock busy > {lock_timeout}s -- using HEAD {head[:12]}"
                time.sleep(0.2)
        try:
            f = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"],
                               capture_output=True, text=True, timeout=fetch_timeout)
            if f.returncode != 0:
                return head, f"fetch origin failed ({(f.stderr or '').strip()[:100]}) -- using HEAD {head[:12]}"
        except (OSError, subprocess.SubprocessError) as e:
            return head, f"fetch origin failed ({type(e).__name__}) -- using HEAD {head[:12]}"
        sym = _git_run(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        cands = [sym.stdout.decode().strip()] if sym is not None and sym.returncode == 0 and sym.stdout.strip() else []
        cands += ["origin/main", "origin/master"]
        remote = next((c for c in cands if _rev(repo, c)), None)
        if not remote:
            return head, f"no origin default branch -- using HEAD {head[:12]}"
        rsha = _rev(repo, remote)
        if rsha == head:
            return head, f"HEAD is {remote} ({head[:12]})"
        behind = _git_run(repo, "merge-base", "--is-ancestor", head, rsha)
        if behind is not None and behind.returncode == 0:
            return rsha, f"HEAD {head[:12]} is behind {remote} {rsha[:12]} -- base {rsha[:12]}"
        return head, f"HEAD {head[:12]} is ahead of/diverged from {remote} -- base HEAD"
    finally:
        if fh is not None:
            try:
                fh.close()           # releases the flock
            except OSError:
                pass


def record_base_sha(wt, sha, note="", probe=None):
    """Merge `base_sha` (+ note, optional baseline_probe) into the worktree's harness manifest."""
    p = Path(wt) / ".dispatch-harness.json"
    d = _manifest(Path(wt))
    d["base_sha"] = sha
    if note:
        d["base_note"] = note
    if probe is not None:
        d["baseline_probe"] = probe
    p.write_text(json.dumps(d, indent=2))


def base_drift_findings(wt):
    """[finding] -- the recorded base_sha must be an ANCESTOR of (or equal to) the tree's HEAD
    (seal commits legitimately advance HEAD; a re-base/reset does not). No base_sha = no
    finding (older harnesses)."""
    wt = Path(wt)
    base = _manifest(wt).get("base_sha")
    if not base:
        return []
    r = _git_run(wt, "merge-base", "--is-ancestor", base, "HEAD")
    if r is not None and r.returncode == 0:
        return []
    head = _rev(wt, "HEAD")
    return [_finding("BASE_DRIFT", "worktree HEAD is not descended from the recorded base", base[:12],
                     f"harness metadata says the spec was checked against base {base[:12]} but "
                     f"HEAD is {head[:12] or '?'}, which does not contain it: the tree was "
                     f"re-based/reset after the spec was checked, so every base-relative "
                     f"verdict is stale. Re-run from the resolved base.")]


def baseline_probe(wt, timeout=300):
    """Run the scaffold's verify.sh ONCE at the (still untouched) base, before any author job.
    Returns {"ran": bool, "rc": int|None, "green": bool, "tail": str}. Never raises."""
    wt = Path(wt)
    if not (wt / "verify.sh").is_file():
        return {"ran": False, "rc": None, "green": False, "tail": "no verify.sh"}
    try:
        p = subprocess.run(["bash", "verify.sh"], cwd=str(wt), capture_output=True, text=True,
                           timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return {"ran": True, "rc": p.returncode, "green": p.returncode == 0 and "VERIFY_OK" in out,
                "tail": out.strip()[-300:]}
    except subprocess.TimeoutExpired:
        return {"ran": True, "rc": 124, "green": False, "tail": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ran": False, "rc": None, "green": False, "tail": f"{type(e).__name__}: {e}"}


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
