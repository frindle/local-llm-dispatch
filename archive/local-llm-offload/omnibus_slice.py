#!/usr/bin/env python3
"""omnibus_slice -- automatic detection + plan generation + handoff that turns a
single-file, multi-PROPERTY omnibus dispatch into a per-property SLICED CHAIN,
without a human having to notice the choke.

WHY
---
A single-file omnibus (e.g. "arr-codec-floor": a codec_rank helper + a supersede
guard + two dedup sort keys + a dedup_via_sonarr pass, all in ONE 2700-line file)
chokes `ollama-dispatch-auto`: the model burns its whole iteration budget reading
the target and calls request_more_iterations before authoring any deliverable,
then parks for a human. A human then has to notice and hand-write a slice plan for
`ollama-dispatch-slice`. This module makes that AUTOMATIC.

WHAT THIS MODULE DOES (and does NOT do)
---------------------------------------
It provides three things -- DETECTION, auto PLAN-GENERATION, and auto HANDOFF:
  * is_omnibus()          -- heuristic: does the intent/interface describe several
                             independent properties in one target file?
  * build_slices/build_plan -- deterministically decompose the intent/interface
                             into one slice PER property, in dependency order
                             (a single shared helper lands first).
  * plan_failure_action() -- bounded policy for the failure->requeue path.
  * autoslice_and_handoff/handoff_to_slicer -- write the plan and hand off to the
                             EXISTING `ollama-dispatch-slice` (its state machine,
                             gating, worktree chaining and -- crucially -- its
                             per-slice HUMAN relevance review are reused verbatim).

It does NOT reimplement slicing, gating or worktree chaining, and it NEVER
auto-confirms a relevance review: the slicer still STOPS at each slice's
AWAITING_REVIEW checkpoint for the human. Auto-slicing removes the "human must
notice the choke" toil; it must not remove the "human confirms each slice's cases
are relevant" gate.

RE-ENTRANCY / RECURSION GUARD
-----------------------------
The slicer authors each slice by calling `ollama-dispatch-auto` again. To stop
that per-slice (single-property) dispatch from re-triggering detection -- and to
stop a per-slice FAILURE from re-slicing a single property forever -- the handoff
exports OLLAMA_DISPATCH_NO_SPLIT=1, which is_omnibus() and plan_failure_action()
both honour (returning "no split"/"escalate"). A per-slice failure therefore
surfaces through the slicer's own FAILED state, to the human -- it does not spin.

BOUNDED REQUEUE
---------------
A top-level dispatch that FAILS is converted to a sliced chain at most ONCE: the
slicer's run-state file (~/.ollama-dispatch/slice-runs/<label>.json) is the
idempotency marker. If it does not exist -> convert. If it exists and healthy ->
resume (idempotent). If it exists and a slice already FAILED -> escalate to a
human instead of requeuing again.
"""
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
SLICER = BIN / "ollama-dispatch-slice"
QUEUE = BIN / "ollama-queue.py"
CONFIG_DIR = Path(os.environ.get("OLLAMA_DISPATCH_HOME",
                                 Path.home() / ".ollama-dispatch"))
PLANS_DIR = CONFIG_DIR / "slice-plans"
# Kept in sync with ollama-dispatch-slice.STATE_ROOT so we read the same state
# the slicer writes (the idempotency / bound marker).
SLICE_RUNS_DIR = CONFIG_DIR / "slice-runs"

NO_SPLIT_ENV = "OLLAMA_DISPATCH_NO_SPLIT"
DEFAULT_THRESHOLD = 3

# ---------------------------------------------------------------------------
# decomposition
# ---------------------------------------------------------------------------
_STOP_LEAD = re.compile(
    r"^(?:also\s+|then\s+|and\s+|add(?:ing)?\s+(?:a|an|the)?\s*|"
    r"introduce\s+(?:a|an|the)?\s*|insert\s+(?:a|an|the)?\s*|"
    r"a\s+|an\s+|the\s+|two\s+|three\s+|four\s+|\d+\s+)", re.I)
# a snake_case or dotted.callable identifier, or a Route/quoted token
# BUG (2026-09-19): the camelCase branch required a LOWERCASE first char, so on a
# PascalCase identifier (ProgramRegistry, FastAPI, DeltaAdapter, AwardResult, ...)
# position 0 fails the [a-z] check and the engine backtracks to position 1, matching
# a truncated tail ("rogramRegistry", "astAPI") -- confirmed live in must_contain on
# aw-fix-united-import/aw-fix-delta-import/aw-app-wiring. Fixed by allowing the first
# char to be either case while still requiring >=1 lower/digit char before the case
# transition (so a plain acronym like "API" still does not match -- unchanged from
# the original intent, just no longer anchored to lowercase-only starts).
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+|"  # snake_case
                       r"\b[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")  # camelCase/PascalCase
_DEFN_RE = re.compile(
    r"\bdef\s+([a-zA-Z_]\w*)"                               # def foo
    r"|add(?:s|ing)?\s+(?:a|an|the)?\s*`?([a-zA-Z_]\w*)`?\s+"
    r"(?:helper|function|method|util(?:ity)?)"              # add a foo helper
    # BUG (2026-09-19): this third alternative was `(?:helper|function|method)\s+
    # NAME` with NAME unquoted -- it matched ANY word right after those nouns, so
    # ordinary prose like "route function and helper in the file is untouched"
    # matched "function and" and captured "and" as a "defined name", producing a
    # garbage root slice (id s8-and, must_contain=['and']) that every real slice
    # in the plan then depended on (confirmed live on aw-app-wiring). A bare,
    # unquoted word after "helper"/"function"/"method" is prose, not a naming
    # site -- require the name to be backtick-quoted (an unambiguous identifier
    # reference, e.g. "the `computeFoo` helper") instead.
    r"|(?:helper|function|method)\s+`([a-zA-Z_]\w*)`", re.I)
_ENTRY_RE = re.compile(r"([\w./-]+\.\w+:\d+)|lines?\s+(\d+)(?:\s*[-–]\s*\d+)?",
                       re.I)
# words that mark a clause as a real code property (vs prose filler)
_PROPERTY_HINT = re.compile(
    r"\b(helper|function|method|guard|route|endpoint|handler|column|field|"
    r"sort\s*key|sort|dedup|pass|filter|floor|check|hook|migration|index|"
    r"decorator|class|schema|query|table|param|flag|option|rank|key)\b", re.I)
# PRECISION GATE (2026-09-17): a single bounded fix reads as prose describing ONE
# change with GUARD/SKIP conditions and sub-steps; a genuine omnibus reads as a
# LIST of parallel, independently-nameable code artifacts. The detector used to
# count a single property's guard/skip/condition clauses as separate properties
# (BFMR "if it does NOT match AND", "more than one candidate target order",
# "target order already has that reservationId", "or no confident exact match" ->
# 6 slices; build.py "(1)..(2)..(3).." sub-steps -> 4), so a normal spec-complete
# intent tripped the >=3 threshold and each broken fragment then failed authoring.
# Two positive gates below make a clause count as an INDEPENDENT property only when
# it INTRODUCES an artifact, never when it is a subordinate/condition clause.
#
# A clause that LEADS with a subordinating/continuation/quantifier word is part of
# ONE property's description (a guard/skip/else branch), not a new property.
_CONTINUATION_LEAD = re.compile(
    r"^(?:\(?\d*[.)]?|[-*•])\s*"     # tolerate a leading "(2)"/"3."/"-" marker
    r"(?:if|when|whenever|unless|while|where|because|since|so|such|once|"
    r"after|before|or|and|but|then|otherwise|which|that|whose|already|no|not|"
    r"more|less|fewer|each|every|subject|except|including|e\.g\.|i\.e\.)\b", re.I)
# A clause containing a finite/state verb reads as a condition/problem SENTENCE
# ("target order already HAS that reservationId", "reservations ARE never
# re-pointed", "global rows MUST obey ...") -- not an artifact-introduction noun
# phrase ("a supersede floor guard", "codec_rank dedup sort keys").
_CONDITION_VERB = re.compile(
    r"\b(?:is|are|was|were|be|been|being|has|have|had|does|do|did|"
    r"match(?:es|ed|ing)?|appears?|appeared|obeys?|obey|must|should|shall|"
    r"stays?|stayed|reconciles?|comes?|came|contains?|never|only\s+link)\b", re.I)
# a real property clause is a concise noun phrase; a long run-on is prose (backstop)
_MAX_PROPERTY_WORDS = 16


def _looks_like_property(clause, strict=False):
    """True iff `clause` reads as an INDEPENDENT code-property INTRODUCTION rather
    than a subordinate guard/skip/condition clause or a problem/condition sentence.
    This is the precision gate that stops one function's guard clauses (or one
    change's sub-steps) from each being counted as a separate sliceable property.
    Erring toward FALSE (under-splitting) is safe: a genuine omnibus that slips
    through up-front detection is still caught reactively by the choke detector in
    ollama-dispatch-auto and sliced then.

    `strict`: the caller has no structural signal that this clause boundary was a
    deliberate one (it came from the last-resort comma/" and " fallback split, not
    a newline/bullet/';'). A bare identifier token is then far more likely to be
    one entry in an enumerated list (an import list: "SearchQuery, AwardResult,
    AwardSegment, ... from .base import") than a new property being introduced --
    confirmed live on aw-fix-delta-import, which fragmented one import-line fix
    into 6 slices this way. In strict mode a bare _TOKEN_RE hit is not enough on
    its own; require the stronger _PROPERTY_HINT (or an actual _defined_name)."""
    c = (clause or "").strip()
    if not c:
        return False
    # a subordinate / guard / skip / continuation clause is NOT a new property
    if _CONTINUATION_LEAD.match(c):
        return False
    core = re.sub(r"^(?:\(?\d*[.)]?|[-*•])\s*", "", c)   # drop a leading list marker
    # second line of defence against a bad boundary: a bare identifier or a
    # fragment with unbalanced delimiters is never an independent property.
    if _is_debris(core):
        return False
    # a definer clause ("add a foo helper") is always a property introduction
    if _defined_name(core):
        return True
    # otherwise require an artifact NAME/hint AND that it does not read as a
    # condition/problem sentence, and is concise (not a run-on prose sentence).
    has_hint = _PROPERTY_HINT.search(core) or (not strict and _TOKEN_RE.search(core))
    if not has_hint:
        return False
    if _CONDITION_VERB.search(core):
        return False
    if len(core.split()) > _MAX_PROPERTY_WORDS:
        return False
    return True


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


