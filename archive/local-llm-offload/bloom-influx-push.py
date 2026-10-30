#!/usr/bin/env python3
"""Push Darkbloom + Mac power data to InfluxDB v2 (line protocol over the HTTP API). See bloom_influx.py.

  bloom-influx-push.py                 one pass (what the LaunchAgent runs every minute): new CSV rows, new
                                       classified minutes, new ledger rows, new totals snapshots -> spool -> Influx
  bloom-influx-push.py --dry-run [--backfill] [--sample N]
                                       print counts + sample line protocol; writes nothing, changes no cursor
  bloom-influx-push.py --backfill      re-send EVERYTHING in the sources (idempotent), report counts
  bloom-influx-push.py --verify        compare source point counts with Influx (read-only Flux queries)

Never prints the token. Exit 0 on success or backoff; 1 on a failed delivery (data stays spooled)."""
import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bloom_influx as B  # noqa: E402
import datetime as dt  # noqa: E402


def verify(ctx):
    st = {}
    lines, counts = B.collect(ctx, st, dt.datetime.now(dt.timezone.utc))
    bymeas = {}
    for ln in lines:
        bymeas.setdefault(ln.split(",", 1)[0].split(" ", 1)[0], []).append(ln)
    fields = {"mac_power": "wh", "darkbloom_minute": "paid_frac", "darkbloom_earnings": "usd", "darkbloom_lifetime": "count"}
    bad = 0
    for m, f in fields.items():
        want = len(B.unique_points(bymeas.get(m, [])))
        got = B.flux_count(ctx, m, f)
        # Influx may hold MORE than the sources' current view (points pushed later by the live path, or from rows
        # whose power fields were blank); it must never hold fewer.
        status = "OK" if got == want else ("OK(>=)" if got > want else "MISSING")
        bad += status == "MISSING"
        print(f"{m:20s} source_unique={want:7d} influx={got:7d} {status}")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--url")
    ap.add_argument("--org")
    ap.add_argument("--bucket")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--sample", type=int, default=2)
    a = ap.parse_args(argv)
    ctx = B.Ctx(a.state_dir, a.url, a.org, a.bucket)
    try:
        if a.verify:
            return verify(ctx)
        code, info = B.run_once(ctx, backfill=a.backfill, dry_run=a.dry_run, sample=a.sample)
        if a.dry_run or a.backfill:
            sample = info.pop("sample", None)
            print(json.dumps(info, indent=2, sort_keys=True, default=str))
            for m, ls in (sample or {}).items():
                for ln in ls:
                    print(ln)
        return code
    except Exception:                      # never crash-loop: log (token-scrubbed) and exit 1
        tb = traceback.format_exc().strip().splitlines()
        B.log(ctx, "ERROR: unexpected: " + " | ".join(tb[-3:]))
        print("unexpected error (see push.log)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
