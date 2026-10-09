#!/usr/bin/env python3
"""worker_robust -- model-output robustness for ollama-worker.py (Phase 3, 2026-10-08).

Pure, side-effect-free helpers (no network, no worker globals) so each one is unit-testable
and provable red-on-revert from pipeline-canary.py. The worker imports this module and calls
the functions at the seams marked "worker_robust" in ollama-worker.py.

  strip_think(text)                     think-tag leakage: drop closed <think>..</think> blocks
  repair_tool_calls(content, names)     layered extraction for what the worker's own parser missed:
                                        Qwen3-Coder XML (tolerating unterminated/truncated blocks)
                                        -> Hermes JSON in <tool_call> (tolerating truncation)
                                        -> inline JSON {name, arguments}
                                        -> (optional, profile flag) small-model extractor callback
  diagnose_format_error(content, ...)   WHY a turn that looks like a tool call attempt is unusable
  FormatRequery                         <=3 consecutive free re-prompts, then repeated_format_error
  LoopDetector                          repeated reads / repeated writes / A->B->A / no-progress;
                                        first detection -> warn message, second -> loop_detected
  check_stop_criteria(...)              task_complete gate: verify + required files + must_contain
  ReasoningBudget                       runaway-reasoning (no tool call) detector
  extract_bash_block(content)           bash-only action mode (mini-swe-agent style)

Patterns: SWE-agent forward_with_handling (templated error re-prompt, max_requeries=3);
mini-swe-agent (RepeatedFormatError / named exit reasons); goose toolshim (layered extraction);
vLLM qwen3_coder + hermes tool parsers; oh-my-agent persistent-mode (stop-gate re-checks ALL
criteria every round); Tianshu convergence-detector (refuse 'done' without evidence).
"""
import hashlib
import json
import os
import re

# ---- named exit reasons (registered in failure_ledger.py + worker terminal reasons) -------
REASON_FORMAT = "repeated_format_error"
REASON_LOOP = "loop_detected"
REASON_STOPGATE = "stop_gate_failed"
REASON_REASONING = "reasoning_runaway"
NEW_EXIT_REASONS = (REASON_FORMAT, REASON_LOOP, REASON_STOPGATE, REASON_REASONING)

# ---------------------------------------------------------------------------------------
# 0. think-tag handling
# ---------------------------------------------------------------------------------------
_THINK_CLOSED_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_THINK_STRAY_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def strip_think(text):
    """Remove reasoning that leaked into content. Returns (visible, info) where info is
    {"closed": n, "unterminated": bool, "stray_close": bool}.
      * closed <think>..</think> blocks are dropped (a call QUOTED inside reasoning is not a call)
      * an UNTERMINATED <think> (generation cut mid-reasoning) drops everything after the opener
      * a stray </think> with no opener (template put the opener in the prompt) drops everything
        up to and including it -- what precedes it is reasoning."""
    text = text or ""
    info = {"closed": 0, "unterminated": False, "stray_close": False}
    out, n = _THINK_CLOSED_RE.subn("", text)
    info["closed"] = n
    m = _THINK_OPEN_RE.search(out)
    if m:
        info["unterminated"] = True
        out = out[:m.start()]
    m = _THINK_STRAY_CLOSE_RE.search(out)
    if m:
        info["stray_close"] = True
        out = out[m.end():]
    return out.strip(), info


# ---------------------------------------------------------------------------------------
# 1. layered tool-call repair
# ---------------------------------------------------------------------------------------
_READONLY_OK_TRUNCATED = frozenset({"read_file", "list_files"})
_XML_OPEN_RE = re.compile(r"<function=([\w.\-]+)>")
_PARAM_OPEN_RE = re.compile(r"<parameter=([\w.\-]+)>")


def _trim_one_newline(v):
    if v.startswith("\n"):
        v = v[1:]
    if v.endswith("\n"):
        v = v[:-1]
    return v


