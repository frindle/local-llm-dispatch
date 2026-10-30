#!/usr/bin/env python3
"""Permanent, append-only archive of Darkbloom provider earnings (stdlib only).

Darkbloom has no `darkbloom earnings` CLI (docs/provider/quickstart.md), and the account-earnings API
returns only the latest N ledger entries, so this polls it (launchd, ~15 min) and appends every entry
not yet archived (deduped by ledger id) to ~/.darkbloom/earnings-archive/earnings-YYYY-MM.jsonl.
It NEVER prunes or rewrites an archive file.

Endpoint: GET https://api.darkbloom.dev/v1/provider/account-earnings?limit=1000
(references/d-inference docs/reference/api-contracts.md; Bearer API key). The token
(~/.darkbloom/auth_token) is read locally, sent ONLY to api.darkbloom.dev over https, and is never
printed or logged (error text is scrubbed of it).

Gap alert: if one poll returns >= GAP_THRESHOLD NEW entries the API window (1000) may have rolled past
entries that were never archived -> loud 'GAP SUSPECTED' on stderr + gap log line.

Usage: bloom-earnings-archive.py [--probe] [--limit N] [--state-dir DIR] [--url URL]
  --probe  print the response STRUCTURE (keys/types, one sample entry, no token) and archive nothing.
Read-only toward Darkbloom: a single authenticated GET; no provider control calls."""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bloom_power_lib as L  # noqa: E402

API_URL = "https://api.darkbloom.dev/v1/provider/account-earnings"
ALLOWED_HOST = "api.darkbloom.dev"
GAP_THRESHOLD = 1000
TOKEN_FILE = Path.home() / ".darkbloom" / "auth_token"


def log(msg, state_dir=None):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, file=sys.stderr, flush=True)
    if state_dir:
        try:
            L.append_line(Path(state_dir) / L.ARCHIVE_DIRNAME / "archive.log", line)
        except OSError:
            pass


def read_token(path=TOKEN_FILE):
    try:
        t = Path(path).read_text().strip()
    except OSError:
        return None
    return t or None


def fetch(url, token, limit, timeout=30):
    """Authenticated GET to api.darkbloom.dev only. Returns parsed JSON. Raises RuntimeError with a
    token-scrubbed message."""
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != ALLOWED_HOST:
        raise RuntimeError(f"refusing to send the token to {u.hostname!r} (only {ALLOWED_HOST} over https)")
    sep = "&" if u.query else "?"
    full = f"{url}{sep}limit={int(limit)}"
    req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                "User-Agent": "bloom-earnings-archive/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {ALLOWED_HOST}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        msg = str(e).replace(token, "***")
        raise RuntimeError(f"fetch failed: {type(e).__name__}: {msg}") from None


def describe(payload):
    """Structure only (no secrets): top-level keys/types and the keys of the first entry, values
    truncated. Amounts are Penn's own earnings data, not secrets."""
    out = {"top_type": type(payload).__name__}
    if isinstance(payload, dict):
        out["top_keys"] = {k: type(v).__name__ + (f"[{len(v)}]" if isinstance(v, (list, dict)) else "")
                           for k, v in payload.items()}
        out["scalars"] = {k: v for k, v in payload.items() if isinstance(v, (int, float, str, bool)) and len(str(v)) < 60}
    ents = L.extract_entries(payload)
    out["entries"] = len(ents)
    if ents:
        out["first_entry"] = {k: (str(v)[:60]) for k, v in ents[0].items()}
        out["parsed_first"] = {"id": L.entry_id(ents[0]), "ts": str(L.entry_ts(ents[0])),
                               "usd": L.entry_usd(ents[0]), "kind": L.entry_kind(ents[0])}
    return out


def poll_once(payload, base, now, threshold=GAP_THRESHOLD):
    """Archive one API payload. Returns dict(new, seen, noid, gap)."""
    entries = L.extract_entries(payload)
    had_prior = bool(L.load_archive_ids(base))      # first-ever poll is a backfill, not a gap
    new, noid = L.merge_into_archive(base, entries, now)
    gap = had_prior and new >= threshold
    # Account totals snapshot (lifetime count/total as reported) so archived sums can be reconciled with
    # what Darkbloom reports; entries older than the API window can no longer be recovered.
    if isinstance(payload, dict):
        snap = {k: payload.get(k) for k in ("count", "total_micro_usd", "available_balance_micro_usd",
                                              "withdrawable_balance_micro_usd", "recent_count", "history_limit")}
        snap["at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        snap["new"] = new
        L.append_line(Path(base) / L.ARCHIVE_DIRNAME / "totals.jsonl", json.dumps(snap, sort_keys=True))
    return {"seen": len(entries), "new": new, "noid": noid, "gap": gap}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state-dir", default=str(L.HOME_DIR))
    ap.add_argument("--url", default=API_URL)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    token = read_token()
    if not token:
        log("ERROR: no Darkbloom auth token at ~/.darkbloom/auth_token (provider not logged in?)", a.state_dir)
        return 2
    try:
        payload = fetch(a.url, token, a.limit)
    except RuntimeError as e:
        log(f"ERROR: {e}", a.state_dir)
        return 1
    if a.probe:
        print(json.dumps(describe(payload), indent=2, sort_keys=True))
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    r = poll_once(payload, a.state_dir, now)
    if r["seen"] == 0:
        log("WARN: response contained 0 recognizable entries (schema change?). Nothing archived.", a.state_dir)
        return 1
    log(f"ok seen={r['seen']} new={r['new']} no_id={r['noid']}", a.state_dir)
    if r["noid"]:
        log(f"WARN: {r['noid']} entries had no id field; keyed by content hash", a.state_dir)
    if r["gap"]:
        log(f"GAP SUSPECTED: {r['new']} new entries in one poll (>= {GAP_THRESHOLD}); the API returns only the "
            "latest entries, so older ones between polls may be missing. Reconcile against the Darkbloom console.",
            a.state_dir)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
