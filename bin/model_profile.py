#!/usr/bin/env python3
"""model_profile -- loader for model_profiles.yaml, the single source of truth for
what we send to a model (sampling, max_tokens, ctx, thinking control, tool-call
format, stop words). Every value in the YAML is transcribed from a vendored model
card under references/models/ and cites it (`source:`).

Public API
  get_profile(model, role="author", lane=None) -> dict   flat, resolved profile
  build_request_fields(model, role, api, overrides=None, think=None, lane=None)
        -> dict   exactly what to merge into the request body:
        api="openai": top-level temperature/top_p/top_k/min_p/presence_penalty/
                      repetition_penalty(+repeat_penalty alias)/max_tokens/stop and
                      chat_template_kwargs{enable_thinking,preserve_thinking}
        api="ollama": {"options": {...num_ctx,num_predict,repeat_penalty...},
                       "think": bool}   (think only when the profile/override sets it)
  resolve_alias(model, lane=None) -> (model, aliased_from_or_None)   logged once
  check_profiles(path=None) -> [problems]   drift/citation check (used by canary)
  python3 model_profile.py --show MODEL [ROLE] | --check | --rehash

`overrides` (explicit CLI/caller values) replace profile values only when not None.
Unknown models get the YAML `fallback` profile with a LOUD warning (once per model).
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES_PATH = Path(os.environ.get("MODEL_PROFILES_PATH") or HERE / "model_profiles.yaml")

_CACHE = {}
_WARNED = set()

SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty",
                 "repetition_penalty")
MODE_KEYS = SAMPLING_KEYS + ("max_tokens", "ctx", "enable_thinking",
                             "preserve_thinking", "preserve_reasoning", "stop")


def _warn(key, msg):
    """Log a warning once per process per key, to stderr (never silent)."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"[model_profile] WARNING: {msg}", file=sys.stderr, flush=True)


def load_profiles(path=None, refresh=False):
    p = Path(path) if path else PROFILES_PATH
    key = str(p)
    if not refresh and key in _CACHE:
        return _CACHE[key]
    try:
        import yaml
    except ImportError as e:  # loud: the profile is the only source of defaults
        raise RuntimeError(f"model_profile: PyYAML missing in {sys.executable} ({e}); "
                           f"cannot read {p}") from None
    with open(p) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "models" not in data:
        raise RuntimeError(f"model_profile: {p} has no 'models' table")
    _CACHE[key] = data
    return data


def cards_root(data=None):
    data = data or load_profiles()
    return Path(os.environ.get("MODEL_CARDS_ROOT")
                or os.path.expanduser(str(data.get("cards_root") or "")))


def _find_entry(data, model):
    m = str(model or "").strip()
    models = data["models"]
    if m in models:
        return m, models[m]
    ml = m.lower()
    for k, v in models.items():
        if k.lower() == ml:
            return k, v
        if ml in [str(n).lower() for n in (v.get("names") or [])]:
            return k, v
    return None, None


def resolve_alias(model, lane=None, data=None):
    """Explicit, logged alias resolution. Returns (resolved_model, aliased_from).
    Aliases apply only on the lanes they declare (`lanes:`); with no lane given a lane-scoped alias does NOT apply."""
    data = data or load_profiles()
    a = (data.get("aliases") or {}).get(str(model or ""))
    if not a:
        return model, None
    lanes = a.get("lanes")
    if lanes and lane not in lanes:   # lane-scoped alias: never applies without a lane
        return model, None
    _warn(("alias", model, a["to"]),
          f"ALIAS {model} -> {a['to']} on lane {lane or 'any'}: {a.get('reason', '')} "
          f"[{a.get('source', 'no source')}]")
    return a["to"], model


def _role_mode(data, role):
    roles = data.get("roles") or {}
    r = str(role or "author")
    if r in roles:
        return roles[r]
    _warn(("role", r), f"unknown role {r!r}; treating as 'author' (thinking_coding)")
    return roles.get("author", "thinking_coding")