def _coerce_param(key, value):
    if key == "additional":
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def parse_qwen3_xml(content, valid_names):
    """Qwen3-Coder XML: <function=NAME><parameter=K>V</parameter>...</function>, optionally inside
    <tool_call>..</tool_call>. Tolerates a MISSING </parameter>, </function> and </tool_call>
    (the vLLM qwen3_coder parser does the same for streamed/unterminated output).
    Returns (calls, spans, truncated) -- spans are (start, end) of what was consumed;
    truncated is True when the LAST call ran to end-of-input with no closer at all (so its last
    parameter value may be cut short)."""
    calls, spans, truncated = [], [], False
    pos = 0
    while True:
        m = _XML_OPEN_RE.search(content, pos)
        if not m:
            break
        name = m.group(1)
        body_start = m.end()
        # the call ends at </function>, else at </tool_call>, else at the next <function=, else EOF
        end_f = content.find("</function>", body_start)
        nxt = _XML_OPEN_RE.search(content, body_start)
        end_t = content.find("</tool_call>", body_start)
        cands = [(p, ln) for p, ln in ((end_f, len("</function>")), (end_t, 0)) if p >= 0]
        if nxt:
            cands.append((nxt.start(), 0))
        if cands:
            body_end, closer_len = min(cands)
            closed = True
        else:
            body_end, closer_len, closed = len(content), 0, False
        body = content[body_start:body_end]
        args = {}
        ppos = 0
        while True:
            pm = _PARAM_OPEN_RE.search(body, ppos)
            if not pm:
                break
            key = pm.group(1)
            vstart = pm.end()
            vend = body.find("</parameter>", vstart)
            nextp = _PARAM_OPEN_RE.search(body, vstart)
            if vend >= 0 and (not nextp or vend < nextp.start()):
                value, ppos = body[vstart:vend], vend + len("</parameter>")
            elif nextp:                      # missing </parameter>: value runs to the next <parameter=
                value, ppos = body[vstart:nextp.start()], nextp.start()
            else:                            # last parameter, no closer: runs to the end of the call
                value, ppos = body[vstart:], len(body)
            args[key] = _coerce_param(key, _trim_one_newline(value))
        # extend the consumed span over a trailing </tool_call> and a leading <tool_call>
        s, e = m.start(), body_end + closer_len
        lead = content[:s].rstrip()
        if lead.lower().endswith("<tool_call>"):
            s = len(lead) - len("<tool_call>")
        tail = content[e:]
        tm = re.match(r"\s*</tool_call>", tail)
        if tm:
            e += tm.end()
        if name in valid_names:
            calls.append({"name": name, "arguments": args})
            spans.append((s, e))
            if not closed:
                truncated = True
        pos = max(e, body_start)
    return calls, spans, truncated