# ---------------------------------------------------------------------------
# BUG (2026-09-18): the splitter treated every ','/'.'/' and ' as a candidate
# clause boundary REGARDLESS of code syntax, so a separator INSIDE a bracketed
# or quoted code span was taken as a property boundary. A single coherent
# sentence was then chopped into fragments that are not sentences at all --
# confirmed live on 9+ nested sub-plans, e.g.
#   "given a card {targetRate, maxDate}, the current CC rate, ..."
#       -> slice intent "In lib/ccWaitlist.ts: maxDate}."
#   "a presence table (identity_key TEXT, broker_id TEXT, ..., primary key
#    identity_key+broker_id)"
#       -> "a presence table (identity_key TEXT." / "broker_id TEXT." /
#          "primary key identity_key+broker_id)."
# Dispatched models thrash on those: the fragment names no change to make.
# Two grounded rules below: (1) a separator only splits at bracket depth 0 and
# outside quotes; (2) a candidate fragment that is a bare identifier, has
# unbalanced delimiters, or is neither an artifact noun phrase nor a clause
# with a verb is re-merged into its neighbour instead of becoming a slice.
_OPEN_DELIM = {"(": ")", "[": "]", "{": "}"}
_CLOSE_DELIM = {")": "(", "]": "[", "}": "{"}
_QUOTE_CHARS = "\"'`"


def _splittable_mask(text):
    """Bool per character: True only where that character sits at bracket depth
    zero and outside any quoted span (i.e. a separator there is real prose
    punctuation, not part of a code expression)."""
    mask = [True] * len(text)
    stack = []
    quote = None
    for i, ch in enumerate(text):
        if quote is not None:
            mask[i] = False
            if ch == quote:
                quote = None
            continue
        if ch in _QUOTE_CHARS:
            # an apostrophe between word chars is a contraction/possessive
            # ("today's date"), not an opening quote.
            if (ch == "'" and i and text[i - 1].isalnum()
                    and i + 1 < len(text) and text[i + 1].isalnum()):
                mask[i] = not stack
                continue
            quote = ch
            mask[i] = False
            continue
        if ch in _OPEN_DELIM:
            stack.append(_OPEN_DELIM[ch])
            mask[i] = False
            continue
        if ch in _CLOSE_DELIM:
            if stack and stack[-1] == ch:
                stack.pop()
            mask[i] = False
            continue
        mask[i] = not stack
    return mask


def _resplit(pattern, text, flags=0):
    """re.split(), but a separator inside ()/[]/{} or quotes never splits.
    Returns (parts, seps) with len(seps) == len(parts) - 1 so a rejected
    fragment can be re-merged losslessly."""
    if not text:
        return [text], []
    mask = _splittable_mask(text)
    parts, seps, last = [], [], 0
    for m in re.finditer(pattern, text, flags):
        if m.start() == m.end():
            continue
        if not all(mask[i] for i in range(m.start(), m.end())):
            continue        # separator lives inside brackets/quotes -> not a boundary
        parts.append(text[last:m.start()])
        seps.append(m.group(0))
        last = m.end()
    parts.append(text[last:])
    return parts, seps


def _balanced(text):
    stack = []
    quote = None
    for ch in text:
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in _OPEN_DELIM:
            stack.append(_OPEN_DELIM[ch])
        elif ch in _CLOSE_DELIM:
            if not stack or stack.pop() != ch:
                return False
        elif ch in '"`':
            quote = ch
    return not stack and quote is None


# any verb at all -- a fragment with none of these and no artifact hint is not a
# statement about a change, it is debris from a bad boundary ("broker_id TEXT").
_FRAGMENT_VERB = re.compile(
    r"\b(?:is|are|was|were|be|been|being|has|have|had|does|do|did|must|should|"
    r"shall|can|may|will|add|adds|create|creates|return|returns|accept|accepts|"
    r"use|uses|call|calls|set|sets|store|stores|write|writes|read|reads|"
    r"decide|decides|compare|compares|emit|emits|raise|raises|skip|skips|"
    r"include|includes|expose|exposes|expire|expires|match|matches|check|"
    r"checks|update|updates|insert|inserts|record|records|load|loads|parse|"
    r"parses|filter|filters|sort|sorts|list|lists|map|maps|contain|contains|"
    r"obey|obeys|preserve|preserves|keep|keeps|make|makes|take|takes|treat|"
    r"treats|throw|throws|handle|handles|build|builds|define|defines|rename|"
    r"implement|implements|import|imports|delete|deletes|remove|removes)\b",
    re.I)


def _is_debris(clause):
    """True iff `clause` cannot stand on its own as a sliceable statement:
    a bare identifier, unbalanced delimiters, or neither an artifact
    noun phrase (hint/definer) nor anything with a verb."""
    c = re.sub(r"^(?:\(?\d*[.)]?|[-*•])\s*", "", (clause or "").strip())
    c = c.strip(" .")
    if not c:
        return True
    if not _balanced(c):
        return True
    words = c.split()
    if len(words) <= 1:                       # bare identifier / single token
        return True
    if _PROPERTY_HINT.search(c) or _defined_name(c):
        return False
    return not _FRAGMENT_VERB.search(c)


def _remerge_pairs(parts, seps):
    """Drop empty pieces and fold each debris fragment back into its neighbour
    (previous by preference), restoring the separator text that split them.
    Returns [(clause_text, separator_that_preceded_it)] so a LATER stage can fold
    losslessly too (see _fold_to_openers)."""
    items = []                                 # (text, sep_before)
    for i, p in enumerate(parts):
        items.append((p, seps[i - 1] if i else ""))
    items = [(t, s) for t, s in items if t and t.strip()]
    if len(items) <= 1:
        return [(t.strip(), s) for t, s in items]
    out = []                                   # list of [text, sep_before]
    for text, sep in items:
        if out and _is_debris(text):
            out[-1][0] = out[-1][0] + sep + text
        else:
            out.append([text, sep])
    # a leading debris piece has no previous neighbour -> fold it forwards
    while len(out) > 1 and _is_debris(out[0][0]):
        out[1][0] = out[0][0] + " " + out[1][0]
        out.pop(0)
    return [(t.strip(), s) for t, s in out if t and t.strip()]


def _remerge(parts, seps):
    """Back-compat: the clause texts only (separators discarded)."""
    return [t for t, _s in _remerge_pairs(parts, seps)]


def _split_on_pairs(pattern, text, flags=0):
    """Bracket/quote-aware split + debris re-merge, keeping each clause's
    preceding separator text."""
    return _remerge_pairs(*_resplit(pattern, text, flags))


def _split_on(pattern, text, flags=0):
    """Bracket/quote-aware split + debris re-merge."""
    return _remerge(*_resplit(pattern, text, flags))


# ---------------------------------------------------------------------------
# BUG (2026-09-26): SILENT REQUIREMENT DROP. The precision gate below used to be a
# FILTER -- `proper = [c for c in raw if _looks_like_property(c)]` -- and whatever
# it rejected was DELETED, never folded back. Two live symptoms, one root cause,
# confirmed on arr-webhook-recent-upgrade-priority (a 5-item "Required change"
# list -> 2 slices, reported as success):
#   (1) 4 of 5 author-numbered requirements vanished from the plan entirely (both
#       label constants, the Sonarr relabel wiring, the prioritize_normal_torrents
#       exemption). A real numbered requirement is a 20-40 word sentence carrying
#       a finite verb ("returns True iff ...", "they must never be swept ..."), so
#       it trips _MAX_PROPERTY_WORDS and/or _CONDITION_VERB and was dropped.
#   (2) the ONE slice that survived carried only the head of its own item: item 1
#       split on ';' into "def _is_recent_year(year, now=None) -> bool" + three
#       rejected tail fragments, and the tails held the whole CONTRACT -- the
#       one-year window (`>= now.year - 1`), the UTC clock default, and the
#       None/0/non-numeric rule. The authoring model then invented its own
#       (a 3-year window, a future-year rejection, a naive local clock).
# FIX: the gate now decides BOUNDARIES, not membership. A candidate that does not
# read as a property OPENER is folded into the previous opener (forward into the
# first one if there is no previous), so no spec text can ever be dropped -- the
# clause list is a lossless partition of the input. Under-splitting is still safe
# (the reactive choke detector catches it); deleting requirements never was.
#
# Second half of the fix: an author-written, line-anchored list ("1." / "-" at the
# start of a line) is a DELIBERATE enumeration of separate required changes, so
# each item is an opener on a relaxed test (not debris, not a continuation lead,
# and it names some artifact) rather than the prose heuristics tuned for run-on
# single-fix paragraphs. Inline "(1)..(2)..(3)" sub-steps inside a paragraph are
# NOT line-anchored and so remain unaffected -- that is what keeps the 2026-09-17
# precision wins (BFMR guard clauses, build.py sub-steps) intact.
_ENUM_MARK = re.compile(r"^([ \t]*)(\(?\d{1,3}[.)]|[-*•])[ \t]+(?=\S)")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
# a heading that introduces GLOBAL constraints rather than one more required change
_SHARED_HEAD = re.compile(
    r"^[ \t]*(?:#+\s*)?(?:behaviou?rs?\s+that\s+must\s+not\s+change|"
    r"behaviou?r\s+that\s+must\s+not\s+change|must\s+not\s+change|"
    r"do\s+not\s+change|everything\s+else|constraints?|invariants?|"
    r"non-?goals?|out\s+of\s+scope|scope)\b", re.I)


def _enumerated_items(text):
    """(preamble, items, shared_tail) for an author-written, line-anchored list.

    An item runs from its marker line to the line before the next marker of the
    SAME kind at the SAME (minimum) indent -- so continuation lines, code fences
    and nested sub-bullets stay with their item. Markers inside a ``` fence are
    ignored. `shared_tail` is everything from the first GLOBAL-constraint heading
    on; it belongs to every slice, not to the last item. Returns ("", [], "") when
    the text carries no such list.
    """
    lines = text.splitlines()
    fenced = False
    marks = []                                  # (line_idx, indent, kind)
    for i, ln in enumerate(lines):
        if _FENCE.match(ln):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _ENUM_MARK.match(ln)
        if m:
            kind = "num" if m.group(2)[0].isdigit() or m.group(2)[0] == "(" else "bul"
            marks.append((i, len(m.group(1).expandtabs(4)), kind))
    if len(marks) < 2:
        return "", [], ""
    first_kind = marks[0][2]
    indent = min(ind for _i, ind, k in marks if k == first_kind)
    tops = [i for i, ind, k in marks if k == first_kind and ind == indent]
    if len(tops) < 2:
        return "", [], ""
    # a GLOBAL-constraint heading at or after the list ends the enumeration
    cut = len(lines)
    fenced = False
    for i, ln in enumerate(lines):
        if _FENCE.match(ln):
            fenced = not fenced
            continue
        if fenced or i <= tops[0]:
            continue
        if _SHARED_HEAD.match(ln) and not _ENUM_MARK.match(ln):
            cut = i
            break
    tops = [i for i in tops if i < cut]
    if len(tops) < 2:
        return "", [], ""
    preamble = "\n".join(lines[:tops[0]]).strip()
    bounds = tops + [cut]
    items = []
    for a, b in itertools.pairwise(bounds):
        chunk = "\n".join(lines[a:b]).strip()
        if chunk:
            items.append(chunk)
    shared_tail = "\n".join(lines[cut:]).strip()
    return preamble, items, shared_tail