def get_profile(model, role="author", lane=None, data=None):
    """Flat resolved profile for (model, role). Never raises for an unknown model:
    returns the fallback profile with fallback=True and a loud warning."""
    data = data or load_profiles()
    resolved, aliased_from = resolve_alias(model, lane, data)
    key, entry = _find_entry(data, resolved)
    mode = _role_mode(data, role)
    if entry is None:
        _warn(("fallback", str(resolved)),
              f"NO PROFILE for model {resolved!r} -- using FALLBACK legacy defaults "
              f"(no model card). Add an entry to {PROFILES_PATH} citing its card.")
        entry, key = data["fallback"], "<fallback>"
    modes = entry.get("modes") or {}
    vals = dict(modes.get(mode) or modes.get("thinking_coding") or {})
    out = {k: vals.get(k) for k in MODE_KEYS}
    out["stop"] = list(vals.get("stop") or [])
    # Per-role thinking policy (2026-10-09): a mode may carry `thinking: off|low|on` (or a bool).
    # off/on pin enable_thinking; low keeps the mode's enable_thinking (reasoning_effort is NOT
    # visibly honored by Darkbloom) and is only surfaced as out["thinking"]. Absent = unchanged.
    _th = vals.get("thinking")
    _tn = str(_th).strip().lower() if _th is not None else None
    if _tn in ("off", "false", "no", "none", "0"):
        out["enable_thinking"], out["thinking"] = False, "off"
    elif _tn in ("on", "true", "yes", "1"):
        out["enable_thinking"], out["thinking"] = True, "on"
    elif _tn in ("low", "medium", "high"):
        out["thinking"] = _tn
    else:
        out["thinking"] = None
    out.update({
        "model": resolved, "requested_model": model, "aliased_from": aliased_from,
        "profile_key": key, "role": role, "mode": mode,
        "tool_call_format": entry.get("tool_call_format", "native"),
        "card": entry.get("card"), "source": entry.get("source"),
        "fallback": bool(entry.get("fallback")),
    })
    # Phase 3 robustness knobs (worker_robust.py): global `robust_defaults` <- per-model `robust`.
    rb = dict(data.get("robust_defaults") or {})
    for k, v in (entry.get("robust") or {}).items():
        if isinstance(v, dict) and isinstance(rb.get(k), dict):
            rb[k] = {**rb[k], **v}
        else:
            rb[k] = v
    out["robust"] = rb
    return out


def build_request_fields(model, role, api, overrides=None, think=None, lane=None,
                         turn_kind=None, arm=None):
    """What to put in the request body for `api` ("openai" | "ollama").
    `overrides`: {temperature, top_p, top_k, min_p, presence_penalty,
    repetition_penalty, max_tokens, num_ctx, stop}; None values are ignored.
    `think`: tri-state CLI override of enable_thinking (None = profile)."""
    p = get_profile(model, role, lane)
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    v = {k: ov.get(k, p[k]) for k in SAMPLING_KEYS}
    max_tokens = ov.get("max_tokens", p["max_tokens"])
    stop = ov.get("stop", p["stop"])
    ctx = ov.get("num_ctx", ov.get("ctx", p["ctx"]))
    thinking = think if think is not None else p["enable_thinking"]
    if think is None and turn_kind is not None:
        _pol = thinking_policy(role, turn_kind, arm)      # None = keep the profile's value
        if _pol is not None:
            thinking = _pol
    if api == "openai":
        f = {k: val for k, val in v.items() if val is not None}
        if "repetition_penalty" in f:
            # llama-server reads repeat_penalty, Darkbloom/MLX reads repetition_penalty;
            # each ignores the other's name.
            f["repeat_penalty"] = f["repetition_penalty"]
        if max_tokens is not None:
            f["max_tokens"] = max_tokens
        if stop:
            f["stop"] = list(stop)
        ctk = {}
        if thinking is not None:
            ctk["enable_thinking"] = bool(thinking)
        if p["preserve_thinking"] is not None and thinking:
            ctk["preserve_thinking"] = bool(p["preserve_thinking"])
        if ctk:
            f["chat_template_kwargs"] = ctk
        return f
    if api == "ollama":
        opts = {}
        for k, val in v.items():
            if val is None:
                continue
            opts["repeat_penalty" if k == "repetition_penalty" else k] = val
        if ctx is not None:
            opts["num_ctx"] = ctx
        if max_tokens is not None:
            opts["num_predict"] = max_tokens
        if stop:
            opts["stop"] = list(stop)
        f = {"options": opts}
        if thinking is not None:
            f["think"] = bool(thinking)
        return f
    raise ValueError(f"build_request_fields: unknown api {api!r}")


def role_wants_reasoning_kept(model, role, lane=None):
    return bool(get_profile(model, role, lane)["preserve_reasoning"])