def _balance_json(s):
    """Close an unterminated JSON object: finish an open string (dropping a dangling backslash),
    drop a dangling comma/colon, close open brackets/braces. Returns repaired text or None."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if not stack and not in_str:
        return s
    out = s
    if in_str:
        if esc:
            out = out[:-1]
        out += '"'
    out = re.sub(r"[,:]\s*$", "", out)
    # a key with no value left over (`..., "key"`) -> give it null so json.loads accepts it
    if re.search(r'[{,]\s*"[^"\\]*"\s*$', out) and stack and stack[-1] == "}":
        out += ": null"
    return out + "".join(reversed(stack))


def _escape_ctrl(s):
    res, in_str, esc = [], False, False
    for ch in s:
        if not in_str:
            res.append(ch)
            in_str = ch == '"'
            continue
        if esc:
            res.append(ch)
            esc = False
        elif ch == "\\":
            res.append(ch)
            esc = True
        elif ch == '"':
            res.append(ch)
            in_str = False
        else:
            res.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, ch))
    return "".join(res)


def _loads_lenient(s):
    for fix in (lambda x: x, _escape_ctrl,
                lambda x: re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", x),
                lambda x: _escape_ctrl(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", x))):
        try:
            return json.loads(fix(s))
        except Exception:
            continue
    return None


def _json_to_call(obj, valid_names):
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else None
    if fn is not None:
        obj = fn
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or name.strip() not in valid_names:
        return None
    args = obj.get("arguments", obj.get("parameters"))
    if isinstance(args, str):
        args = _loads_lenient(args) or {}
    if args is None and "name" in obj:
        args = {}
    if not isinstance(args, dict):
        args = {k: v for k, v in obj.items() if k not in ("name", "tool", "tool_name", "function")}
    return {"name": name.strip(), "arguments": args}


def parse_hermes_json(content, valid_names):
    """Hermes: <tool_call>{"name":..,"arguments":{..}}</tool_call>. Tolerates trailing text after
    the JSON, a missing </tool_call>, and a JSON object cut off mid-string (balanced + closed).
    Returns (calls, spans, truncated)."""
    calls, spans, truncated = [], [], False
    for m in re.finditer(r"<tool_call>\s*", content):
        start = m.end()
        if content[start:start + 9].startswith("<function"):
            continue                          # XML dialect, handled by parse_qwen3_xml
        brace = content.find("{", start)
        if brace < 0 or content[start:brace].strip():
            continue
        # string-aware scan for the matching close
        depth, in_str, esc, end = 0, False, False, None
        for i in range(brace, len(content)):
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        trunc = end is None
        if trunc:
            close = content.find("</tool_call>", brace)
            cand = content[brace:close] if close >= 0 else content[brace:]
            cand = _balance_json(cand)
            if cand is None:
                continue
            seg_end = close + len("</tool_call>") if close >= 0 else len(content)
        else:
            cand = content[brace:end]
            seg_end = end
            tm = re.match(r"\s*</tool_call>", content[end:])
            if tm:
                seg_end += tm.end()
        call = _json_to_call(_loads_lenient(cand), valid_names)
        if call:
            calls.append(call)
            spans.append((m.start(), seg_end))
            truncated = truncated or trunc
    return calls, spans, truncated


def parse_inline_json(content, valid_names):
    """Bare {"name": N, "arguments": {...}} (or {"tool": N, ...}) anywhere in the text, string-aware.
    Returns (calls, spans, truncated=False). Complements the worker's regex scan: this one also
    accepts a name that is a known tool only (prose false positives), nested "function" wrappers
    and string-encoded arguments."""
    calls, spans = [], []
    i, n = 0, len(content)
    while i < n:
        j = content.find("{", i)
        if j < 0:
            break
        depth, in_str, esc, end = 0, False, False, None
        for k in range(j, n):
            ch = content[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            i = j + 1
            continue
        obj = _loads_lenient(content[j:end])
        call = _json_to_call(obj, valid_names) if obj is not None else None
        if call and any(k in obj for k in ("name", "tool", "tool_name", "function")):
            calls.append(call)
            spans.append((j, end))
            i = end
        else:
            i = j + 1
    return calls, spans, False


def _blank_spans(content, spans):
    out = content
    for s, e in sorted(spans, reverse=True):
        out = out[:s] + out[e:]
    return out.strip()


def repair_tool_calls(content, valid_names, tool_schemas=None, extractor=None):
    """Layered extraction. Returns dict:
      calls    : [{"name","arguments"}]   (possibly [])
      cleaned  : content with the consumed call text removed
      layer    : "xml" | "hermes" | "inline" | "extractor" | None
      truncated: True when an accepted call ran to end-of-input with no closer
      refused_truncated: [names] of truncated calls NOT accepted (a half-written write_file must
                         never be executed -- the caller re-prompts instead)
      think    : strip_think info
    `extractor(visible_text) -> list[call]` is the optional small-model fallback (profile flag
    `tool_extractor`, off by default); it only runs when every deterministic layer found nothing
    AND the text looks like a call attempt."""
    valid = set(valid_names)
    visible, think = strip_think(content)
    res = {"calls": [], "cleaned": visible, "layer": None, "truncated": False,
           "refused_truncated": [], "think": think}
    if not visible:
        return res
    for layer, fn in (("xml", parse_qwen3_xml), ("hermes", parse_hermes_json),
                      ("inline", parse_inline_json)):
        calls, spans, trunc = fn(visible, valid)
        if not calls:
            continue
        if trunc:
            keep = []
            for c in calls:
                if c["name"] in _READONLY_OK_TRUNCATED:
                    keep.append(c)
                else:
                    res["refused_truncated"].append(c["name"])
            if len(keep) != len(calls):
                calls = keep
        if calls:
            res.update(calls=calls, cleaned=_blank_spans(visible, spans), layer=layer, truncated=trunc)
            return res
        if res["refused_truncated"]:
            return res
    if extractor is not None and looks_like_call_attempt(visible, valid):
        try:
            calls = [c for c in (extractor(visible) or []) if isinstance(c, dict)
                     and c.get("name") in valid and isinstance(c.get("arguments", {}), dict)]
        except Exception:
            calls = []
        if calls:
            res.update(calls=calls, layer="extractor")
    return res


# ---------------------------------------------------------------------------------------
# 2. format-error diagnosis + requery
# ---------------------------------------------------------------------------------------
def looks_like_call_attempt(visible, valid_names=()):
    """True when prose-free-ish content smells like a tool call: tags, a json fence with name/args,
    or a JSON object naming a tool."""
    if re.search(r"</?tool_call>|<function=|<parameter=|\[TOOL_CALLS\]|</?function>", visible):
        return True
    if re.search(r'"(?:name|tool|tool_name)"\s*:\s*"(?:%s)"' % "|".join(map(re.escape, valid_names or ["\0"])), visible):
        return True
    if re.search(r'"arguments"\s*:\s*\{', visible):
        return True
    return False


def required_params(tool_schemas):
    out = {}
    for t in tool_schemas or []:
        fn = t.get("function", t)
        out[fn.get("name")] = list((fn.get("parameters") or {}).get("required") or [])
    return out


def validate_calls(calls, tool_schemas):
    """Missing-required-parameter problems for parsed calls -> list of (name, [missing])."""
    req = required_params(tool_schemas)
    bad = []
    for c in calls:
        need = req.get(c["name"])
        if need is None:
            continue
        args = c.get("arguments") or {}
        miss = [k for k in need if k not in args]
        if miss:
            bad.append((c["name"], miss))
    return bad


FORMAT_EXAMPLE = ("<tool_call>\n<function=NAME>\n<parameter=KEY>\nVALUE\n</parameter>\n"
                  "</function>\n</tool_call>")


def diagnose_format_error(content, valid_names, tool_schemas=None, repair=None):
    """None when `content` is not a failed tool-call attempt. Otherwise (kind, message) with a
    templated, specific description of the problem. `repair` is a repair_tool_calls() result for
    the same content (computed if omitted). Kinds: truncated, unknown_tool, bad_json,
    missing_param, dangling_closer, no_parseable_call."""
    valid = sorted(set(valid_names))
    repair = repair or repair_tool_calls(content, valid)
    visible = strip_think(content)[0]
    if repair["calls"]:
        bad = validate_calls(repair["calls"], tool_schemas)
        if bad:
            nm, miss = bad[0]
            return ("missing_param",
                    f"Your {nm} call is missing required parameter(s): {', '.join(miss)}. "
                    f"Re-send the call with every required parameter.")
        return None
    if repair["refused_truncated"]:
        nm = repair["refused_truncated"][0]
        return ("truncated",
                f"Your {nm} call was cut off before it was closed (no closing tag / unterminated "
                f"value), so it was NOT run -- a half-written file must not be applied. Re-send it "
                f"complete. If the content is long, write the file in smaller pieces (create it "
                f"with the first part, then extend it with edit_file or a shell append).")
    if not looks_like_call_attempt(visible, valid):
        return None
    m = _XML_OPEN_RE.search(visible)
    if m and m.group(1) not in valid:
        return ("unknown_tool",
                f"'{m.group(1)}' is not a tool. Valid tools: {', '.join(valid)}. "
                f"Use exactly one of those names.")
    for jm in re.finditer(r'"(?:name|tool|tool_name)"\s*:\s*"([^"]+)"', visible):
        if jm.group(1) not in valid:
            return ("unknown_tool",
                    f"'{jm.group(1)}' is not a tool. Valid tools: {', '.join(valid)}. "
                    f"Use exactly one of those names.")
    if re.search(r"</tool_call>", visible) and not re.search(r"<tool_call>", visible):
        return ("dangling_closer",
                "Your response has a closing </tool_call> but no opening <tool_call>, so no call "
                "was recognized. Open the block with <tool_call> before the <function=...> line.")
    if "{" in visible and re.search(r'"(?:name|arguments)"', visible):
        return ("bad_json",
                "Your tool call contains JSON that could not be parsed (check quotes, commas and "
                "escaping of newlines inside strings). Re-send it as valid JSON, or use the "
                "XML format.")
    return ("no_parseable_call",
            "A tool call was started but could not be parsed. Use exactly this format and "
            "nothing else:\n" + FORMAT_EXAMPLE)


def format_error_message(kind, detail, attempt, limit):
    if kind == "bash_format":
        return f"[format error {attempt}/{limit}] {detail}"
    return (f"[format error {attempt}/{limit}] {detail}\n"
            f"Reply with ONE corrected tool call using exactly this format:\n{FORMAT_EXAMPLE}")


def make_llm_extractor(chat_fn, valid_names):
    """Optional last repair layer (profile `tool_extractor`, OFF by default): ask a model to
    re-emit the tool call(s) as JSON. `chat_fn(messages) -> str`. Only called when every
    deterministic layer found nothing AND the text looks like a call attempt."""
    names = sorted(valid_names)

    def extract(text):
        out = chat_fn([
            {"role": "system", "content": "You convert a malformed tool call into strict JSON. Reply with "
             'ONLY {"name": <tool>, "arguments": {...}} or the single word NONE. Valid tools: '
             + ", ".join(names) + ". Never invent arguments that are not in the text."},
            {"role": "user", "content": text[-6000:]}])
        calls, _, _ = parse_inline_json(out or "", set(names))
        return calls
    return extract


class FormatRequery:
    """Consecutive format errors are re-prompted for free (not charged as an iteration) up to
    `limit` times; the next one ends the run with REASON_FORMAT. Any good turn resets."""

    def __init__(self, limit=3):
        self.limit = int(limit)
        self.streak = 0
        self.total = 0
        self.kinds = []

    def on_error(self, kind):
        """-> ("requery", n) or ("stop", n)"""
        self.streak += 1
        self.total += 1
        self.kinds.append(kind)
        if self.streak > self.limit:
            return "stop", self.streak
        return "requery", self.streak

    def on_ok(self):
        self.streak = 0


# ---------------------------------------------------------------------------------------
# 3. loop / convergence detector
# ---------------------------------------------------------------------------------------
LOOP_DEFAULTS = {
    "repeat_read": 5,       # identical read-only call this many times
    "repeat_write": 4,      # identical write (same path, same bytes) this many times
    "aba_returns": 3,       # times a path returns to an EARLIER content hash (A->B->A counts 1)
    "no_progress_iters": 8,  # consecutive iterations with nothing novel (no new call sig, no new bytes)
    "verify_spin": 0,       # OFF by default (poor precision, see calibration); the job's own verify re-run this many times in a row with NO edit between
    "min_iter": 4,          # never fire before this iteration
}
_READ_TOOLS = frozenset({"read_file", "list_files", "web_fetch", "web_search"})
_RO_BASH_RE = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:ls|cat|head|tail|grep|egrep|rg|wc|which|pwd|echo|stat|file|find|diff|"
    r"sed\s+-n|git\s+(?:log|status|diff|show|branch|rev-parse|ls-files)|python3?\s+-c|node\s+-e)\b")


def bash_is_readonly(cmd):
    """Conservative: a pipeline of read-only commands with no redirection/in-place flag."""
    cmd = str(cmd or "")
    if re.search(r"(?<![<2&])>|>>|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|<<", cmd):
        return False
    return all(_RO_BASH_RE.match(part) for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip()) if part)
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def loop_config(profile_robust=None, env=None):
    """defaults <- profile `robust.loop_detector` <- env WORKER_LOOP_<KEY> (int)."""
    cfg = dict(LOOP_DEFAULTS)
    cfg["enabled"] = True
    p = (profile_robust or {}).get("loop_detector") or {}
    for k, v in p.items():
        if k in cfg or k == "enabled":
            cfg[k] = v
    env = os.environ if env is None else env
    for k in list(cfg):
        ev = env.get("WORKER_LOOP_" + k.upper())
        if ev is not None and ev != "":
            try:
                cfg[k] = bool(int(ev)) if k == "enabled" else int(ev)
            except ValueError:
                pass
    return cfg


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()[:12]


class LoopDetector:
    """Feed it every tool call (observe) and tell it when an iteration ends (end_iteration).
    end_iteration returns None, or (action, kind, message) with action "warn" (first detection,
    inject message) or "stop" (second detection -> REASON_LOOP)."""

    def __init__(self, cfg=None):
        self.cfg = dict(LOOP_DEFAULTS)
        self.cfg["enabled"] = True
        self.cfg.update(cfg or {})
        self.read_counts = {}
        self.write_counts = {}
        self.path_hist = {}        # path -> list of content hashes (collapsed consecutive dupes)
        self.path_returns = {}     # path -> number of returns to an earlier hash
        self.seen_sigs = set()
        self.iter_novel = False
        self.stale_iters = 0
        self.novel_writes = 0      # writes with content never seen before on that path
        self.warn_novel_writes = None
        self.mutations = 0         # edits / non-read-only shell commands so far
        self.verify_spin_n = 0
        self._last_verify_mut = None
        self.warned = False
        self.detections = []
        self._calls_this_iter = 0

    # -- input ----------------------------------------------------------------------------
    def observe(self, name, args, exempt=False, content_hash=None):
        """exempt: the job's own verify command (re-running it after an edit is progress)."""
        if not self.cfg.get("enabled"):
            return
        args = args if isinstance(args, dict) else {}
        self._calls_this_iter += 1
        sig = name + ":" + _h(json.dumps(args, sort_keys=True, default=str))
        if sig not in self.seen_sigs:
            self.seen_sigs.add(sig)
            if not exempt:
                self.iter_novel = True
        if exempt:
            # the job's own verify: progress only if something changed since its last run
            if self._last_verify_mut is not None and self._last_verify_mut == self.mutations:
                self.verify_spin_n += 1
            else:
                self.verify_spin_n = 0
            self._last_verify_mut = self.mutations
            return
        if name in _WRITE_TOOLS or (name == "run_bash" and not bash_is_readonly(args.get("command"))):
            self.mutations += 1
            self.read_counts.clear()    # state changed: re-reading is no longer "the same thing"
            if name == "run_bash" and self.read_counts.get(sig, 0) == 0:
                self.novel_writes += 1      # a new mutating shell command (heredoc write etc.)
        if name in _WRITE_TOOLS or (name == "run_bash" and content_hash):
            path = str(args.get("path") or "")
            if name == "edit_file":
                ch = _h("edit|" + str(args.get("old_string")) + "|" + str(args.get("new_string")))
            elif content_hash:
                ch = content_hash
            else:
                ch = _h(args.get("content"))
            key = (path, ch)
            self.write_counts[key] = self.write_counts.get(key, 0) + 1
            hist = self.path_hist.setdefault(path, [])
            if not hist or hist[-1] != ch:
                if ch in hist:
                    self.path_returns[path] = self.path_returns.get(path, 0) + 1
                if ch not in hist:
                    self.novel_writes += 1
                    self.iter_novel = True
                hist.append(ch)
        elif name in _READ_TOOLS or name == "run_bash":
            self.read_counts[sig] = self.read_counts.get(sig, 0) + 1

    # -- verdict --------------------------------------------------------------------------
    def _detect(self, i):
        c = self.cfg
        if i < c["min_iter"]:
            return None
        for sig, n in self.read_counts.items():
            if n >= c["repeat_read"]:
                return ("repeat_read", sig, f"the same call ({sig.split(':')[0]}) has now been issued "
                        f"{n} times with identical arguments and returns the same thing")
        for (path, _), n in self.write_counts.items():
            if n >= c["repeat_write"]:
                return ("repeat_write", path, f"you have written identical content to {path or 'a file'} "
                        f"{n} times")
        for path, n in self.path_returns.items():
            if n >= c["aba_returns"]:
                return ("write_thrash", path, f"{path or 'a file'} keeps flipping back to content it already "
                        f"had (A->B->A, {n} reversions)")
        if c.get("verify_spin") and self.verify_spin_n + 1 >= c["verify_spin"]:
            return ("verify_spin", "", f"the verify command has now run {self.verify_spin_n + 1} times in a "
                    f"row with no edit in between, so it can only report the same failure")
        if self.stale_iters >= c["no_progress_iters"]:
            return ("no_progress", "", f"the last {self.stale_iters} iterations produced nothing new "
                    f"(no new command, no new file content)")
        return None

    def end_iteration(self, i):
        if not self.cfg.get("enabled"):
            return None
        if self.iter_novel or self._calls_this_iter == 0:
            self.stale_iters = 0 if self.iter_novel else self.stale_iters
        else:
            self.stale_iters += 1
        self.iter_novel = False
        self._calls_this_iter = 0
        d = self._detect(i)
        if not d:
            return None
        kind, subject, what = d
        self.detections.append(kind)
        if not self.warned:
            self.warned = True
            self.warn_novel_writes = self.novel_writes
            # re-arm: the SAME evidence must repeat once more (not a full threshold again)
            self._rearm(kind, subject)
            return ("warn", kind, loop_message(kind, what))
        if kind in ("repeat_read", "no_progress", "verify_spin") and self.novel_writes > (self.warn_novel_writes or 0):
            # it ignored the warning but produced genuinely new bytes: that is progress, not a loop
            return None
        return ("stop", kind, f"loop detected again after a warning: {what}")

    def _rearm(self, kind, subject):
        c = self.cfg
        if kind == "repeat_read":
            self.read_counts[subject] = c["repeat_read"] - 1
        elif kind == "repeat_write":
            for k in list(self.write_counts):
                if k[0] == subject:
                    self.write_counts[k] = c["repeat_write"] - 1
        elif kind == "write_thrash":
            self.path_returns[subject] = c["aba_returns"] - 1
        elif kind == "verify_spin":
            self.verify_spin_n = c["verify_spin"] - 2
        elif kind == "no_progress":
            self.stale_iters = c["no_progress_iters"] - 2