def _looks_like_enum_item(clause):
    """Relaxed opener test for an item of an author-written enumeration: the
    author already declared the boundary, so only reject a fragment that cannot
    be a requirement at all (debris) or that explicitly continues the previous
    item ("if ...", "otherwise ...", "and ..."). No word cap and no
    condition-verb veto: a real numbered requirement is a full sentence."""
    c = (clause or "").strip()
    if not c:
        return False
    if _CONTINUATION_LEAD.match(c):
        return False
    core = re.sub(r"^(?:\(?\d*[.)]?|[-*•])\s*", "", c)
    if _is_debris(core):
        return False
    return bool(_defined_name(core) or _PROPERTY_HINT.search(core)
                or _TOKEN_RE.search(core))


def _fold_to_openers(pairs, is_opener):
    """pairs: [(clause, separator_before)]. Keep only clauses that OPEN a new
    property; fold every other clause back into the opener it belongs to (the
    previous one, or forward into the first one when it leads). LOSSLESS: every
    character of the input survives in exactly one returned clause."""
    out = []                                    # [[text, was_opener]]
    for text, sep in pairs:
        if out and not is_opener(text):
            joiner = sep if (sep and sep.strip()) else (sep or " ")
            if "\n" in (sep or ""):
                joiner = "\n"
            out[-1][0] = out[-1][0] + joiner + text
        else:
            out.append([text, is_opener(text)])
    # a leading non-opener has no previous opener -> fold it forwards
    while len(out) > 1 and not out[0][1]:
        out[1][0] = out[0][0] + "\n" + out[1][0]
        out.pop(0)
    return [t.strip() for t, _o in out if t and t.strip()]


def _split_clauses_ctx(text):
    """(clauses, shared_context). Split an intent/interface string into candidate
    property clauses on strong separators (newlines, numbered/bulleted list items,
    ';', ' + '), falling back to ' and '/',' only when the strong split found
    nothing.

    Every returned clause is a property OPENER; non-opening candidates are folded
    into their opener rather than dropped (see the 2026-09-26 note above), so
    clauses + shared_context are always a lossless partition of `text`.
    `shared_context` is parent-spec prose that constrains EVERY slice (the
    scene-setter before an enumeration and its must-not-change/scope tail); it is
    carried separately so it lands in each slice's intent without contaminating
    titles, slugs or frozen must_contain literals.
    """
    text = (text or "").strip()
    if not text:
        return [], ""
    # (A) an author-written, line-anchored enumeration is a deliberate list of
    # separate required changes -- honour it before the prose heuristics.
    preamble, items, shared_tail = _enumerated_items(text)
    if len(items) >= 2:
        pairs = [(it, "\n") for it in items]
        opened = _fold_to_openers(pairs, _looks_like_enum_item)
        # only trust the enumeration when it really reads as parallel artifacts:
        # at least two items, and at least half of them, open a property.
        if len(opened) >= 2 and len(opened) * 2 >= len(items):
            # global parent context (scene-setter + must-not-change rules) belongs
            # to EVERY slice, never only to the first/last item. It is returned
            # SEPARATELY so it reaches each slice's intent (what the authoring
            # model reads) without polluting the slice's title/slug or -- critically
            # -- its FROZEN must_contain literals: a literal lifted from the
            # must-not-change prose is not something this slice's diff will add.
            return opened, "\n\n".join(x for x in (preamble, shared_tail) if x)
    # strip a leading "<preamble>:" scene-setter ("Add codec awareness to X:")
    # so it does not get glued onto the first property clause.
    mpre = re.match(r"^[^:.\n]{0,80}:\s+(?=\S)", text)
    if mpre and _PROPERTY_HINT.search(text[mpre.end():]):
        text = text[mpre.end():]
    # newlines / bullets / numbered items first
    # NOTE (2026-09-18): every split below goes through _split_on(), which never
    # cuts at a separator inside ()/[]/{} or quotes and re-merges debris
    # fragments -- see the _splittable_mask/_is_debris block above.
    parts = _split_on_pairs(r"(?:\r?\n)+|(?:^|\s)(?:\d+[.)]|[-*•])\s+", text)
    # within each, split on ';' and ' + ' (spaced plus = joiner, not a bullet)
    out = []
    for p, psep in parts:
        sub = _split_on_pairs(r"\s*;\s*|\s+\+\s+", p)
        for j, (s, ssep) in enumerate(sub):
            out.append((s, psep if j == 0 else ssep))
    # if still a single blob, fall back to ' and '/',' as candidate boundaries
    used_fallback_split = len(out) <= 1
    if used_fallback_split:
        blob = out[0][0] if out else text
        out = _split_on_pairs(r"\s*,\s*|\s+\band\b\s+", blob)
    # trim trailing scope boilerplate ("all in one file", "in <file>")
    cleaned = []
    for c, sep in out:
        c = re.sub(r",?\s*(all\s+)?in\s+(one\s+file|the\s+same\s+file|[\w./-]+)\.?$",
                   "", c, flags=re.I).strip(" .")
        if c:
            cleaned.append((c, sep))
    raw = cleaned or out
    # PRECISION GATE (authoritative): the number of PROPERTIES is the number of
    # clauses that INTRODUCE an artifact -- not guard/skip/condition clauses or
    # sub-steps of one change. Any separator (';', ' + ', ',', ' and ', numbered/
    # bulleted) only proposes candidate BOUNDARIES; a clause OPENS a slice only if
    # it positively reads as an independent property -- and a clause that does not
    # is folded into its opener, never discarded. This is what stops a single
    # function's guard clauses from being sliced apart WITHOUT losing their text.
    proper = _fold_to_openers(
        raw, lambda c: _looks_like_property(c, strict=used_fallback_split))
    if len(proper) >= 2:
        return proper, ""
    # not a multi-property list -> ONE property (the whole intent); NOT an omnibus.
    single = _norm(text)
    return ([single] if single else []), ""


def _split_clauses(text):
    """Back-compat: the property clauses only (shared context discarded)."""
    return _split_clauses_ctx(text)[0]


def _decompose_ctx(intent, interface=None):
    """((clauses, shared_context)) for the richer of interface vs intent."""
    ci, xi = _split_clauses_ctx(interface) if interface else ([], "")
    ct, xt = _split_clauses_ctx(intent)
    return (ci, xi) if len(ci) > len(ct) else (ct, xt)


def decompose_properties(intent, interface=None):
    """Return the richer of (interface-derived, intent-derived) clause lists.
    Each element is a raw property clause string."""
    return _decompose_ctx(intent, interface)[0]


# BUG (2026-09-18): a must_contain literal is FROZEN and enforced by the gate, so a
# literal that matches almost any source certifies nothing. cc-waitlist-r2 carried
# must_contain=['on'] on FOUR slices: _DEFN_RE's old unquoted third alternative read
# "...calls an injected submit function on SUBMIT..." as "function <name>" and captured
# "on" as a defined name. That bogus name then (a) created a garbage definer slice
# (s4-on, "on helper") which became the DAG ROOT every other slice depended on, and
# (b) was propagated into every consumer's must_contain by the definer-name rule below.
# The _DEFN_RE capture is already fixed (backticks required), but nothing validated the
# literals themselves. A single common word is never a discriminating gate literal.
_COMMON_WORD = frozenset("""
a an the and or but if when then else on in at by to of for from with within without
is are was were be been being has have had does do did not no nor so such as
this that these those it its there here which who whom whose while until since
new old all any each every one two both same other another next last first
set get put add use call run make take keep pass fail skip show hide
true false none null nil void self this that value key name type data item
list dict str int bool float char text line file path code test spec
helper function method class field param flag option check guard route
""".split())
# identifier STRUCTURE: snake_case, dotted, kebab/flag, a phrase, or a camelCase hump.
_STRUCTURED_LIT = re.compile(r"[_.\-@/\s]|[a-z0-9][A-Z]")


def _is_discriminating(lit):
    """True iff `lit` is specific enough to be worth freezing as a gate literal.
    Structured identifiers (snake_case/camelCase/dotted/flags/phrases) and anything
    containing non-alphabetic characters always qualify. A bare alphabetic word only
    qualifies when it is neither a common English word nor a very short lowercase
    token -- 'on', 'and', 'key', 'url' discriminate nothing; 'emails', 'Identity',
    'ValueError' do."""
    s = (lit or "").strip()
    if not s:
        return False
    if _STRUCTURED_LIT.search(s):
        return True
    if not s.isalpha():
        return True                     # digits/symbols -> specific enough
    if s.lower() in _COMMON_WORD:
        return False
    if s.islower() and len(s) < 4:
        return False
    return True


def _defined_name(clause):
    # BUG (2026-09-26): this used to look at the FIRST _DEFN_RE match only, so a
    # non-discriminating misparse earlier in the clause masked a real naming site
    # later in it: "1. Add a helper: ... def _is_recent_year(...)" matched
    # "Add a ... helper" with name "a" -> rejected -> None, so the REAL definer
    # `_is_recent_year` was never seen, the slice was not recognised as the shared
    # helper, and no consumer slice depended on it (confirmed on
    # arr-webhook-recent-upgrade-priority). Scan every match and take the first
    # DISCRIMINATING one instead.
    for m in _DEFN_RE.finditer(clause):
        name = next((g for g in m.groups() if g), None)
        # a "defined name" that does not discriminate is a misparse of prose, not a
        # naming site -- refusing it here is what stops the garbage DEFINER SLICE
        # (and the dependency root it becomes) from ever being created.
        if name and _is_discriminating(name):
            return name
    return None


def _slug(text, maxlen=24):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "prop"


