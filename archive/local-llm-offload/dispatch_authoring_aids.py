#!/usr/bin/env python3
"""dispatch_authoring_aids.py -- shared, PURE helpers for harness authoring (2026-10-09).

Born from the hand-repair of slice aw-codec-floor s2b (a "define a local before a log line" change
inside handle_upgrade_import, a 7059-line module with heavy IO globals; the model failed 14x2
iterations). Four lessons live here so scaffold / slicer / the auto self-check share ONE rule:

  A. HEAVY-GLOBAL FIXTURE EXAMPLE  heavy_python_reasons(), heavy_fixture_block(), heavy_prompt_section()
     A python target that is big (> DISPATCH_HEAVY_TARGET_LINES, default 1500) OR imports an IO
     library at module top level (requests, flask, sqlite3, psycopg2, ...) gets a WORKED EXAMPLE of the
     only pattern that works: exec_module FIRST, THEN patch `target.<io global>` for every IO global,
     THEN call the handler; and for a change only visible in a LOCAL variable, a recording stub of the
     module's `log` that reads sys._getframe(depth).f_locals at a known log line. (The model had set stubs
     AFTER exec_module / after the call, so real network globals still ran.)
  B. NO INVENTED DEFECTS            defect_todo_text(), defect_section_findings()
     A baseline probe shows a defect or it does not. Otherwise the section says "behavior to add".
  C. SELF-CHECK NAMES THE FAILURE   selfcheck_failure_report()
     "refimpl.py does not make verify.sh print VERIFY_OK" was printed when the real failure was a
     NameError in the fixture. The report names stage, file:line and the first failing assertion.
  E. EDIT-SURFACE LINT              edit_surface_findings()
     A slice whose target file is huge (> DISPATCH_EDIT_SURFACE_LINES, default 2000) and which carries
     no excerpt / extraction hint gets a WARNING (never a block).

Every function is pure (text in, text/list out) and fail-open for its callers. Env knobs:
  DISPATCH_HEAVY_TARGET_LINES  (1500)   size threshold for the heavy-global fixture example
  DISPATCH_HEAVY_TARGET=0               disable A entirely
  DISPATCH_EDIT_SURFACE_LINES  (2000)   size threshold for the edit-surface warning
  DISPATCH_EDIT_SURFACE_WARN=0          disable E entirely
"""
from __future__ import annotations

import ast
import os
import re

# --------------------------------------------------------------------------- tunables


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def heavy_lines_threshold() -> int:
    return _env_int("DISPATCH_HEAVY_TARGET_LINES", 1500)


def edit_surface_lines_threshold() -> int:
    return _env_int("DISPATCH_EDIT_SURFACE_LINES", 2000)


# Top-level imports that mean "this module does IO when its globals run".
IO_MODULES = frozenset({
    "requests", "httpx", "aiohttp", "urllib3", "flask", "fastapi", "starlette", "django", "bottle",
    "sqlite3", "psycopg", "psycopg2", "pymysql", "mysql", "sqlalchemy", "redis", "pymongo", "boto3",
    "smtplib", "paramiko", "socket", "websocket", "websockets", "docker", "influxdb_client",
    "subprocess", "http", "ftplib", "telnetlib", "imaplib", "poplib",
})
# dotted forms (urllib.request is IO; urllib.parse is not)
IO_DOTTED = frozenset({"urllib.request", "http.client", "http.server", "xmlrpc.client"})


# --------------------------------------------------------------------------- A. heavy target


def _top_level_imports(text: str):
    """[(local_alias, module_dotted)] for module-level imports (descending into try/if/with, never
    into def/class). Falls back to a column-0 regex when the file does not parse."""
    out = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in re.finditer(r"^(?:import\s+([\w.]+)(?:\s+as\s+(\w+))?|from\s+([\w.]+)\s+import\s+([\w*, ]+))", text, re.M):
            if m.group(1):
                out.append(((m.group(2) or m.group(1).split(".")[0]), m.group(1)))
            else:
                for nm in m.group(4).split(","):
                    nm = nm.strip().split(" as ")[0].strip()
                    if nm:
                        out.append((nm, m.group(3)))
        return out

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import):
                for al in node.names:
                    out.append((al.asname or al.name.split(".")[0], al.name))
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for al in node.names:
                    out.append((al.asname or al.name, node.module))
            elif isinstance(node, (ast.Try,)):
                walk(node.body)
                for h in node.handlers:
                    walk(h.body)
                walk(node.orelse)
                walk(node.finalbody)
            elif isinstance(node, (ast.If, ast.With)):
                walk(node.body)
                walk(getattr(node, "orelse", []) or [])
    walk(tree.body)
    return out