def loop_message(kind, what):
    todo = {
        "repeat_read": "You already have this information. Stop re-reading; act on it now -- make "
                       "the edit it implies, or run a different command that tests a new hypothesis.",
        "repeat_write": "Re-writing the same bytes changes nothing. Run the verify to see the "
                        "actual failure, then change something that addresses it.",
        "write_thrash": "You are undoing and redoing the same change. Pick one version, then run the "
                        "verify and read its output before touching the file again.",
        "verify_spin": "Re-running it cannot change the result. Read its output, make an edit that "
                       "addresses the first failing check, and only then run it again.",
        "no_progress": "Nothing has changed for several turns. Either make a concrete edit toward "
                       "the failing check, or, if everything is already done, call task_complete.",
    }[kind]
    return (f"[loop detected] {what}. {todo} If this repeats once more the run will be stopped.")


# ---------------------------------------------------------------------------------------
# 4. stop-gate
# ---------------------------------------------------------------------------------------
def check_stop_criteria(cwd, required_files=(), must_contain=(), verify_ok=None, verify_out="",
                        extra_files=()):
    """Re-checks ALL criteria (never only the previously failing ones).
    -> (ok, failures) with failures a list of human-readable strings.
      * verify_ok: True/False from the worker's _quick_verify, or None when there is no verify
      * required_files: paths (relative to cwd) that must exist and be non-empty
      * must_contain: [(path_or_None, literal)] -- literal must appear in that file (or, with
        path None, in ANY required file or any of `extra_files`, e.g. the files changed this run)."""
    from pathlib import Path
    cwd = Path(cwd)
    failures = []
    if verify_ok is False:
        tail = (verify_out or "").strip()[-600:]
        failures.append("verify command exits non-zero" + (f":\n{tail}" if tail else ""))
    texts = {}
    for f in required_files:
        p = cwd / f
        try:
            ok = p.is_file() and p.stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            failures.append(f"required file missing or empty: {f}")
        else:
            try:
                texts[f] = p.read_text(errors="replace")
            except OSError:
                texts[f] = ""
    for item in must_contain:
        path, lit = item if isinstance(item, (tuple, list)) else (None, item)
        if path:
            if path not in texts:
                try:
                    texts[path] = (cwd / path).read_text(errors="replace")
                except OSError:
                    texts[path] = None
            body = texts[path]
            if body is None:
                failures.append(f"must-contain literal {lit!r}: file {path} is missing")
            elif lit not in body:
                failures.append(f"must-contain literal not found in {path}: {lit!r}")
        else:
            for ef in extra_files:
                if ef not in texts:
                    try:
                        texts[ef] = (cwd / ef).read_text(errors="replace")
                    except (OSError, UnicodeError):
                        texts[ef] = ""
            if not any(lit in t for t in texts.values() if t):
                failures.append(f"must-contain literal not found in the target or any changed file: {lit!r}")
    return (not failures), failures