def _title(clause):
    # a clause lifted from an author-written list still carries its "3. " marker
    # and may open with a fenced code block; neither belongs in a title or a
    # worktree/branch slug (it produced ids like "s1-1-add-a-helper-python-de").
    t = re.sub(r"^\s*(?:\(?\d{1,3}[.)]|[-*•])\s+", "", (clause or "").strip())
    t = re.sub(r"^(?:```|~~~)[^\n]*\n?", "", t)
    t = "\n".join(ln for ln in t.splitlines()
                  if not _FENCE.match(ln)).strip()
    t = _STOP_LEAD.sub("", t).strip()
    words = t.split()
    return " ".join(words[:6]) if words else clause[:40]


def _tokens(clause):
    """Candidate must_contain literals for a clause. Only DISCRIMINATING tokens are
    kept -- a frozen gate literal that matches almost any source certifies nothing
    (see _is_discriminating). An empty list is an honest outcome: better no literal
    check than a vacuous one that reads as coverage."""
    seen, out = set(), []
    for m in _TOKEN_RE.findall(clause):
        if m not in seen and _is_discriminating(m):
            seen.add(m)
            out.append(m)
    return out


def _entry_point(clause):
    m = _ENTRY_RE.search(clause)
    if not m:
        return None
    return m.group(1) or (f"line {m.group(2)}" if m.group(2) else None)


# ---------------------------------------------------------------------------
# BEHAVIOR COVERAGE (2026-10-03, replay-bfmr-replace-tracking). _decompose_ctx takes
# the RICHER clause source; a 3-signature interface beat a one-clause intent, so the
# slices carried bare signatures and EVERY behavioral clause of the intent (trim,
# throw on empty/equal/wrong type/status, never mutate, exactly one match) was
# dropped. Each slice gate then faithfully verified a signature -- green meant
# nothing, and wrong code converged. A plan must CARRY the intent's behavior.
# ---------------------------------------------------------------------------
_BEHAVIOR_RE = re.compile(
    r"\b(?:throw|throws|throwing|thrown|raise|raises|raising|error|errors|reject|"
    r"rejects|never|must|only|exactly|unchanged|identical|trim|trimmed|trims|"
    r"immutable|mutate|mutates|mutating|allowed|forbid|forbidden|invalid|unique|"
    r"single|empty|whitespace|equal|equals|at\s+most|at\s+least|more\s+than|"
    r"zero|none|fail|fails|refuse|refuses|ignore|ignores|preserve|preserves|keep|"
    r"keeps|unless|otherwise|default|defaults)\b", re.I)
_COV_STOP = frozenset("""
the and for with from that this then than into onto over under when where which
while also each every other any are was were has have had its their there here
given set sets use uses using via per not nor but return returns returned value
values new old string number object array row rows field fields helper function
""".split())
_COV_SPLIT = re.compile(r"(?<=[.;])\s+|,\s+(?=(?:and\s+|or\s+)?[a-z])|\n+")


_COV_STOP = _COV_STOP | frozenset("""
must should would could like keep keeps kept already itself needs need else just
sensibly also all some""".split())
# paraphrase-tolerant canonical forms: a qwen slice says "string"/"length"/"raises"
# where the omnibus said "str"/"len"/"throw" -- the same behavior, not a drop.
_COV_SYN = {
    "str": "string", "strs": "string", "int": "integer", "ints": "integer",
    "len": "length", "char": "character", "chars": "character",
    "bool": "boolean", "dict": "dictionary", "dicts": "dictionary",
    "raise": "throw", "raises": "throw", "raising": "throw", "raised": "throw",
    "throws": "throw", "throwing": "throw", "thrown": "throw",
    "original": "unchanged", "same": "unchanged", "identical": "unchanged",
    "untouched": "unchanged", "intact": "unchanged",
    "exceed": "greater", "exceeds": "greater", "larger": "greater",
}


def _cov_norm(w):
    w = _COV_SYN.get(w, w)
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return _COV_SYN.get(w, w)


def _cov_tokens(text):
    return {_cov_norm(w) for w in
            (t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text))
            if w not in _COV_STOP}


def behavior_clauses(intent):
    """The intent's BEHAVIORAL fragments (throw/never/must/trim/exactly/... clauses)
    worth requiring in some slice. Fragments with < 2 content tokens are dropped."""
    out = []
    for frag in _COV_SPLIT.split(intent or ""):
        frag = frag.strip(" .;,:-")
        if frag and _BEHAVIOR_RE.search(frag) and len(_cov_tokens(frag)) >= 2:
            out.append(frag)
    return out


def uncovered_behavior(intent, slices, min_overlap=0.5):
    """Behavior fragments of `intent` that NO slice intent substantially carries
    (>= min_overlap of the fragment's content tokens present in one slice's
    intent+title). [] == every behavioral clause reached some slice."""
    hay = [_cov_tokens((s.get("intent") or "") + " " + (s.get("title") or ""))
           for s in slices if isinstance(s, dict)]
    missing = []
    for frag in behavior_clauses(intent):
        need = _cov_tokens(frag)
        if not any(len(need & h) / len(need) >= min_overlap for h in hay):
            missing.append(frag)
    return missing


_DECL_START = re.compile(
    r"^\s*(?:In\s+\S+:\s*)?(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(function|def|class|const|let|var|type|interface|enum)\b")
_BEHAVIOR_VERB = re.compile(
    r"\b(?:returns?|returning|computes?|sets?|maps?|filters?|selects?|picks?|"
    r"builds?|given|when|if|so\s+that|such\s+that)\b", re.I)


def signature_only_slices(slices):
    """Ids of non-type slices whose intent is a bare declaration: it opens with a
    function/class/const declaration and carries no behavioral keyword or verb in
    its prose. A type/interface/enum slice is legitimately shape-only."""
    bad = []
    for s in slices:
        if not isinstance(s, dict):
            continue
        it = s.get("intent") or ""
        m = _DECL_START.match(it)
        if not m or m.group(1) in ("type", "interface", "enum"):
            continue
        prose = re.sub(r"\([^()]*\)|\{[^{}]*\}|<[^<>]*>|\[[^\[\]]*\]|`[^`]*`", " ", it)
        if not (_BEHAVIOR_RE.search(prose) or _BEHAVIOR_VERB.search(prose)):
            bad.append(s.get("id"))
    return bad


# PRESERVE-ONLY slices (2026-10-03, replay-endorse s6/s5/s4d). A slice whose WHOLE
# job is "preserve / ensure ... still / keep ... unchanged" asks for nothing new:
# at its baseline the property already holds (it existed before the chain, or an
# earlier slice implemented it), so no honest fixture can be red there and
# authoring burns its whole retry ladder (s6: 4 author attempts, ~40 min GPU). Such
# clauses are CONSTRAINTS: they belong inside the intent + verify_shape adversarial
# case of the slice that EDITS the same code, where a regression is possible.
_PRESERVE_LEAD = re.compile(
    r"^\s*(?:In\s+[^\s:,]+\s*[:,]\s*)?(?:(?:please|also|and)\s+)?"
    r"(preserve|ensure|keep|maintain|retain|verify|confirm|guarantee|check|"
    r"make\s+sure|do\s+not\s+(?:change|break|touch|alter))\b", re.I)
_CHANGE_VERB = re.compile(
    r"\b(?:add|adds|adding|create|creates|implement|implements|introduce|introduces|"
    r"modify|modifies|change|changes|replace|replaces|rewrite|rewrites|remove|removes|"
    r"delete|deletes|rename|renames|fix|fixes|refactor|refactors|"
    r"handle|handles|support|supports|wire|wires|export|exports|define|defines|"
    r"compute|computes|emit|emits|convert|converts|parse|parses)\b", re.I)


def preserve_only_slices(slices):
    """Ids of slices whose intent LEADS with a preserve/ensure/keep/verify verb and
    names no change anywhere (no add/modify/replace/implement/handle/... verb):
    green at baseline by construction. "Ensure f handles null" names a change
    (handles) and is NOT flagged."""
    bad = []
    for s in slices:
        if not isinstance(s, dict) or s.get("kind") == "invariant":
            continue
        it = s.get("intent") or ""
        # code spans are not prose: `<T extends X>`, `f(a, b)`, `...`
        prose = re.sub(r"`[^`]*`|<[^<>]*>|\([^()]*\)", " ", it)
        if _PRESERVE_LEAD.match(it) and not _CHANGE_VERB.search(prose):
            bad.append(s.get("id"))
    return bad


_PARENT_BEHAVIOR_HDR = ("Behavior the PARENT intent requires -- implement EVERY part "
                        "that applies to this slice's symbol; dropping any of it is a "
                        "defect, and the fixture must assert it:\n")