# ---------------------------------------------------------------- thinking policy / escalation
# THINKING_POLICY_MARK (2026-10-09). Settings live in model_profiles.yaml, never in code.
#   thinking_policy.roles[role]   -> "profile" (use the role's mode enable_thinking; the default,
#                                    i.e. NO behaviour change) or the name of an arm
#   thinking_policy.arms[arm]     -> {turn_kind: on|off|profile}, turn kinds TURN_KINDS
#   thinking_policy.ab_roles      -> roles the experiment arm (arg or env MODEL_THINKING_ARM)
#                                    may override; every other role keeps its pinned policy
#   escalation.roles[role]        -> {models: [...], claude: bool}: the ordered models a bigger-model
#                                    step may use (any Darkbloom-hostable model with a profile)
# Known probe facts (2026-10-09, Darkbloom): Qwen thinking arrives INSIDE content; chat_template_kwargs.
# enable_thinking=false is honored; reasoning_effort is NOT visibly honored (never sent); tool_choice
# required/named gives 400.
TURN_KINDS = ("oneshot", "tool_first", "tool_after_failure", "tool_mechanical")
_ONOFF = {"on": True, "true": True, "off": False, "false": False}


def thinking_policy(role, turn_kind, arm=None, data=None):
    """True / False = force enable_thinking for this (role, turn kind); None = defer to the
    role's profile mode. `arm` (or env MODEL_THINKING_ARM) overrides only roles listed in
    thinking_policy.ab_roles."""
    data = data or load_profiles()
    tp = data.get("thinking_policy") or {}
    arms = tp.get("arms") or {}
    pol = (tp.get("roles") or {}).get(str(role or "author"), "profile")
    arm = arm or os.environ.get("MODEL_THINKING_ARM") or None
    if arm and str(role or "author") in (tp.get("ab_roles") or []):
        pol = arm
    if pol in (None, "profile"):
        return None
    table = arms.get(pol)
    if not isinstance(table, dict):
        _warn(("arm", pol), f"thinking_policy: unknown arm {pol!r} for role {role!r}; using the profile")
        return None
    v = str(table.get(turn_kind, "profile")).strip().lower()
    return _ONOFF.get(v)


def escalation_models(role, data=None):
    """Ordered list of model names a bigger-model/escalation step may use for `role` (from the
    YAML `escalation:` section; [] when none is configured)."""
    data = data or load_profiles()
    e = ((data.get("escalation") or {}).get("roles") or {}).get(str(role or "author")) or {}
    return [str(m) for m in (e.get("models") or []) if m]


def escalation_allows_claude(role, data=None):
    data = data or load_profiles()
    e = ((data.get("escalation") or {}).get("roles") or {}).get(str(role or "author")) or {}
    return bool(e.get("claude"))


def check_policy(data):
    """Problems in thinking_policy / escalation (used by check_profiles)."""
    out = []
    tp = data.get("thinking_policy") or {}
    arms = tp.get("arms") or {}
    for arm, table in arms.items():
        for k, v in (table or {}).items():
            if k not in TURN_KINDS:
                out.append(f"thinking_policy.arms.{arm}: unknown turn kind {k!r}")
            if str(v).strip().lower() not in ("on", "off", "true", "false", "profile"):
                out.append(f"thinking_policy.arms.{arm}.{k}: bad value {v!r}")
    for role, pol in (tp.get("roles") or {}).items():
        if pol not in ("profile", None) and pol not in arms:
            out.append(f"thinking_policy.roles.{role}: unknown arm {pol!r}")
    for ab in tp.get("ab_roles") or []:
        if ab not in (data.get("roles") or {}):
            out.append(f"thinking_policy.ab_roles: unknown role {ab!r}")
    for role, e in ((data.get("escalation") or {}).get("roles") or {}).items():
        if role not in (data.get("roles") or {}):
            out.append(f"escalation.roles.{role}: unknown role")
        for m in (e or {}).get("models") or []:
            if _find_entry(data, m)[1] is None:
                out.append(f"escalation.roles.{role}: model {m!r} has no profile entry (add one citing its card)")
    return out


# ---------------------------------------------------------------- drift/citations
def card_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_KEY_RX = {"temperature": r"temperature", "top_p": r"top_?p", "top_k": r"top_?k",
           "min_p": r"min_?p", "presence_penalty": r"presence_?penalty",
           "repetition_penalty": r"repetition_?penalty"}


