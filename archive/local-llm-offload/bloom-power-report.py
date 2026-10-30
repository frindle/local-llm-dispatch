#!/usr/bin/env python3
"""Monthly Darkbloom electricity-vs-income report: THREE transparent methods side by side (stdlib only).

  bloom-power-report.py --month YYYY-MM [--rate 0.128] [--calibration 1.0] [--offset-w 0]
                        [--baseline-w W] [--csv [--out FILE]] [--state-dir DIR]

Inputs: ~/.darkbloom/power/power-log-YYYY-MM.csv (bloom-power-log.py) and the earnings archive
~/.darkbloom/earnings-archive/ (bloom-earnings-archive.py). Days/months are UTC.

 (a) TOTAL machine kWh and cost (everything the Mac drew, incl. Penn's own LLM work).
 (b) kWh/cost split by Darkbloom state: OFFLINE vs ONLINE (idle + serving) vs SERVING only.
     Still includes Penn's own work when it overlapped; that overlap is shown on its own line.
 (c) INCREMENTAL kWh attributable to Darkbloom = (watts while online - idle baseline) over intervals
     with NO non-Darkbloom LLM/queue work. Baseline = --baseline-w, else median of Darkbloom-off clean
     intervals, else a flagged estimate.

Calibration: raw macmon SMC watts are logged; reported watts = raw * --calibration + --offset-w (default
1.0 / 0). To calibrate: plug the Mac into a smart plug / Kill-A-Watt, compare its watts to the logged
avg_w at a few steady loads, and fit wall = a*raw + b (e.g. via two points), then pass --calibration a
--offset-w b. Wall power includes PSU loss, so wall >= SMC reading.

NOT TAX ADVICE. The methods give different numbers; pick one with a CPA and apply it consistently."""
import argparse
import calendar
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bloom_power_lib as L  # noqa: E402


def month_bounds(month):
    y, m = map(int, month.split("-"))
    start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    days = calendar.monthrange(y, m)[1]
    return start, days


def daily(rows, entries, args, baseline):
    days = {}
    for r in rows:
        days.setdefault(r["ts"].strftime("%Y-%m-%d"), []).append(r)
    ed = L.summarize_earnings(entries)["by_day"]
    out = []
    for day in sorted(set(days) | set(ed)):
        rep = L.compute_report(days.get(day, []), args.rate, args.calibration, args.offset_w, baseline)
        e = ed.get(day, {"base": 0.0, "job": 0.0, "n": 0})
        out.append({
            "date": day, "gross_base_usd": round(e["base"], 6), "gross_job_usd": round(e["job"], 6),
            "gross_total_usd": round(e["base"] + e["job"], 6), "earnings_entries": e["n"],
            "a_total_kwh": round(rep["a_total_kwh"], 4), "a_total_cost": round(rep["a_total_cost"], 4),
            "b_offline_kwh": round(rep["b"]["off"]["kwh"], 4), "b_online_kwh": round(rep["b"]["online"]["kwh"], 4),
            "b_serving_kwh": round(rep["b"]["serving"]["kwh"], 4),
            "b_online_cost": round(rep["b"]["online"]["cost"], 4), "b_serving_cost": round(rep["b"]["serving"]["cost"], 4),
            "c_incremental_kwh": round(rep["c"]["total"]["kwh"], 4), "c_incremental_cost": round(rep["c"]["total"]["cost"], 4),
            "covered_hours": round(rep["covered_s"] / 3600, 2),
        })
    return out


def gaps(rows, month):
    start, ndays = month_bounds(month)
    total = ndays * 86400
    now = dt.datetime.now(dt.timezone.utc)
    if start.strftime("%Y-%m") == now.strftime("%Y-%m"):
        total = (now - start).total_seconds()          # current month: only elapsed time can be missing
    covered = sum(r["interval_s"] for r in rows if r["power_ok"])
    unavail = sum(r["interval_s"] for r in rows if not r["power_ok"])
    stale = sum(1 for r in rows if not r["power_ok"])
    return total, covered, unavail, stale


def money(x):
    return f"${x:,.2f}"