def stop_gate_message(failures, attempt, limit):
    lines = "\n".join(f"  - {f}" for f in failures)
    return (f"NOT ACCEPTED (stop-gate {attempt}/{limit}): task_complete is only accepted when ALL "
            f"completion criteria hold. Failing right now:\n{lines}\n"
            f"Fix these, then call task_complete again. Every criterion is re-checked each time.")


_PATH_RE = re.compile(r"^[\w@./\-]+\.[A-Za-z0-9]{1,8}$")


def parse_entry_point(task_text):
    """The `## Entry point` path (`lib/x.ts:12` -> `lib/x.ts`), or None when it is not a file path."""
    lines = (task_text or "").splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^#{1,6}\s*entry point\s*$", line.strip(), re.I):
            for nxt in lines[idx + 1:]:
                t = nxt.strip().strip("`")
                if not t:
                    continue
                t = re.sub(r":\d+(?::\d+)?$", "", t.split()[0] if t.split() else "")
                return t if _PATH_RE.match(t) and not t.startswith(("/", "..")) else None
    return None


def parse_task_criteria(task_text):
    """(required_files, must_contain) from a TASK.md-style spec, conservative:
      * required_files: the `## Entry point` file, plus backticked paths under a
        `## Required files` / `## Files to create` heading
      * must_contain: bullets under `## Must contain`; a bare bullet -> (None, literal) meaning
        "the default target (entry point) or any file changed this run"; `in <path>: `tok``
        pins it to <path> -> (path, literal). Literals containing TODO are ignored.
    Absent sections yield nothing -- the gate only enforces what the spec actually states."""
    files, lits = [], []
    ep = parse_entry_point(task_text)
    if ep:
        files.append(ep)
    sect = None
    for line in (task_text or "").splitlines():
        s = line.strip()
        hm = re.match(r"^#{1,6}\s*(.+?)\s*:?\s*$", s)
        if hm:
            t = hm.group(1).lower()
            if re.fullmatch(r"must[- ]contain", t):
                sect = "lit"
            elif re.fullmatch(r"required files?|files to (?:create|write|add)", t):
                sect = "files"
            else:
                sect = None
            continue
        if sect and re.match(r"^[-*]\s+", s):
            toks = re.findall(r"`([^`]+)`", s)
            if sect == "lit":
                pin = re.match(r"^[-*]\s+in\s+([\w@./\-]+)\s*:", s)
                for tk in toks:
                    if "TODO" in tk:
                        continue
                    if pin and tk == pin.group(1):
                        continue
                    lits.append((pin.group(1) if pin else None, tk))
            else:
                files.extend(t for t in toks if _PATH_RE.match(t) and not t.startswith(("/", "..")))
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out, lits