def _is_io_module(dotted: str) -> bool:
    d = dotted or ""
    return d in IO_DOTTED or d.split(".")[0] in IO_MODULES or any(d.startswith(x + ".") for x in IO_DOTTED)


def heavy_python_reasons(text: str, rel: str = "", *, line_count: int | None = None) -> list:
    """PURE. Why this python target needs the heavy-global fixture example, [] when it does not.
    Rule (documented, env-tunable): the target is a .py file AND (it has more than
    DISPATCH_HEAVY_TARGET_LINES lines (default 1500) OR it imports an IO library at module top level)."""
    if os.environ.get("DISPATCH_HEAVY_TARGET", "1") == "0":
        return []
    if rel and not rel.endswith(".py"):
        return []
    reasons = []
    n = line_count if line_count is not None else (text.count("\n") + (1 if text and not text.endswith("\n") else 0))
    lim = heavy_lines_threshold()
    if n > lim:
        reasons.append(f"{n} lines (> {lim})")
    mods = sorted({m for _a, m in _top_level_imports(text) if _is_io_module(m)})
    if mods:
        reasons.append("top-level import of IO module(s): " + ", ".join(mods[:6]))
    return reasons


_IO_NAME_HINT = re.compile(r"(login|logout|fetch|get_all|send|post|push|notify|request|query|connect|download|"
                           r"upload|scrape|poll|sync|read_file|write_file|commit|publish|webhook|api|http)", re.I)


HEAVY_CAND_SHOW = 80   # was 25: on a 7000-line target the stubs the handler needs sat past the cut