def card_supports(card_text, mode):
    """True when some window of 4 consecutive card lines states this mode's
    temperature, top_p, top_k (always) and presence_penalty / repetition_penalty
    (when non-default), each as KEY=VALUE / KEY: VALUE, compared numerically. This is
    what ties the YAML numbers to the card text, so editing a number without the card
    backing it fails the drift check."""
    import re
    lines = card_text.splitlines()
    need = {"temperature": mode.get("temperature"), "top_p": mode.get("top_p"),
            "top_k": mode.get("top_k")}
    if mode.get("presence_penalty") not in (None, 0, 0.0):
        need["presence_penalty"] = mode["presence_penalty"]
    if mode.get("repetition_penalty") not in (None, 1, 1.0):
        need["repetition_penalty"] = mode["repetition_penalty"]
    need = {k: v for k, v in need.items() if v is not None}
    for i in range(len(lines)):
        w = " ".join(lines[i:i + 4]).lower()
        ok = True
        for k, v in need.items():
            found = [float(x) for x in re.findall(_KEY_RX[k] + r"\W{0,4}(-?\d+(?:\.\d+)?)", w)]
            if not any(abs(f - float(v)) < 1e-9 for f in found):
                ok = False
                break
        if ok:
            return True
    return False


def check_profiles(path=None):
    """List of problems (empty = OK): every model has a card file that exists, a
    `source:` citation, card_sha256 matching the card on disk, a mode for every role,
    max_tokens >= 32768 for thinking_coding, and every alias target exists."""
    problems = []
    data = load_profiles(path, refresh=True)
    root = cards_root(data)
    for name, e in data["models"].items():
        if not str(e.get("source") or "").strip():
            problems.append(f"{name}: missing source: citation")
        card = e.get("card")
        if not card:
            problems.append(f"{name}: missing card:")
            continue
        cp = root / card
        if not cp.is_file():
            problems.append(f"{name}: cited card does not exist: {cp}")
        else:
            want = str(e.get("card_sha256") or "")
            got = card_sha256(cp)
            if want != got:
                problems.append(f"{name}: card changed since profile was written "
                                f"(card_sha256 {want[:12] or 'missing'} != {got[:12]}); "
                                f"re-review {card} then run model_profile.py --rehash")
        card_text = cp.read_text(errors="replace") if cp.is_file() else ""
        for mode in set((data.get("roles") or {}).values()):
            m = (e.get("modes") or {}).get(mode)
            if not isinstance(m, dict):
                problems.append(f"{name}: missing mode {mode}")
                continue
            miss = [k for k in MODE_KEYS if k not in m]
            if miss:
                problems.append(f"{name}.{mode}: missing keys {miss}")
            if card_text and not card_supports(card_text, m):
                problems.append(f"{name}.{mode}: sampling values are not stated together in "
                                f"{card} (temperature/top_p/top_k/penalties do not match the card)")
        tc = (e.get("modes") or {}).get("thinking_coding") or {}
        if (tc.get("max_tokens") or 0) < 32768:
            problems.append(f"{name}.thinking_coding: max_tokens < 32768")
        if not e.get("tool_call_format"):
            problems.append(f"{name}: missing tool_call_format")
    problems += check_policy(data)
    for src, a in (data.get("aliases") or {}).items():
        if _find_entry(data, a.get("to"))[1] is None:
            problems.append(f"alias {src}: target {a.get('to')} has no profile")
        if not str(a.get("source") or "").strip():
            problems.append(f"alias {src}: missing source:")
    if not str((data.get("fallback") or {}).get("source") or "").strip():
        problems.append("fallback: missing source:")
    return problems


def rehash(path=None):
    """Rewrite every card_sha256 in the YAML in place (text edit, keeps comments)."""
    import re
    p = Path(path) if path else PROFILES_PATH
    data = load_profiles(p, refresh=True)
    root = cards_root(data)
    text = p.read_text()
    for name, e in data["models"].items():
        cp = root / e["card"]
        if not cp.is_file():
            continue
        h = card_sha256(cp)
        pat = re.compile(r'(\n  (?:"%s"|%s):[^\n]*\n(?:(?!\n  \S).)*?card_sha256: )"[^"]*"' %
                         (re.escape(name), re.escape(name)), re.S)
        text, n = pat.subn(lambda m: m.group(1) + '"%s"' % h, text, count=1)
        if not n:
            print(f"rehash: could not locate {name}", file=sys.stderr)
    p.write_text(text)
    _CACHE.clear()


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--check":
        probs = check_profiles()
        for x in probs:
            print("PROBLEM:", x)
        print("OK" if not probs else f"{len(probs)} problem(s)")
        return 1 if probs else 0
    if argv[0] == "--rehash":
        rehash()
        print("rehashed")
        return 0
    if argv[0] == "--show" and len(argv) >= 2:
        role = argv[2] if len(argv) > 2 else "author"
        print(json.dumps({"profile": get_profile(argv[1], role),
                          "openai": build_request_fields(argv[1], role, "openai"),
                          "ollama": build_request_fields(argv[1], role, "ollama")},
                         indent=2, default=str))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