# ---------------------------------------------------------------------------------------
# 5. runaway-reasoning budget
# ---------------------------------------------------------------------------------------
class ReasoningBudget:
    """Streamed-turn guard. Feed reasoning/content chunk sizes; `exceeded()` is True once the turn
    has spent `budget_chars` of reasoning with no content and no tool call. Default 24000 chars
    (~6k tokens) -- real converging turns in the transcripts think for <4k chars at the median
    (see calibration in the report)."""

    def __init__(self, budget_chars=24000):
        self.budget = int(budget_chars)
        self.reasoning = 0
        self.content = 0

    def add_reasoning(self, n):
        self.reasoning += n

    def add_content(self, n):
        self.content += n

    def exceeded(self):
        return self.content == 0 and self.reasoning >= self.budget


def reasoning_runaway_message(chars):
    return ("[reasoning budget] your previous turn spent ~%d characters thinking without making a "
            "tool call, so it was cut off. Do NOT think this through again at length. Decide in "
            "one or two sentences and make exactly one tool call now." % chars)


# ---------------------------------------------------------------------------------------
# 6. bash-only action mode
# ---------------------------------------------------------------------------------------
_BASH_BLOCK_RE = re.compile(r"```(?:bash|sh|shell)[ \t]*\n(.*?)```", re.DOTALL)


