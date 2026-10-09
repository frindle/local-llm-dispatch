#!/usr/bin/env python3
"""darkbloom-field-probe: does Darkbloom's OpenAI endpoint HONOR each sampling /
thinking field the model profiles send?  Sends identical prompts with ONE field
changed at a time (fixed seed, so a honored field must change the output) and reports
whether output text / usage / reasoning_content differ from the baseline.

  python3 ~/bin/darkbloom-field-probe.py [--model ID] [--base URL] [--json]

Reads the Bearer key from ~/.darkbloom/local.json in code; it is never printed.
Does NOT start/stop/switch Darkbloom. If the model is not currently served
(GET /v1/models) it exits 2 with "PENDING" -- run again once the pair is loaded.

Verdicts per field:
  HONORED         output (or usage / reasoning_content) differed from baseline
  NO-EFFECT       identical to baseline (field ignored, OR the value had no effect on this
                  prompt -- re-run with --tries 3 before concluding "ignored")
  UNDETERMINISTIC the baseline itself differed between two identical requests (seed not
                  honored), so differences are not attributable to the field
  REJECTED        HTTP 4xx for that field (server validates and refuses it)
d-inference docs (architecture/inference.md "Sampling parameters") say: honored =
temperature, top_p, top_k, repetition_penalty, presence/frequency_penalty, stop, seed,
max_tokens; min_p IGNORED (always 0). enable_thinking via chat_template_kwargs is the
card's switch (qwen3.6 L827, qwen3.5 L992); this probe is the live check of both claims.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

LOCAL_JSON = os.path.expanduser("~/.darkbloom/local.json")
PAIR = ["qwen3.6-35b-a3b-vl-mtp-mxfp8", "Qwen3.5-9B"]

# A prompt that invites repetition (so penalties have something to bite on) and a short
# reasoning task (so enable_thinking visibly changes the output / usage).
PROMPT = ("List ten different words that rhyme with 'cat', then say the word 'cat' "
          "five more times, then compute 17*23 and give the final number.")


def _rec():
    try:
        return json.load(open(LOCAL_JSON))
    except (OSError, ValueError):
        return {}


def _base(arg):
    if arg:
        return arg.rstrip("/")
    r = _rec()
    b = str(r.get("base_url") or "").rstrip("/") or (
        os.environ.get("DARKBLOOM_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _req(base, path, body=None, timeout=300):
    key = str(_rec().get("api_key") or "")
    r = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def ask(base, model, **extra):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
            "stream": False, "temperature": 0.8, "top_p": 0.95, "seed": 1234,
            "max_tokens": 1024}
    ctk = extra.pop("chat_template_kwargs", None)
    body.update(extra)
    if ctk is not None:
        body["chat_template_kwargs"] = ctk
    try:
        d = _req(base, "/v1/chat/completions", body)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:160]}"}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {"error": f"unreachable: {e}"}
    m = (d.get("choices") or [{}])[0].get("message") or {}
    u = d.get("usage") or {}
    return {"content": m.get("content") or "", "reasoning": m.get("reasoning_content") or m.get("reasoning") or "",
            "completion_tokens": u.get("completion_tokens"),
            "inline_think": "<think>" in (m.get("content") or "") or "</think>" in (m.get("content") or "")}


def differs(a, b):
    return (a["content"] != b["content"] or a["reasoning"] != b["reasoning"]
            or a["completion_tokens"] != b["completion_tokens"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=None, help="default: first of the pair that is served")
    ap.add_argument("--base", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    base = _base(a.base)
    try:
        served = [m["id"] for m in (_req(base, "/v1/models").get("data") or [])]
    except Exception as e:
        print(f"PENDING: cannot list models at {base}/v1/models ({e})")
        return 2
    model = a.model or next((m for m in PAIR if m in served), None)
    if not model or model not in served:
        print(f"PENDING: the pair is not served (served: {served}); load it "
              f"(darkbloom start --model Qwen3.5-9B --model qwen3.6-35b-a3b-vl-mtp-mxfp8 --local-endpoint) "
              f"and re-run: python3 ~/bin/darkbloom-field-probe.py")
        return 2
    t0 = time.time()
    base1, base2 = ask(base, model), ask(base, model)
    if "error" in base1:
        print("baseline failed:", base1["error"])
        return 1
    stable = not differs(base1, base2)
    tests = [
        ("top_k=1 (vs unset)", {"top_k": 1}),
        ("top_k=5", {"top_k": 5}),
        ("presence_penalty=2.0", {"presence_penalty": 2.0}),
        ("repetition_penalty=1.6", {"repetition_penalty": 1.6}),
        ("repeat_penalty=1.6 (llama-server name; expected ignored)", {"repeat_penalty": 1.6}),
        ("min_p=0.9 (docs: ignored)", {"min_p": 0.9}),
        ("chat_template_kwargs.enable_thinking=false", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("chat_template_kwargs.enable_thinking=true", {"chat_template_kwargs": {"enable_thinking": True}}),
        ("chat_template_kwargs.enable_thinking=true,preserve_thinking=true",
         {"chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}}),
    ]
    rows = []
    for name, extra in tests:
        r = ask(base, model, **extra)
        if "error" in r:
            verdict = "REJECTED"
        elif not stable:
            verdict = "UNDETERMINISTIC"
        else:
            verdict = "HONORED" if differs(base1, r) else "NO-EFFECT"
        rows.append({"field": name, "verdict": verdict,
                     "completion_tokens": r.get("completion_tokens"),
                     "has_reasoning_content": bool(r.get("reasoning")),
                     "inline_think_in_content": r.get("inline_think"),
                     "error": r.get("error")})
    out = {"model": model, "base": base, "baseline_stable": stable,
           "baseline": {"completion_tokens": base1["completion_tokens"],
                        "has_reasoning_content": bool(base1["reasoning"]),
                        "inline_think_in_content": base1["inline_think"]},
           "results": rows, "seconds": round(time.time() - t0, 1)}
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"model={model} baseline_stable={stable} baseline_tokens={base1['completion_tokens']} "
              f"baseline_reasoning_content={bool(base1['reasoning'])} inline_think={base1['inline_think']}")
        for r in rows:
            print(f"  {r['verdict']:15} {r['field']:62} tokens={r['completion_tokens']} "
                  f"reasoning_content={r['has_reasoning_content']} inline_think={r['inline_think_in_content']}"
                  + (f"  {r['error']}" if r["error"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