def io_candidate_globals(text: str, limit: int = 40) -> list:
    """PURE. Names of module-level functions the fixture should patch: those whose body touches an
    imported IO module (or open()/print-free file IO), plus ones whose NAME says IO. Best effort."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    io_aliases = {a for a, m in _top_level_imports(text) if _is_io_module(m)}
    # Module-level globals BUILT FROM an IO module (`session = requests.Session()`) are IO handles
    # too: a function that only touches `session.post(...)` never names the imported module, so the
    # alias-only scan missed it (aw-codec-floor s3: relabel_download_to_base / supersede_torrent
    # absent from the stub list -> the fixture's fake session returned True and the handler logged
    # "Deluge connection failed" 24 iterations in a row).
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(n, ast.Name) and n.id in io_aliases for n in ast.walk(node.value)):
            io_aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    names = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hit = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in io_aliases:
                hit = True
                break
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "open":
                hit = True
                break
        if hit or _IO_NAME_HINT.search(node.name):
            names.append(node.name)
    return names[:limit]


HEAVY_RULES = """\
This target is a HEAVY-GLOBAL python module ({why}). Its module-level functions do real I/O (network,
database, disk, notifications) when called, so a fixture written like a pure-function test hangs, hits
the network, or passes for the wrong reason. The ONLY pattern that works here:

  1. Load the module with importlib (spec_from_file_location + exec_module). Loading only DEFINES things.
  2. THEN patch `target.<name>` for EVERY I/O global the handler can reach -- AFTER exec_module and
     BEFORE the call. Stubs set earlier are overwritten by the module's own definitions; stubs set after
     the call do nothing. Patch a typo'd name and `_patch_io` raises, instead of silently not patching.
  3. Reset the module's own state per case (caches, "recent" sets, counters) before each call.
  4. A change that only DEFINES A LOCAL VARIABLE (no new return value or side effect) is invisible from
     outside. Observe it from the caller's frame at a KNOWN log line: replace the module's `log` (or
     the nearest call that runs after the local is set) with a recorder that reads
     `sys._getframe(depth).f_locals`. At baseline the name is missing -> every case reports MISSING
     and FAILS for the right reason; after the change it reports the value.
  5. Never read the target's source text to assert on it; EXECUTE the handler and assert the output.
{cands}"""

HEAVY_EXAMPLE = '''\
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", {target!r})
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)        # defines names only; NOTHING below has run yet

# I/O globals the handler can reach. Candidates found in the target: {cand_line}
IO_STUBS = {{
    # "login_to_service": lambda *a, **k: None,
    # "get_all_items": lambda *a, **k: dict(ITEMS),
    # "send_notification": lambda *a, **k: None,
}}


def _patch_io(extra=None):
    """Patch target.<name> for EVERY I/O global AFTER exec_module, BEFORE calling the handler."""
    for name, fn in {{**IO_STUBS, **(extra or {{}})}}.items():
        if not hasattr(target, name):
            raise AttributeError("target has no global " + name)
        setattr(target, name, fn)


MISSING = "<local undefined>"


class _Probe:
    """Stand-in for the module's logger. Records local `var` from the CALLER's frame at the log line
    containing `line_substr` -- the way to observe a change that only defines a local variable."""
    def __init__(self, line_substr, var):
        self.line_substr, self.var, self.seen = line_substr, var, []

    def _hit(self, msg, depth):
        if self.line_substr in str(msg):
            self.seen.append(sys._getframe(depth).f_locals.get(self.var, MISSING))

    def info(self, msg, *a, **k):
        self._hit(msg, 2)

    def warning(self, msg, *a, **k):
        self._hit(msg, 2)

    def error(self, msg, *a, **k):
        self._hit(msg, 2)

    debug = exception = info


def _run(handler_args):
    """One case: reset module state, patch I/O, install the probe, call the handler, return what it saw."""
    probe = _Probe("<substring of the log line that runs AFTER the local is set>", "<local_name>")
    target.log = probe                      # or target.logger / the module's real logger name
    # target.some_cache.clear()             # reset module-level state per case
    _patch_io({{}})                          # per-case overrides, e.g. {{"get_all_items": lambda: dict(ITEMS)}}
    target.handler(*handler_args)           # the function under test
    return probe.seen

# CASES = [("description", lambda: _run((...)), [expected_value])]  -- then the scaffold's main() below
'''


def _example_code(target_rel: str, cands) -> str:
    cand_line = ", ".join(cands[:12]) if cands else "(none detected -- grep the handler for network/db/file/clock calls)"
    return HEAVY_EXAMPLE.format(target=target_rel, cand_line=cand_line)


def heavy_prompt_section(target_rel: str, text: str, fixture: str = "test_fixture.py", *,
                         line_count: int | None = None) -> str:
    """PURE. The prompt section for the author model ("" when the target is not heavy)."""
    why = heavy_python_reasons(text, target_rel, line_count=line_count)
    if not why:
        return ""
    cands = io_candidate_globals(text)
    cl = ("\nI/O globals detected in the target (patch every one the handler reaches): "
          + ", ".join(cands[:HEAVY_CAND_SHOW]) + "\n") if cands else ""
    return ("\n## HEAVY-GLOBAL TARGET -- fixture pattern (worked example)\n"
            + HEAVY_RULES.format(why="; ".join(why), cands=cl)
            + f"\nShape for `{fixture}` (adapt names; keep the scaffold's CASES/main() runner):\n```python\n"
            + _example_code(target_rel, cands) + "```\n")


def heavy_fixture_block(target_rel: str, text: str, *, line_count: int | None = None) -> str:
    """PURE. The worked example as a COMMENT block for the scaffolded fixture stub ("" if not heavy).
    Comments only: the stub stays inert, so it still fails with SCAFFOLD_INCOMPLETE until authored."""
    why = heavy_python_reasons(text, target_rel, line_count=line_count)
    if not why:
        return ""
    cands = io_candidate_globals(text)
    cl = ("\nI/O globals detected in the target: " + ", ".join(cands[:HEAVY_CAND_SHOW]) + "\n") if cands else ""
    body = HEAVY_RULES.format(why="; ".join(why), cands=cl) + "\nWORKED EXAMPLE (uncomment and adapt):\n\n" + _example_code(target_rel, cands)
    lines = ["# " + "=" * 74, "# HEAVY-GLOBAL TARGET -- read this before writing any case", "# " + "=" * 74]
    lines += [("# " + l).rstrip() for l in body.splitlines()]
    lines.append("# " + "=" * 74)
    return "\n".join(lines) + "\n"


def inject_heavy_into_fixture(stub: str, target_rel: str, text: str) -> str:
    """PURE. Insert the comment block just before the stub's `CASES = [` (appended when absent)."""
    blk = heavy_fixture_block(target_rel, text)
    if not blk:
        return stub
    marker = "\nCASES = ["
    i = stub.find(marker)
    if i < 0:
        return stub.rstrip("\n") + "\n\n" + blk
    return stub[:i + 1] + blk + "\n" + stub[i + 1:]