def extract_bash_block(content):
    """mini-swe-agent: exactly ONE ```bash block per turn. -> ("ok", cmd) | ("none", None) |
    ("multiple", n). Think-tag leakage is stripped first. `echo TASK_COMPLETE` (alone) = done."""
    visible = strip_think(content)[0]
    blocks = _BASH_BLOCK_RE.findall(visible)
    if not blocks:
        return "none", None
    if len(blocks) > 1:
        return "multiple", len(blocks)
    cmd = blocks[0].strip()
    return ("ok", cmd) if cmd else ("none", None)


def bash_mode_is_done(cmd):
    return cmd.strip().splitlines()[:1] == ["echo TASK_COMPLETE"] and len(cmd.strip().splitlines()) == 1


BASH_MODE_PROMPT = (
    "Respond with EXACTLY ONE fenced ```bash block per turn containing the single shell command to "
    "run, preceded by at most two sentences of reasoning. Write files with heredocs (cat > f <<'EOF'). "
    "When the task is complete, reply with a block containing only: echo TASK_COMPLETE")


def bash_mode_error(kind, n=None):
    if kind == "multiple":
        return (f"Format error: you sent {n} bash blocks. Send EXACTLY ONE ```bash block per turn.")
    return "Format error: no ```bash block found. Send EXACTLY ONE ```bash block with the next command."