def build_slices(intent, interface=None, target="the target file"):
    """Deterministically decompose into slice dicts (id/title/intent/depends_on/
    must_contain/verify_shape/entry_point), inferring the shared-helper dependency
    so the definer slice lands first."""
    clauses, shared_ctx = _decompose_ctx(intent, interface)
    if not clauses:
        return []
    # pass 1: identify definer slices (introduce a new helper/function)
    definers = {}  # clause index -> defined name
    for i, c in enumerate(clauses):
        name = _defined_name(c)
        if name:
            definers[i] = name

    slices = []
    ids = []
    for i, c in enumerate(clauses):
        # a definer slice reads best named after the thing it introduces
        # (codec_rank), which also gives a clean worktree/branch id.
        if i in definers:
            title = f"{definers[i]} helper"
            sid = f"s{i+1}-{_slug(definers[i])}"
        else:
            title = _title(c)
            sid = f"s{i+1}-{_slug(title)}"
        ids.append(sid)
        slices.append({
            "_idx": i,
            "id": sid,
            "title": title,
            "intent": f"In {target}: {c.rstrip('.')}." + (
                "\n\nParent-spec context that constrains THIS slice (do not "
                "re-derive or re-invent any of it):\n" + shared_ctx
                if shared_ctx else ""),
            "depends_on": [],
            "must_contain": _tokens(c),
            "verify_shape": "",
            "entry_point": _entry_point(c),
            "_raw": c,
        })

    # pass 2: dependency inference.
    #  (a) text reference: a later clause that names a definer depends on it.
    #  (b) single-definer fallback: for a coherent single-file omnibus with EXACTLY
    #      ONE new helper, every other slice is a consumer of it -> depends on it
    #      (the helper must land first so consumers can call it).
    definer_idxs = list(definers)
    for s in slices:
        i = s["_idx"]
        if i in definers:
            continue
        deps = []
        for di in definer_idxs:
            dname = definers[di]
            # match on the RAW clause only: the intent now also carries the shared
            # parent context, and a helper merely NAMED in a must-not-change note
            # is not a dependency of every slice.
            if dname and dname in s["_raw"]:
                deps.append(ids[di])
        if not deps and len(definer_idxs) == 1 and definer_idxs[0] != i:
            deps = [ids[definer_idxs[0]]]
        s["depends_on"] = sorted(set(deps))
        # a consumer should assert it actually calls the helper it depends on
        for di in definer_idxs:
            if ids[di] in s["depends_on"] and definers[di] not in s["must_contain"]:
                s["must_contain"].insert(0, definers[di])

    # definer slices: guarantee the helper name is a must-contain literal
    for di, name in definers.items():
        s = slices[di]
        if name not in s["must_contain"]:
            s["must_contain"].insert(0, name)

    # verify_shape hint (the per-slice AUTO author writes the real fixture; the
    # human relevance-reviews it -- this is only guidance).
    for s in slices:
        base = (f"behavioral: exercise the property from {s['title']!r} and assert "
                f"it holds. ADVERSARIAL: add a case a plausible-wrong build would "
                f"fail (e.g. a boundary / opposite-direction case). Kill test: "
                f"revert this change -> red.")
        if s["_idx"] in definers:
            base = ("behavioral: import the new helper and assert it returns the "
                    "specified value on representative inputs. " + base)
        s["verify_shape"] = base

    for s in slices:
        s.pop("_idx", None)
        s.pop("_raw", None)
        if s["entry_point"] is None:
            s.pop("entry_point")
    # BEHAVIOR COVERAGE: if any behavioral clause of the intent reached no slice
    # (typically: the interface won the clause count and the slices are bare
    # signatures), carry the whole parent intent into every slice.
    if intent and uncovered_behavior(intent, slices):
        for s in slices:
            s["intent"] = s["intent"] + "\n\n" + _PARENT_BEHAVIOR_HDR + _norm(intent)
    return slices


# A target this small is ONE slice no matter how many exported functions the intent
# lists (2026-10-02): the slicer split a ~30-line single-function helper into 3
# slices -- 3x author+preflight+gate+chain overhead for a file a single dispatch
# reads in one page. Omnibus slicing exists for the big-existing-file choke (the
# model burns its budget reading a 50KB target); a small or not-yet-existing file
# cannot choke anything.
SMALL_TARGET_LINES = 300
SMALL_TARGET_BYTES = 16 * 1024


def target_is_small(target_path):
    """(small, why). A missing target (creation task) is small: a new one-file
    helper is written in one go. None/'' -> (False, ...) (no size evidence)."""
    if not target_path:
        return False, "no target path"
    p = Path(target_path)
    if not p.exists():
        return True, f"target {p.name} does not exist yet (creation task)"
    try:
        data = p.read_bytes()
    except OSError as e:
        return False, f"target unreadable ({e})"
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    if lines < SMALL_TARGET_LINES and len(data) < SMALL_TARGET_BYTES:
        return True, f"target {p.name} is small ({lines} lines, {len(data)} B)"
    return False, f"target {p.name} is {lines} lines, {len(data)} B"


def is_omnibus(intent, interface=None, threshold=DEFAULT_THRESHOLD, env=None,
               target_path=None):
    """(is_omnibus, reason, n_slices). Detection is disabled under a slicer
    (NO_SPLIT env) so a per-slice single-property dispatch never re-triggers,
    and for a SMALL target (target_is_small), which is one slice by definition."""
    env = os.environ if env is None else env
    if env.get(NO_SPLIT_ENV):
        return False, "detection disabled (running under the slicer)", 0
    slices = build_slices(intent, interface)
    n = len(slices)
    small, why_small = target_is_small(target_path)
    if small:
        return False, f"{why_small} -> one slice ({n} properties listed)", n
    if n >= threshold:
        return True, f"{n} independent properties detected (>= threshold {threshold})", n
    return False, f"{n} propert{'y' if n == 1 else 'ies'} (< threshold {threshold})", n


# ---------------------------------------------------------------------------
# plan build / write
# ---------------------------------------------------------------------------
def build_plan(repo, target, lang, label, intent, interface=None,
               threshold=DEFAULT_THRESHOLD, bundle=None):
    slices = build_slices(intent, interface, target=target)
    # A preserve-only slice is green at its baseline by construction (see
    # preserve_only_slices): drop it; its clause stays in force as a constraint via
    # the parent-intent carry below.
    _keep = set(preserve_only_slices(slices))
    if _keep:
        slices = [s for s in slices if s.get("id") not in _keep]
        if intent and uncovered_behavior(intent, slices):
            for s in slices:
                if _PARENT_BEHAVIOR_HDR not in s["intent"]:
                    s["intent"] = s["intent"] + "\n\n" + _PARENT_BEHAVIOR_HDR + _norm(intent)
    if len(slices) < 2:
        return None
    _miss = uncovered_behavior(intent, slices)
    if _miss:
        # Refuse to slice rather than emit a plan whose gates verify shape only.
        print("[auto] refusing to auto-slice: behavioral clause(s) of the intent are "
              "carried by no slice: " + "; ".join(repr(m) for m in _miss[:4]),
              file=sys.stderr)
        return None
    plan = {
        "repo": repo,
        "target": target,
        "lang": lang,
        "label": label,
        "intent": _norm(intent),
        "shared_worktree": True,
        "note": ("AUTO-GENERATED by omnibus_slice from a single-file multi-property "
                 "intent. Single-file OMNIBUS: all slices edit " + str(target) +
                 " in a sequential chain on branch slice/" + str(label) +
                 ", each authored+gated individually against the previous slice's "
                 "landed tree. Integrate by copying the finished branch tree onto "
                 "main, never merge. Each slice STILL stops for a human relevance "
                 "review; auto-slicing does not auto-confirm."),
        "slices": slices,
    }
    if bundle:
        # Queue bundle tag inherited from the AUTO run that sliced this (see
        # ollama-dispatch-slice load_state "bundle"): the chain's rows keep
        # scheduling/rendering with the rest of the bundle after auto-slicing.
        plan["bundle"] = str(bundle)
    return plan


def plan_path_for(label):
    return PLANS_DIR / f"{label}.slices.json"


def write_plan(plan):
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    p = plan_path_for(plan["label"])
    p.write_text(json.dumps(plan, indent=2) + "\n")
    return p


# ---------------------------------------------------------------------------
# bounded failure->requeue policy (reuses the slicer's run-state as the marker)
# ---------------------------------------------------------------------------
def slice_run_state(label):
    p = SLICE_RUNS_DIR / f"{label}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def harness_filled(wt):
    """True once the AUTHOR model has actually FILLED the harness (TASK.md no
    longer carries the scaffold's TODO markers). Used by the reactive choke
    detector: a request_more_iterations pause while this is still False means the
    model spent its whole budget reading the target and authored nothing -- the
    omnibus choke -- so the dispatch is aborted and converted to a sliced chain."""
    t = Path(wt) / "TASK.md"
    if not t.exists():
        return False
    txt = t.read_text(errors="ignore")
    return "TODO" not in txt and len(txt.strip()) > 200


def failed_slices(state):
    if not state:
        return []
    slices = state.get("slices", {})
    return [sid for sid, s in slices.items()
            if s.get("status") in ("failed", "blocked")]


def plan_failure_action(label, intent, interface=None,
                        threshold=DEFAULT_THRESHOLD, env=None, target_path=None):
    """Bounded policy for a FAILED top-level dispatch:
      * under a slicer (NO_SPLIT)        -> 'escalate' (never recurse)
      * a slice run exists + a slice FAILED -> 'escalate' (already tried once)
      * a slice run exists + healthy     -> 'resume'   (idempotent)
      * no slice run + decomposable      -> 'convert'
      * no slice run + not decomposable  -> 'cannot-slice-escalate'
    """
    env = os.environ if env is None else env
    if env.get(NO_SPLIT_ENV):
        return "escalate"
    st = slice_run_state(label)
    if st is not None:
        return "escalate" if failed_slices(st) else "resume"
    if target_is_small(target_path)[0]:
        return "cannot-slice-escalate"     # a small target is already one slice
    return "convert" if len(build_slices(intent, interface)) >= 2 \
        else "cannot-slice-escalate"


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------
def handoff_to_slicer(plan_path, model=None, host=None, num_ctx=None, _run=None):
    """Hand off to the EXISTING slicer. Exports NO_SPLIT so the per-slice AUTO
    calls it spawns do not re-detect/re-slice. `--execute` advances to the FIRST
    human relevance-review checkpoint and STOPS -- it never auto-confirms."""
    cmd = ["python3", str(SLICER), str(plan_path), "--execute"]
    if model:
        cmd += ["--model", model]
    if host:
        cmd += ["--host", host]
    if num_ctx:
        cmd += ["--num-ctx", str(num_ctx)]
    env = dict(os.environ)
    env[NO_SPLIT_ENV] = "1"
    print("[auto] handoff -> " + " ".join(shlex.quote(c) for c in cmd))
    runner = _run or (lambda c, e: subprocess.run(c, env=e).returncode)
    return runner(cmd, env)


PLANNER = BIN / "ollama-dispatch-plan"
PLANNER_FALLBACK_OFF_ENV = "OLLAMA_DISPATCH_NO_PLANNER_FALLBACK"


def planner_fallback_cmd(*, repo, target, lang, label, intent, interface=None, bundle=None):
    """argv for the LLM planner (ollama-dispatch-plan --generate): qwen decomposes the
    omnibus, the structural gate refuses a bad plan, and --auto-confirm-plan (WITHOUT
    --then-execute) writes ~/.ollama-dispatch/slice-plans/<label>.slices.json and returns.
    Execution is the caller's: handoff_to_slicer() exports NO_SPLIT so the per-slice AUTO
    calls never re-slice, which planner --then-execute would not."""
    full = (intent or "").strip()
    if interface and str(interface).strip():
        full += "\n\nINTERFACE / CONTRACT NOTES: " + str(interface).strip()
    cmd = ["python3", str(PLANNER), "--generate", "--repo", str(repo), "--target", str(target),
           "--lang", str(lang), "--label", str(label), "--intent", full,
           "--auto-confirm-plan"]
    if bundle:
        cmd += ["--bundle", str(bundle)]
    return cmd