# --------------------------------------------------------------------------- B. no invented defects

_ADDITIVE_RE = re.compile(
    r"^\s*(?:in\s+[\w`./-]+[, ]+)?(?:add|adds|define|defines|introduce|introduces|create|creates|extract|extracts|"
    r"compute|computes|bind|binds|set|sets|assign|assigns|emit|emits|expose|exposes|wire|wires|implement|"
    r"implements|new\b|a new\b)", re.I)
_DEFECT_CLAIM_RE = re.compile(r"\b(bug|broken|incorrect(?:ly)?|wrong(?:ly)?|crash(?:es)?|fails?|failing|"
                              r"regress\w*|defect|raises?|throws?|never works|does not work)\b", re.I)


def intent_is_additive(intent: str) -> bool:
    """PURE. True when the intent adds/defines something rather than fixing observed wrong behavior."""
    return bool(_ADDITIVE_RE.search(intent or ""))


def defect_todo_text(intent: str = "") -> str:
    """PURE. The placeholder the scaffold writes under `## Confirmed defect`. Starts with TODO so the
    self-check refuses a TASK.md that kept it."""
    add = ("\nThis slice's intent is ADDITIVE (it adds/defines something), so use form (b).\n"
           if intent_is_additive(intent) else "")
    return (
        "TODO -- write ONLY what a baseline run actually shows, in ONE of two forms (delete the other):\n"
        "  (a) DEFECT: `Running <the probe/fixture> at baseline prints <the exact output pasted here>`,\n"
        "      which is a wrong value or an exception. Use this ONLY when you ran it and saw that.\n"
        "  (b) BEHAVIOR TO ADD (no defect observed at baseline): `At baseline <symbol or line, with\n"
        "      file:line> does not exist / does not do X; this change adds it.` Nothing is claimed broken.\n"
        "Do NOT name callers, logging, activity records, notifications or any other consumer unless you can\n"
        "cite the file:line in the TARGET that reads the value. A defect or consumer you did not observe\n"
        "misleads the dispatched model into editing code that does not exist. Not a suspected mechanism,\n"
        "not a named culprit." + add)


_SECTION_RE = re.compile(r"##+\s*Confirmed (?:defect|symptom)[^\n]*\n(.*?)(?=\n##\s|\Z)", re.S | re.I)
_IDENT_TOKEN_RE = re.compile(r"`([A-Za-z_][\w.]*(?:\(\))?)`|\b([a-z]+(?:_[a-z0-9]+)+|[a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")


