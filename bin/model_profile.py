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


def build_request_fields(model, role, api, overrides=None, think=None, lane=None):
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