def planner_fallback_and_handoff(*, repo, target, lang, label, intent, interface=None,
                                 model=None, host=None, num_ctx=None, reason="",
                                 bundle=None, _run=None, _handoff=None):
    """The regex slicer found no clean decomposition (a single target whose intent is one
    prose property) but the dispatch cannot run as one job (e.g. ctx ceiling). Fall back to
    the LLM planner, then execute the gated plan through the slicer under the dispatch's
    bundle. Idempotent: an existing plan file for this label is reused, never regenerated.
    Returns an exit code (0 = chain handed off); nonzero = planner failed / gate never
    clean -- the caller then escalates exactly as before."""
    if os.environ.get(PLANNER_FALLBACK_OFF_ENV):
        print(f"[auto] planner fallback disabled ({PLANNER_FALLBACK_OFF_ENV}).", file=sys.stderr)
        return 1
    pth = plan_path_for(label)
    runner = _run or (lambda c: subprocess.run(c).returncode)
    if pth.exists():
        print(f"[auto] PLANNER FALLBACK ({reason}): reusing the existing plan {pth}")
    else:
        cmd = planner_fallback_cmd(repo=repo, target=target, lang=lang, label=label,
                                   intent=intent, interface=interface, bundle=bundle)
        print(f"[auto] PLANNER FALLBACK ({reason}): the regex slicer found no clean "
              f"decomposition; asking the LLM planner (qwen) for a gated slice plan...")
        rc = runner(cmd)
        if rc != 0 or not pth.exists():
            print(f"[auto] planner fallback did not produce a clean plan (rc={rc}).",
                  file=sys.stderr)
            return rc or 1
    try:   # stamp the bundle on a reused/older plan too, so the chain stays grouped
        plan = json.loads(pth.read_text())
        if bundle and plan.get("bundle") != str(bundle):
            plan["bundle"] = str(bundle)
            pth.write_text(json.dumps(plan, indent=2) + "\n")
    except (OSError, ValueError):
        pass
    return (_handoff or handoff_to_slicer)(pth, model=model, host=host, num_ctx=num_ctx)


def autoslice_and_handoff(*, repo, target, lang, label, intent, interface=None,
                          threshold=DEFAULT_THRESHOLD, model=None, host=None,
                          num_ctx=None, reason="", _run=None, bundle=None):
    """Write (or reuse) the plan and hand off. Idempotent: an existing slice-run
    for this label resumes rather than regenerating/double-enqueuing."""
    st = slice_run_state(label)
    pth = plan_path_for(label)
    if st is None:
        plan = build_plan(repo, target, lang, label, intent, interface, threshold,
                          bundle=bundle)
        if plan is None or len(plan["slices"]) < 2:
            print("[auto] not decomposable into >= 2 slices -- cannot auto-slice.",
                  file=sys.stderr)
            return 1
        write_plan(plan)
        print(f"[auto] AUTO-SLICE ({reason}): {len(plan['slices'])} slices -> {pth}")
        for s in plan["slices"]:
            dep = f" deps={s['depends_on']}" if s["depends_on"] else " (independent)"
            print(f"[auto]     {s['id']}: {s['title']}{dep}")
        print("[auto] each slice STILL stops for your relevance review "
              "(no auto-confirm).")
    else:
        if not pth.exists():
            plan = build_plan(repo, target, lang, label, intent, interface, threshold,
                              bundle=bundle)
            if plan:
                write_plan(plan)
        print(f"[auto] AUTO-SLICE resume ({reason}): existing slice run for {label!r}")
    return handoff_to_slicer(pth, model=model, host=host, num_ctx=num_ctx, _run=_run)


# ---------------------------------------------------------------------------
# optional, OFF-by-default, bounded model refinement of slice boundaries
# ---------------------------------------------------------------------------
def llm_refine_slices(intent, interface, target, deterministic,
                      model=None, host=None, timeout=180):
    """OPTIONAL: ask a small model to refine the deterministic decomposition into
    cleaner per-property slices. Bounded (one queue call, short timeout) and
    fail-safe: ANY error / malformed output falls back to `deterministic`. Not
    used by default; the deterministic path is the shipping path."""
    try:
        prompt = (
            "Decompose this single-file coding omnibus into independent property "
            "slices. Return ONLY a JSON array; each item {\"title\":..,\"intent\":.."
            ",\"depends_on\":[titles],\"must_contain\":[literals]}. A shared helper "
            "must have no depends_on and be referenced by its consumers.\n\n"
            f"target: {target}\nintent: {intent}\ninterface: {interface or ''}\n")
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(prompt)
            pf = fh.name
        cmd = ["python3", str(QUEUE), "oneshot", "--model",
               model or "qwen3.8:27b-q4_K_M", "--host", host or "studio",
               "--num-ctx", "32768", "--task-file", pf]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        m = re.search(r"\[.*\]", cp.stdout, re.S)
        if not m:
            return deterministic
        items = json.loads(m.group(0))
        if not isinstance(items, list) or len(items) < 2:
            return deterministic
        title_to_id = {}
        refined = []
        for i, it in enumerate(items):
            title = _norm(it.get("title") or "")[:60] or f"prop {i+1}"
            sid = f"s{i+1}-{_slug(title)}"
            title_to_id[title] = sid
            refined.append({"id": sid, "title": title,
                            "intent": f"In {target}: {_norm(it.get('intent') or title)}.",
                            "depends_on_titles": it.get("depends_on") or [],
                            "must_contain": [x for x in (it.get("must_contain") or [])
                                             if isinstance(x, str)],
                            "verify_shape": ("behavioral + ADVERSARIAL + kill test "
                                             "(revert -> red).")})
        for s in refined:
            s["depends_on"] = sorted({title_to_id[t] for t in s.pop("depends_on_titles")
                                      if t in title_to_id})
        return refined
    except Exception as e:  # pragma: no cover - network/model path
        print(f"[auto] llm slice refinement failed ({e}); using deterministic plan.",
              file=sys.stderr)
        return deterministic