def defect_section_findings(task_text: str, *, target_text: str = "", intent: str = "",
                            extra_text: str = "") -> list:
    """PURE. Advisory findings about the `## Confirmed defect` section of a TASK.md:
      * identifier-like tokens in it that appear nowhere in the target, the intent, the fixture/refimpl
        (`extra_text`) or the rest of TASK.md  -> an INVENTED consumer/symbol;
      * a defect claim (bug/broken/fails...) when the intent is additive and the section does not use the
        'behavior to add' form.
    Empty list = nothing suspicious (or no section / no target text to compare with)."""
    m = _SECTION_RE.search(task_text or "")
    if not m:
        return []
    sec = m.group(1)
    if not sec.strip() or sec.strip().startswith("TODO"):
        return []
    rest = (task_text[:m.start()] + task_text[m.end():]) if task_text else ""
    haystack = "\n".join([target_text or "", intent or "", extra_text or "", rest])
    out = []
    if target_text:
        seen = []
        for mm in _IDENT_TOKEN_RE.finditer(sec):
            tok = (mm.group(1) or mm.group(2) or "").rstrip("()")
            if not tok or len(tok) < 4 or tok in seen:
                continue
            base = tok.split(".")[-1]
            if base in haystack or tok in haystack:
                continue
            seen.append(tok)
        if seen:
            out.append("the Confirmed-defect section names " + ", ".join(f"`{t}`" for t in seen[:6])
                       + " which appear nowhere in the target, the intent or the fixture -- an invented "
                       "consumer/symbol? State only what the baseline probe shows (or write 'Behavior to add').")
    if intent_is_additive(intent) and _DEFECT_CLAIM_RE.search(sec) and not re.search(r"behaviou?r to add", sec, re.I):
        out.append("the intent is additive but the section claims a defect (" +
                   (_DEFECT_CLAIM_RE.search(sec).group(0)) + "); unless the baseline probe printed a wrong "
                   "value/exception, write 'Behavior to add (no defect observed at baseline)'.")
    return out


# --------------------------------------------------------------------------- C. self-check report

_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)|AssertionError)\b[:\s]?(.*)$")
_SECTION_HDR_RE = re.compile(r"^=== (.+?) ===\s*$", re.M)
_CASE_FAIL_RE = re.compile(r"^\s*(FAIL (?!:)[^\n]+|not ok \d+ - [^\n]+|✖ [^\n]+)$", re.M)


def _stdlib_like(path: str) -> bool:
    p = path.replace("\\", "/")
    return (p.startswith("<") or "/site-packages/" in p or "/lib/python" in p or "/Lib/" in p
            or "/Frameworks/Python.framework/" in p)