def render(month, rep, earn, rows, args):
    L_ = []
    p = L_.append
    p(f"DARKBLOOM POWER vs INCOME  {month} (UTC)   rate ${args.rate}/kWh   calibration x{args.calibration} + {args.offset_w} W")
    p("=" * 100)
    p(f"Gross Darkbloom income (archived ledger): {money(earn['gross'])}  = base reward {money(earn['base'])} + per-job {money(earn['job'])}  ({earn['n']} entries)")
    p("")
    g = earn["gross"]
    p("(a) TOTAL machine electricity (everything the Mac drew, incl. your own LLM work)")
    p(f"    {rep['a_total_kwh']:.2f} kWh  -> {money(rep['a_total_cost'])}     net vs gross: {money(g - rep['a_total_cost'])}")
    p("")
    b = rep["b"]
    p("(b) By Darkbloom state (includes any of your own work that overlapped)")
    for label, k in (("OFFLINE (provider not running)", "off"), ("ONLINE (idle + serving)", "online"),
                     ("  of which online-idle", "idle"), ("  of which SERVING", "serving")):
        p(f"    {label:34s} {b[k]['seconds']/3600:8.1f} h  {b[k]['kwh']:8.2f} kWh  {money(b[k]['cost']):>10s}")
    p(f"    overlap: {b['online']['kwh_with_other_work']:.2f} kWh of ONLINE time also had your own LLM/queue work running")
    p(f"    net vs gross using ONLINE cost: {money(g - b['online']['cost'])}    using SERVING-only cost: {money(g - b['serving']['cost'])}")
    p("")
    c = rep["c"]
    p("(c) INCREMENTAL kWh attributable to Darkbloom (online watts - baseline; your own-work intervals excluded)")
    p(f"    baseline: {rep['c_baseline_w']:.1f} W  [{rep['c_baseline_src']}]")
    p(f"    excluded (your own work while Darkbloom online): {rep['c_excluded_other_s']/3600:.1f} h")
    for label, k in (("online-idle (clean)", "idle"), ("serving (clean)", "serving"), ("TOTAL incremental", "total")):
        p(f"    {label:34s} {c[k]['seconds']/3600:8.1f} h  {c[k]['kwh']:8.2f} kWh  {money(c[k]['cost']):>10s}")
    p(f"    net vs gross: {money(g - c['total']['cost'])}")
    p("")
    p("SUMMARY (cost to offset -> net income)")
    p(f"    (a) total machine       {money(rep['a_total_cost']):>10s} -> {money(g - rep['a_total_cost'])}")
    p(f"    (b) online-state        {money(b['online']['cost']):>10s} -> {money(g - b['online']['cost'])}")
    p(f"    (b) serving-state       {money(b['serving']['cost']):>10s} -> {money(g - b['serving']['cost'])}")
    p(f"    (c) incremental         {money(c['total']['cost']):>10s} -> {money(g - c['total']['cost'])}")
    p("")
    total, covered, unavail, bad = gaps(rows, month)
    p("DATA GAPS")
    p(f"    power coverage: {covered/3600:.1f} h of {total/3600:.1f} h in period ({100*covered/total if total else 0:.1f}%); "
      f"uncovered {max(0, total - covered)/3600:.1f} h (machine off/asleep, sampler not running, or macmon unavailable)")
    p(f"    rows with macmon unavailable: {bad} ({unavail/3600:.1f} h)")
    if not rows:
        p("    NO power rows for this month.")
    if not earn["n"]:
        p("    NO archived earnings for this month (the API only keeps the latest ~1000 entries; entries from before "
          "the archiver started cannot be recovered; compare with the console lifetime total).")
    p("")
    p("NOT TAX ADVICE. The three methods give different numbers (a >= b-online >= b-serving; c depends on the baseline). "
      "This is bookkeeping data: choose ONE method with your CPA and apply it consistently. Wattage is SMC "
      "system power (not wall power) x the calibration above; the 'serving' signal cannot separate network jobs from "
      "your own local-endpoint requests except by the other_work flag (see docs/bloom-power.md).")
    return "\n".join(L_)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--month", required=True, help="YYYY-MM (UTC)")
    ap.add_argument("--rate", type=float, default=0.128, help="$/kWh (default 0.128)")
    ap.add_argument("--calibration", type=float, default=1.0)
    ap.add_argument("--offset-w", type=float, default=0.0)
    ap.add_argument("--baseline-w", type=float, default=None)
    ap.add_argument("--csv", action="store_true", help="also write the per-day CSV export")
    ap.add_argument("--out", default=None, help="CSV path (default ./bloom-power-report-<month>.csv)")
    ap.add_argument("--state-dir", default=str(L.HOME_DIR))
    a = ap.parse_args(argv)
    try:
        calendar.monthrange(*map(int, a.month.split("-")))
    except (ValueError, TypeError):
        print("--month must be YYYY-MM", file=sys.stderr)
        return 2
    rows = L.load_power_rows(a.state_dir, a.month)
    entries = L.load_earnings(a.state_dir, a.month)
    rep = L.compute_report(rows, a.rate, a.calibration, a.offset_w, a.baseline_w, a.month)
    earn = L.summarize_earnings(entries)
    print(render(a.month, rep, earn, rows, a))
    if a.csv:
        d = daily(rows, entries, a, rep["c_baseline_w"])
        out = Path(a.out or f"bloom-power-report-{a.month}.csv")
        meta = [["# method_note", "a=total machine; b=by darkbloom state; c=incremental over baseline excluding own LLM work"],
                ["# rate_per_kwh", a.rate], ["# calibration", a.calibration], ["# offset_w", a.offset_w],
                ["# baseline_w", round(rep["c_baseline_w"], 2)], ["# baseline_source", rep["c_baseline_src"]],
                ["# not_tax_advice", "pick one method with a CPA and apply consistently"]]
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            for m in meta:
                w.writerow(m)
            if d:
                dw = csv.DictWriter(fh, fieldnames=list(d[0]))
                dw.writeheader()
                dw.writerows(d)
        print(f"\nCSV written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