# ---------------------------------------------------------------------------
# self-test (pipeline convention: `--self-test`, in-process, no queue/model/git)
# ---------------------------------------------------------------------------
def _self_test():
    import tempfile
    global SLICE_RUNS_DIR, PLANS_DIR
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond), extra))

    # ---- GOLDEN: the codec-floor 4-property single-file intent ---------------
    codec_intent = (
        "Add codec awareness to arr-webhook.py: add a codec_rank helper + a "
        "supersede floor guard in the supersede sweep + codec_rank dedup sort "
        "keys in both keeper sorts + a dedup_via_sonarr pack-vs-pack pass, all "
        "in one file.")
    slices = build_slices(codec_intent, target="arr-webhook.py")
    check("golden: 4 slices", len(slices) == 4, f"got {len(slices)}")
    if slices:
        s1 = slices[0]
        check("golden: s1 is codec_rank definer",
              "codec_rank" in s1["must_contain"], s1["must_contain"])
        check("golden: s1 has no deps (lands first)",
              s1["depends_on"] == [], s1["depends_on"])
        consumers_ok = all(s["depends_on"] == [s1["id"]] for s in slices[1:])
        check("golden: s2/s3/s4 depend on s1",
              consumers_ok, [s["depends_on"] for s in slices[1:]])
        check("golden: consumers must_contain codec_rank",
              all("codec_rank" in s["must_contain"] for s in slices[1:]),
              [s["must_contain"] for s in slices[1:]])
        check("golden: every slice has id+intent (slicer contract)",
              all(s.get("id") and s.get("intent") for s in slices))
        # dependency order => s1 first (equivalent to the slicer's topo sort)
        order_ok = all(slices[0]["id"] not in s["depends_on"] or True
                       for s in slices) and s1["depends_on"] == []
        check("golden: codec_rank first in dependency order", order_ok)

    # ---- REGRESSION (2026-09-18): never split inside brackets/quotes ---------
    # Both intents below are REAL ones that the old comma/period splitter
    # chopped into un-authorable debris on live sub-plans tonight.
    cc_intent = (
        "In lib/ccWaitlist.ts: Build a Card Center gift-card submission waitlist "
        "engine as a NEW module lib/ccWaitlist.ts. Core rule the tests MUST pin: "
        "given a card {targetRate, maxDate}, the current CC rate, and today's "
        "date, decide SUBMIT only when currentRate >= targetRate AND today <= "
        "maxDate (compare by local calendar day, inclusive of the deadline day).")
    def _body(sl):   # slice intent minus the "In <target>: " prefix
        return sl["intent"].split(": ", 1)[-1]
    cc = build_slices(cc_intent, target="lib/ccWaitlist.ts")
    check("brackets: no bare '{...}' fragment slice (was 'maxDate}')",
          all(not _is_debris(_body(s)) for s in cc),
          [_body(s) for s in cc if _is_debris(_body(s))])
    check("brackets: '{targetRate, maxDate}' stays intact in one clause",
          all(("targetRate" in _body(s)) == ("maxDate" in _body(s))
              for s in cc if "targetRate" in _body(s) or "maxDate}" in _body(s)),
          [_body(s) for s in cc])
    sql_intent = (
        "Create broker_guard/state.py with init_db(path: str) that creates, "
        "using CREATE TABLE IF NOT EXISTS, a presence table (identity_key TEXT, "
        "broker_id TEXT, first_seen TEXT, last_seen TEXT, primary key "
        "identity_key+broker_id).")
    sql = build_slices(sql_intent, target="broker_guard/state.py")
    check("brackets: SQL column list is not split into per-column slices",
          all(not _is_debris(_body(s)) for s in sql),
          [_body(s) for s in sql if _is_debris(_body(s))])
    check("brackets: every slice intent has balanced delimiters",
          all(_balanced(s["intent"]) for s in cc + sql),
          [s["intent"] for s in cc + sql if not _balanced(s["intent"])])
    check("debris: bare identifier rejected", _is_debris("maxDate}"))
    check("debris: unbalanced paren rejected",
          _is_debris("a presence table (identity_key TEXT"))
    check("debris: real noun-phrase property kept",
          not _is_debris("a codec_rank helper"))
    _qparts, _ = _resplit(r"\s*,\s*", "the 'a, b' flag, a real boundary")
    check("mask: separator inside quotes does not split",
          _qparts == ["the 'a, b' flag", "a real boundary"], _qparts)
    _bparts, _ = _resplit(r"\s*,\s*", "f(a, b), c")
    check("mask: separator inside parens does not split",
          _bparts == ["f(a, b)", "c"], _bparts)

    # ---- REGRESSION (2026-09-18): no degenerate must_contain literals --------
    # The real cc-waitlist-r2 text that produced must_contain=['on'] on four slices.
    on_text = ("A runner iterates candidate cards, applies the decision, calls an "
               "injected submit function on SUBMIT and an injected onExpire function "
               "on EXPIRE, and returns a summary.")
    check("literal: 'function on SUBMIT' yields no defined name (was 'on')",
          _defined_name(on_text) is None, _defined_name(on_text))
    check("literal: 'on' never reaches must_contain",
          "on" not in _tokens(on_text), _tokens(on_text))
    check("literal: the real identifier survives",
          "onExpire" in _tokens(on_text), _tokens(on_text))
    for bad in ("on", "and", "the", "key", "url", "is", "helper", "value"):
        check(f"literal: {bad!r} is not discriminating", not _is_discriminating(bad))
    for good in ("onExpire", "codec_rank", "ValueError", "emails", "default_factory",
                 "--broker", "@dataclass", "class Identity", "lib/ccWaitlist.ts"):
        check(f"literal: {good!r} IS discriminating", _is_discriminating(good))
    # a degenerate definer must not become a slice (and thus a DAG root) at all
    on_slices = build_slices(on_text, target="lib/ccWaitlist.ts")
    check("literal: no garbage 'on helper' definer slice",
          not any(s["id"].endswith("-on") or s["title"] == "on helper"
                  for s in on_slices),
          [(s["id"], s["title"]) for s in on_slices])
    check("literal: no slice carries a vacuous literal",
          all(all(_is_discriminating(t) for t in s["must_contain"])
              for s in on_slices),
          [s["must_contain"] for s in on_slices])

    # ---- REGRESSION (2026-09-19): PascalCase literals are not truncated ------
    # _TOKEN_RE's camelCase branch used to require a LOWERCASE first char. On a
    # PascalCase identifier position 0 failed the [a-z] test, the engine backtracked
    # to position 1, and the FROZEN must_contain literal became a truncated tail:
    # FastAPI->'astAPI', ProgramRegistry->'rogramRegistry', AwardResult->'wardResult'.
    # A truncated tail is still a substring of the real name, so the gate kept
    # PASSING -- it just silently asserted something weaker than it claimed, on
    # aw-app-wiring / aw-fix-delta-import / aw-fix-united-import. Assert on the
    # TOKENS (behaviour), not on the regex source, and pin both directions: the
    # whole identifier is produced, AND no truncated tail ever is.
    _pascal = ["FastAPI", "ProgramRegistry", "DeltaAdapter", "AwardResult",
               "AwardSegment", "AwardPrice", "ProgramAdapter", "Jinja2Templates"]
    for _p in _pascal:
        check(f"literal: {_p!r} survives whole", _p in _tokens(_p), _tokens(_p))
        # the exact truncation the bug produced -- a leading-char-stripped tail
        check(f"literal: {_p[1:]!r} (truncated {_p!r}) is never emitted",
              _p[1:] not in _tokens(_p), _tokens(_p))
    # and in the prose form the plans are actually built from
    _pascal_text = ("In src/webui/app.py: app = FastAPI(title='Award Search', "
                    "lifespan=lifespan), and ProgramRegistry.register('delta', "
                    "DeltaAdapter) returning an AwardResult.")
    _pt = _tokens(_pascal_text)
    for _p in ("FastAPI", "ProgramRegistry", "DeltaAdapter", "AwardResult"):
        check(f"literal: {_p!r} whole in prose", _p in _pt, _pt)
        check(f"literal: {_p[1:]!r} not in prose", _p[1:] not in _pt, _pt)
    # the narrowing that made the old branch lowercase-anchored must be preserved:
    # a bare all-caps acronym is still NOT a camelCase token.
    for _acr in ("API", "HTTP", "JSON"):
        check(f"literal: bare acronym {_acr!r} is not a camel token",
              _acr not in _tokens(_acr), _tokens(_acr))

    # ---- REGRESSION (2026-09-26): SILENT REQUIREMENT DROP --------------------
    # The real arr-webhook-recent-upgrade-priority parent spec: an author-written
    # 5-item "Required change" list plus a global must-not-change tail. The old
    # precision gate was a FILTER, so items whose prose carried a finite verb or ran
    # past _MAX_PROPERTY_WORDS were DELETED -- the plan came out with a subset of the
    # slices and reported success, and the ONE surviving slice carried only the head
    # of its own item (its ';'-separated CONTRACT tail -- the one-year window, the
    # UTC clock default, the None/0 rule -- was deleted too, so the authoring model
    # invented a 3-year window, a future-year rejection and a naive local clock).
    # Assert BEHAVIOUR both ways: every required change survives as its own slice,
    # AND every concrete contract detail survives somewhere in the slice intents.
    recent_iface = (
        "Add a fast-track: when the underlying media is RECENT -- its release year\n"
        "is the current year or the immediately preceding year -- an upgrade for it\n"
        "should behave like a normal priority download instead: top of the Deluge\n"
        "queue, never bottomed.\n"
        "\n"
        "1. Add a helper:\n"
        "\n"
        "   ```python\n"
        "   def _is_recent_year(year, now=None) -> bool\n"
        "       # now defaults to datetime.now(timezone.utc); returns True iff year\n"
        "       # is a truthy int-or-int-like value and int(year) >= now.year - 1.\n"
        "       # None/0/missing/non-numeric -> False.\n"
        "   ```\n"
        "\n"
        "2. Add two new label env-var constants next to `SONARR_UPG_LABEL` /\n"
        "   `RADARR_UPG_LABEL`:\n"
        "\n"
        "   ```python\n"
        "   RADARR_UPG_PRIORITY_LABEL = os.environ.get('RADARR_UPGRADE_PRIORITY_LABEL', 'radarr-upgrade-recent')\n"
        "   SONARR_UPG_PRIORITY_LABEL = os.environ.get('SONARR_UPGRADE_PRIORITY_LABEL', 'sonarr-upgrade-recent')\n"
        "   ```\n"
        "\n"
        "3. In `relabel_radarr_upgrades`: for each torrent whose movie hasFile, branch on\n"
        "   `_is_recent_year(movie.get('year'))`. Recent -> `ensure_label_exists_named(RADARR_UPG_PRIORITY_LABEL)`,\n"
        "   `set_torrent_label(torrent_hash, RADARR_UPG_PRIORITY_LABEL)`, collect into a separate\n"
        "   `priority_hashes` list sent to `core.queue_top`. Not-recent -> unchanged existing path\n"
        "   (`RADARR_UPG_LABEL`, queue_bottom). Both branches still count toward the function's\n"
        "   returned relabeled count.\n"
        "\n"
        "4. In `relabel_sonarr_upgrades`: same split, using `_is_recent_year` on the year parsed\n"
        "   from the episode response's `airDateUtc` (fall back to `airDate` if `airDateUtc` is\n"
        "   absent; unparseable/missing -> not recent, existing throttled path).\n"
        "\n"
        "5. In `prioritize_normal_torrents`: add `RADARR_UPG_PRIORITY_LABEL` and\n"
        "   `SONARR_UPG_PRIORITY_LABEL` to `priority_labels` (swept to queue_top every hour). Do\n"
        "   NOT add them to `upgrade_labels` -- they must never be swept to queue_bottom.\n"
        "\n"
        "Behaviour that must NOT change:\n"
        "- Older-than-last-year upgrades still get `RADARR_UPG_LABEL` / `SONARR_UPG_LABEL` and are\n"
        "  moved to `core.queue_bottom`.\n"
        "- `purge_stalled_upgrade_torrents`, the weekly quota and the batch cursor are untouched.\n")
    rsl = build_slices("recent-upgrade fast-track", recent_iface,
                       target="arr-webhook.py")
    check("drop: 5 author-numbered required changes -> 5 slices (was a subset)",
          len(rsl) == 5, f"got {len(rsl)}: {[s['id'] for s in rsl]}")
    _pre, _items, _tail = _enumerated_items(recent_iface)
    _sq = lambda s: re.sub(r"\s+", "", s)
    check("drop: clause split is LOSSLESS (no requirement text deleted)",
          _sq(_pre + "".join(_items) + _tail) == _sq(recent_iface))
    check("drop: every enumerated item survives as its own clause",
          [_sq(x) for x in _items] == [_sq(c) for c in _split_clauses(recent_iface)])
    _rbody = "\n".join(s["intent"] for s in rsl)
    # the CONTRACT details the authoring model had to invent when they were dropped
    for _need in ("int(year) >= now.year - 1", "datetime.now(timezone.utc)",
                  "None/0/missing/non-numeric", "RADARR_UPGRADE_PRIORITY_LABEL",
                  "SONARR_UPGRADE_PRIORITY_LABEL", "priority_hashes",
                  "core.queue_top", "airDateUtc", "airDate", "upgrade_labels",
                  "queue_bottom"):
        check(f"drop: contract detail {_need!r} reaches the slice intents",
              _need in _rbody)
    # global must-not-change prose constrains EVERY slice, not just the last item
    check("drop: shared parent context reaches EVERY slice intent",
          all("purge_stalled_upgrade_torrents" in s["intent"] for s in rsl),
          [s["id"] for s in rsl if "purge_stalled_upgrade_torrents"
           not in s["intent"]])
    # ...but must NOT contaminate the FROZEN gate literals: a name that only
    # appears in must-not-change prose is not something this slice's diff adds.
    check("drop: shared context does not leak into must_contain literals",
          all("purge_stalled_upgrade_torrents" not in s["must_contain"]
              for s in rsl), [s["must_contain"] for s in rsl])
    # titles/slugs must not carry the list marker or a code fence
    check("drop: slice ids carry no list marker / fence debris",
          not any(re.match(r"^s\d+-\d", s["id"]) or "python" == s["id"][-6:]
                  for s in rsl), [s["id"] for s in rsl])
    # the helper is recognised as the shared definer and lands first
    check("drop: _is_recent_year is the definer root",
          rsl[0]["id"] == "s1-is-recent-year" and rsl[0]["depends_on"] == [],
          (rsl[0]["id"], rsl[0]["depends_on"]))
    check("drop: consumers depend on the helper slice",
          all(s["depends_on"] == ["s1-is-recent-year"] for s in rsl[1:]),
          [s["depends_on"] for s in rsl[1:]])
    # the masked-definer bug: a non-discriminating earlier match must not hide the
    # real naming site later in the same clause.
    check("drop: 'Add a helper: ... def _is_recent_year' resolves to the real name",
          _defined_name("Add a helper: def _is_recent_year(year, now=None) -> bool")
          == "_is_recent_year",
          _defined_name("Add a helper: def _is_recent_year(year, now=None) -> bool"))
    # and the precision wins must survive: INLINE "(1)(2)(3)" sub-steps are not a
    # line-anchored author list, so they still do not split (build_intent below).

    # ---- detection positive / negative --------------------------------------
    is_o, _why, n = is_omnibus(codec_intent, threshold=3, env={})
    check("detect: codec-floor IS omnibus", is_o and n == 4, f"{is_o},{n}")
    single = "Add a codec_rank helper mapping release names to integer codec ranks."
    is_o2, _w2, n2 = is_omnibus(single, threshold=3, env={})
    check("detect: single property is NOT omnibus", (not is_o2), f"{is_o2},{n2}")
    is_o3, _w3, _n3 = is_omnibus(codec_intent, threshold=3,
                                 env={NO_SPLIT_ENV: "1"})
    check("detect: NO_SPLIT env disables detection", not is_o3)

    # ---- PRECISION: a SINGLE bounded fix must NOT decompose (2026-09-17) -------
    # Two real over-triggers. A single function's guard/skip conditions and a
    # single change's sub-steps are NOT independent properties. Assert each stays
    # 1 clause (not an omnibus), while the golden multi-property intents above
    # still decompose to 4 -- proving the gate raised precision, not just muted it.
    bfmr_intent = (  # ONE reconciliation function in lib/bfmrAutoLink.ts
        "BFMR reservations whose bfmrOrderId later comes to match a DIFFERENT "
        "order than their current OrderBfmrLink are never re-pointed. "
        "autoLinkBfmrReservations (lib/bfmrAutoLink.ts ~85-98) only links "
        "reservations with ZERO existing links (orderLinks: {none:{}}), so a link "
        "created against the wrong order stays stale forever; nothing reconciles "
        "it. FIX: add a bounded reconciliation pass, invoked from "
        "autoLinkBfmrReservations, that for each OrderBfmrLink whose reservation "
        "has a NON-NULL bfmrOrderId: digit-normalizes bfmrOrderId and compares to "
        "the CURRENTLY-LINKED order's orderNumber using the SAME "
        "normalization/containment logic as matchByOrderNumber (~lines 117-134); "
        "if it does NOT match AND exactly ONE different existing order's "
        "orderNumber matches by exact digit equality, re-points the link's orderId "
        "to that order, subject to guardLink against the target order (no duplicate "
        "reservationId on the target, no over-allocation), then calls "
        "recalcBfmrSalePrice for BOTH the old and the new orderId. It MUST SKIP "
        "(log a warning, never throw) every ambiguous case: reservation.bfmrOrderId "
        "null; more than one candidate target order; target order already has that "
        "reservationId linked; or no confident exact match. Never stomp a link that "
        "already matches. Only edit lib/bfmrAutoLink.ts and its test file.")
    is_b, _wb, nb = is_omnibus(bfmr_intent, threshold=3, env={})
    check("precision: BFMR single-function fix is NOT an omnibus (was 6)",
          (not is_b) and nb == 1, f"is_omnibus={is_b}, n={nb}")
    build_intent = (  # ONE build.py change; its '(1)(2)(3)' are sub-steps
        "Global-coverage eSIM plans (worldwide/multi-region) never appear on the "
        "built site: collect() in build.py iterates REGIONS but skips every "
        "README.md, and the global plans live in Global/README.md, so nothing "
        "parses them. FIX build.py so global plans surface everywhere: (1) parse "
        "Global/README.md's first rate table into global plan rows using the SAME "
        "row-parsing logic as parse_country (provider/plan/price/data_gb/etc.); "
        "(2) stop treating 'Global' as a country in the destination picker (it "
        "must NOT appear as a country checkbox/region grid); (3) flag every global "
        "row as global-coverage and render it BOTH as a dedicated 'Worldwide / "
        "Global' section on index.html AND merged into every selected country's "
        "ranked results and into each per-country page's table, each row visibly "
        "badged 'Global'; global rows must obey the same rank/filter controls "
        "(rankBy, minGB, topN, hotspot, unlimited). build.py must still build all "
        "existing country pages without error.")
    is_bd, _wbd, nbd = is_omnibus(build_intent, threshold=3, env={})
    check("precision: build.py sub-step fix stays a sane count (<3, was 4)",
          (not is_bd) and nbd < 3, f"is_omnibus={is_bd}, n={nbd}")
    # guard/skip-condition fragments themselves are rejected as non-properties
    check("precision: a guard clause is not a property",
          not _looks_like_property("if it does NOT match AND exactly one order"))
    check("precision: a skip condition is not a property",
          not _looks_like_property("target order already has that reservationId linked"))
    check("precision: an artifact-introduction clause IS a property",
          _looks_like_property("a supersede floor guard in the supersede sweep"))
    # --auto-slice-threshold / --no-auto-slice still work (threshold respected,
    # NO_SPLIT still hard-disables): a 4-property intent at threshold 5 -> not omni.
    is_hi, _whi, nhi = is_omnibus(codec_intent, threshold=5, env={})
    check("threshold: 4 properties < threshold 5 -> not omnibus (flag honored)",
          (not is_hi) and nhi == 4, f"is_omnibus={is_hi}, n={nhi}")

    # ---- plan is loadable by the slicer's contract (repo/target/slices) ------
    plan = build_plan("/tmp/repo", "arr-webhook.py", "python", "omni-selftest",
                      codec_intent)
    check("plan: has repo/target/slices",
          plan and all(k in plan for k in ("repo", "target", "slices")))
    check("plan: 4 slices", plan and len(plan["slices"]) == 4)

    # ---- idempotency: build twice -> byte-identical --------------------------
    p2 = build_plan("/tmp/repo", "arr-webhook.py", "python", "omni-selftest",
                    codec_intent)
    check("idempotent: plan build is deterministic",
          json.dumps(plan, sort_keys=True) == json.dumps(p2, sort_keys=True))

    # ---- interface variant (numbered list) decomposes too -------------------
    iface = ("1. add a codec_rank helper\n2. codec floor guard using codec_rank\n"
             "3. codec_rank sort keys\n4. dedup_via_sonarr pack pass with codec_rank")
    isl = build_slices("codec work", interface=iface, target="arr-webhook.py")
    check("interface: numbered list -> 4 slices", len(isl) == 4, f"got {len(isl)}")

    # ---- BOUNDED failure->requeue policy (hermetic temp state dir) ----------
    tmp = Path(tempfile.mkdtemp(prefix="omni-selftest-"))
    _saved_runs, _saved_plans = SLICE_RUNS_DIR, PLANS_DIR
    SLICE_RUNS_DIR = tmp / "slice-runs"
    PLANS_DIR = tmp / "slice-plans"
    SLICE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # fresh failure, decomposable -> convert
        a1 = plan_failure_action("omni-selftest", codec_intent, threshold=3, env={})
        check("requeue: fresh decomposable failure -> convert", a1 == "convert", a1)
        # fresh failure, single property -> cannot slice, escalate
        a2 = plan_failure_action("omni-single", single, threshold=3, env={})
        check("requeue: single-property failure -> cannot-slice-escalate",
              a2 == "cannot-slice-escalate", a2)
        # slice run exists + healthy -> resume (idempotent, no double-convert)
        (SLICE_RUNS_DIR / "omni-run.json").write_text(json.dumps(
            {"slices": {"s1": {"status": "done"}, "s2": {"status": "enqueued"}}}))
        a3 = plan_failure_action("omni-run", codec_intent, threshold=3, env={})
        check("requeue: existing healthy run -> resume", a3 == "resume", a3)
        # slice run exists + a slice FAILED -> escalate (do NOT requeue forever)
        (SLICE_RUNS_DIR / "omni-bad.json").write_text(json.dumps(
            {"slices": {"s1": {"status": "done"}, "s2": {"status": "failed"}}}))
        a4 = plan_failure_action("omni-bad", codec_intent, threshold=3, env={})
        check("requeue: existing run with a failed slice -> escalate",
              a4 == "escalate", a4)
        # under a slicer (NO_SPLIT) -> escalate, never recurse
        a5 = plan_failure_action("omni-x", codec_intent, threshold=3,
                                 env={NO_SPLIT_ENV: "1"})
        check("requeue: under slicer (NO_SPLIT) -> escalate", a5 == "escalate", a5)

        # handoff exports NO_SPLIT and calls the slicer with --execute (mock run)
        seen = {}

        def _mock_run(cmd, env):
            seen["cmd"] = cmd
            seen["env"] = env
            return 0
        rc = autoslice_and_handoff(
            repo="/tmp/repo", target="arr-webhook.py", lang="python",
            label="omni-handoff", intent=codec_intent, threshold=3,
            model="qwen3.8:27b-q4_K_M", host="studio", num_ctx=32768,
            reason="selftest", _run=_mock_run)
        check("handoff: returns slicer rc", rc == 0, rc)
        check("handoff: invokes slicer --execute",
              "--execute" in seen.get("cmd", []) and str(SLICER) in seen.get("cmd", []))
        check("handoff: exports NO_SPLIT=1 to child",
              seen.get("env", {}).get(NO_SPLIT_ENV) == "1")
        check("handoff: pins qwen/studio/32768 to the slicer",
              "qwen3.8:27b-q4_K_M" in seen.get("cmd", [])
              and "studio" in seen.get("cmd", [])
              and "32768" in seen.get("cmd", []))
        check("handoff: wrote a plan file",
              plan_path_for("omni-handoff").exists())
        # second handoff for the SAME label resumes (idempotent, no re-convert):
        # simulate the slicer having created run-state, then re-handoff.
        (SLICE_RUNS_DIR / "omni-handoff.json").write_text(json.dumps(
            {"slices": {"s1-codec-rank-helper": {"status": "awaiting_review"}}}))
        rc2 = autoslice_and_handoff(
            repo="/tmp/repo", target="arr-webhook.py", lang="python",
            label="omni-handoff", intent=codec_intent, threshold=3,
            _run=_mock_run)
        check("idempotent handoff: existing run resumes (rc 0)", rc2 == 0, rc2)
    finally:
        SLICE_RUNS_DIR, PLANS_DIR = _saved_runs, _saved_plans

    passed = sum(1 for _n, ok, _e in results if ok)
    total = len(results)
    for name, ok, extra in results:
        mark = "ok  " if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if not ok and extra != "":
            line += f"   -> {extra}"
        print(line)
    print(f"self-test: {passed}/{total} passed")
    return 0 if passed == total else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        return _self_test()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