def selfcheck_failure_report(output: str, *, fixture_names=("test_fixture.py",), target: str = "",
                             refimpl: str = "refimpl.py") -> dict:
    """PURE. Name WHICH stage / file failed and the first failing assertion from verify.sh output.
    Returns {"kind", "stage", "file", "line", "exception", "first_assertion", "text"}; `text` is the
    ready-to-print block ("" when nothing could be determined). kind is one of
      fixture-error   an exception propagated out of the FIXTURE (import/setup crash, NameError, ...) --
                      refimpl.py is NOT the problem
      target-error    the traceback ends inside the target called from the fixture (refimpl-emitted
                      code raised) or the target does not parse
      case-failed     the fixture ran and named failing case(s) (fixture and refimpl disagree)
      literals        a Must-contain literal is missing
      other           a verify stage failed for another reason
    """
    out = output or ""
    fx = {os.path.basename(n) for n in (fixture_names or ())}
    tgt = os.path.basename(target) if target else ""
    # stage: the first "=== name ===" section that holds a verify-style `  FAIL:` line
    stage = ""
    hdrs = list(_SECTION_HDR_RE.finditer(out))
    for i, h in enumerate(hdrs):
        end = hdrs[i + 1].start() if i + 1 < len(hdrs) else len(out)
        if re.search(r"^\s*FAIL:", out[h.end():end], re.M):
            stage = h.group(1)
            break
    # traceback: LAST traceback block's frames and exception
    frames = [(m.group(1), int(m.group(2)), m.group(3) or "") for m in _FRAME_RE.finditer(out)]
    user = [f for f in frames if not _stdlib_like(f[0])]
    exc = ""
    for ln in reversed(out.splitlines()):
        mm = _EXC_RE.match(ln.strip())
        if mm and (frames or "Error" in ln):
            exc = ln.strip()[:240]
            break
    first_assert = ""
    cm = _CASE_FAIL_RE.search(out)
    if cm:
        first_assert = cm.group(1).strip()[:300]
    rep = {"kind": "other", "stage": stage, "file": "", "line": 0, "exception": exc,
           "first_assertion": first_assert, "text": ""}
    if user:
        inner = user[-1]
        base = os.path.basename(inner[0])
        # outermost fixture frame (the case that triggered it), if any
        fx_frames = [f for f in user if os.path.basename(f[0]) in fx]
        if base in fx:
            rep.update(kind="fixture-error", file=base, line=inner[1])
        elif fx_frames:
            rep.update(kind="target-error", file=base, line=inner[1])
            rep["called_from"] = f"{os.path.basename(fx_frames[-1][0])}:{fx_frames[-1][1]}"
        elif base == refimpl:
            rep.update(kind="other", file=base, line=inner[1])
        else:
            rep.update(kind="target-error" if (tgt and base == tgt) else "other", file=base, line=inner[1])
    elif "Must-contain literal is missing" in out or re.search(r"^\s*MISSING\b", out, re.M):
        rep["kind"] = "literals"
        rep["file"] = "check_literals.py"
    elif first_assert or re.search(r"\d+/\d+ case\(s\) passed", out):
        rep["kind"] = "case-failed"
        rep["file"] = next(iter(fx), "")
    if rep["kind"] == "other" and not (stage or exc or first_assert):
        return rep
    where = {
        "fixture-error": "the FIXTURE itself raised -- not refimpl.py, not the target",
        "target-error": "the code refimpl.py emitted into the target raised",
        "case-failed": "the fixture ran and named failing case(s): fixture and refimpl.py disagree",
        "literals": "a TASK.md Must-contain literal is absent from the file refimpl.py wrote",
        "other": "see the output below",
    }[rep["kind"]]
    lines = ["WHERE IT FAILED:"]
    if stage:
        lines.append(f"  stage:  {stage}")
    if rep["file"]:
        loc = f"{rep['file']}:{rep['line']}" if rep["line"] else rep["file"]
        extra = f" (called from {rep['called_from']})" if rep.get("called_from") else ""
        lines.append(f"  file:   {loc}{extra} -- {where}")
    else:
        lines.append(f"  kind:   {where}")
    if exc:
        lines.append(f"  error:  {exc}")
    if first_assert:
        lines.append(f"  first failing assertion: {first_assert}")
    rep["text"] = "\n".join(lines)
    return rep


# --------------------------------------------------------------------------- E. edit-surface lint

_EXCERPT_KEYS = ("excerpt", "excerpt_hint", "extract", "extract_to", "extraction", "localized_excerpt")
_EXCERPT_INTENT_RE = re.compile(
    r"\bexcerpt\b|\bextract(?:s|ed|ing)?\b[^.\n]{0,60}\b(?:pure|function|helper|module|file|into)\b", re.I)


def has_excerpt_hint(s: dict) -> bool:
    """PURE. True when the slice carries an excerpt / extraction hint: a non-empty excerpt-ish key, or
    the intent/title says to excerpt or to extract a pure function/helper/module."""
    s = s or {}
    if any(s.get(k) for k in _EXCERPT_KEYS):
        return True
    return bool(_EXCERPT_INTENT_RE.search(" ".join(str(s.get(k) or "") for k in ("intent", "title", "interface"))))


def edit_surface_findings(rel: str, line_count: int, s: dict | None) -> list:
    """PURE. [warning text] when the slice's target is very large and no excerpt/extraction hint is
    present. A WARNING only (never blocks); thresholds env-tunable; DISPATCH_EDIT_SURFACE_WARN=0 off."""
    if os.environ.get("DISPATCH_EDIT_SURFACE_WARN", "1") == "0":
        return []
    lim = edit_surface_lines_threshold()
    if not rel or line_count <= lim or (s or {}).get("surface_ack"):
        return []
    if has_excerpt_hint(s or {}):
        return []
    return [f"edit-surface: target `{rel}` is {line_count} lines (> {lim}) and this slice has no excerpt/extraction "
            f"hint. Small models degrade with input length (the whole file + its globals stay in context no "
            f"matter how finely the change is sliced). Recommend giving the slice an `excerpt` (the function + "
            f"the globals it reads/writes) or a prior slice that EXTRACTS a pure function into a new file. "
            f"Set `surface_ack: true` on the slice to silence."]
