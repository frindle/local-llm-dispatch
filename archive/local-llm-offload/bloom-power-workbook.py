#!/usr/bin/env python3
"""Monthly Darkbloom electricity-vs-income WORKBOOK (.xlsx, openpyxl) for the CPA.

  bloom-power-workbook.py --month YYYY-MM          one month (FINAL if the month is over, else DRAFT)
  bloom-power-workbook.py --auto                   current UTC month (DRAFT) + the previous month as FINAL during the
                                                   first 3 UTC days of a month (what the daily LaunchAgent runs)
  options: --rate 0.128 --calibration 1.0 --offset-w 0 --baseline-w W --root DIR --state-dir DIR --local-only

Output: <root>/Darkbloom/<YYYY>/Darkbloom-<YYYY-MM>.xlsx  (root = $BLOOM_WORKBOOK_ROOT, else "root" in
~/.darkbloom/workbook.json, else the live Proton Drive sync root). A master copy is always kept in
~/.darkbloom/power/workbooks/<YYYY>/. If the root is missing/unmounted/unwritable the Proton copy is skipped with
a logged WARNING (exit 0, no crash). Atomic: the file is built locally and renamed into place; nothing in the
destination is ever deleted (an existing workbook for the same month is replaced, which is the idempotent
regenerate). Only local file presence is verifiable here; whether Proton actually synced is not.

Sheets: Summary, Daily, Minutes (every minute: paid/own/both/idle/off/unattributed), Earnings, Methodology.
Formulas are real Excel formulas (named cells Rate, Calibration, OffsetW, BaselineW). openpyxl cannot cache
formula results, so every headline figure also has a static "script value" + OK/DIFF check beside it.

BOOKKEEPING ESTIMATE, NOT TAX ADVICE. See docs/bloom-power.md."""
import argparse
import calendar
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import openpyxl
except ImportError:                                   # launchd/system python: use the tool venv if present
    _vpy = Path.home() / ".darkbloom" / "venv" / "bin" / "python"
    if _vpy.exists() and not os.environ.get("BLOOM_WB_REEXEC") and Path(sys.executable).resolve() != _vpy.resolve():
        os.environ["BLOOM_WB_REEXEC"] = "1"
        os.execv(str(_vpy), [str(_vpy)] + sys.argv)
    print("openpyxl missing: python3 -m venv ~/.darkbloom/venv && ~/.darkbloom/venv/bin/pip install openpyxl", file=sys.stderr)
    sys.exit(2)
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402

import bloom_power_lib as L  # noqa: E402

CLOUD = Path.home() / "Library" / "CloudStorage"
DEFAULT_PROTON_ROOT = CLOUD / "ProtonDrive-penndalton@pm.me-folder"
DOMAIN_XATTR = "com.apple.file-provider-domain-id"
DEFAULTS = {"rate": 0.128, "calibration": 1.0, "offset_w": 0.0, "baseline_w": None}
NOT_ADVICE = ("BOOKKEEPING ESTIMATE, NOT TAX ADVICE. Generated from sampled data; the methods below give different "
              "numbers. Whether and how electricity may be offset against Darkbloom income is a decision for the owner "
              "and a CPA; pick one method and apply it consistently.")
BOLD = Font(bold=True)
HDR_FILL = PatternFill("solid", fgColor="DDE4EE")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")


# ------------------------------------------------------------------ config / Proton root
def log(msg, state_dir=None):
    line = f"{L.utc_iso(dt.datetime.now(dt.timezone.utc).timestamp())} {msg}"
    print(line, file=sys.stderr)
    try:
        L.append_line(Path(state_dir or L.HOME_DIR) / L.POWER_DIRNAME / "workbook.log", line)
    except OSError:
        pass


def load_config(state_dir):
    try:
        d = json.loads((Path(state_dir) / "workbook.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def detect_proton_root(cloud=CLOUD):
    """The LIVE sync root is the one macOS registers as a File Provider domain (carries the
    com.apple.file-provider-domain-id xattr). A renamed/stale copy such as 'ProtonDrive-...-folder (9-13-26 2:48 PM)'
    has no such xattr. Fallback: the entry without a ' (' rename suffix; last resort the known default path."""
    try:
        cands = sorted(p for p in Path(cloud).glob("ProtonDrive-*") if p.is_dir())
    except OSError:
        cands = []

    def has_domain(p):
        try:
            return DOMAIN_XATTR in os.listxattr(p)
        except (OSError, AttributeError):
            return False
    live = [p for p in cands if has_domain(p)]
    if live:
        return live[0]
    plain = [p for p in cands if " (" not in p.name]
    return plain[0] if plain else DEFAULT_PROTON_ROOT


def resolve_root(cli_root, state_dir):
    if cli_root:
        return Path(cli_root)
    if os.environ.get("BLOOM_WORKBOOK_ROOT"):
        return Path(os.environ["BLOOM_WORKBOOK_ROOT"])
    r = load_config(state_dir).get("root")
    return Path(r).expanduser() if r else detect_proton_root()


def atomic_publish(src, dest_dir, name):
    """Copy src into dest_dir as `name` atomically (temp name then rename). Never deletes anything else."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".{name}.tmp{os.getpid()}"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest_dir / name)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()                      # our own half-written temp only
        except OSError:
            pass
        raise
    return dest_dir / name


def publish(local_file, root, year, name, state_dir):
    """Master copy already at local_file. Returns (proton_path or None, warning or None)."""
    root = Path(root)
    if not root.is_dir() or not os.access(root, os.W_OK):
        w = f"WARNING: workbook root {root} is missing/unmounted/unwritable; workbook kept only at {local_file}"
        log(w, state_dir)
        return None, w
    try:
        return atomic_publish(local_file, root / "Darkbloom" / str(year), name), None
    except OSError as e:
        w = f"WARNING: could not write to {root}/Darkbloom/{year}: {e}; workbook kept only at {local_file}"
        log(w, state_dir)
        return None, w


# ------------------------------------------------------------------ data
def load_totals(state_dir):
    out = []
    p = Path(state_dir) / L.ARCHIVE_DIRNAME / "totals.jsonl"
    try:
        for ln in p.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def month_is_final(month, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return month < now.strftime("%Y-%m")


def gather(month, state_dir, params, metrics_path=None, queue_path=None):
    rows = L.load_power_rows(state_dir, month)
    entries = sorted(L.load_earnings(state_dir, month), key=lambda e: L.entry_ts(e))
    windows = L.load_own_windows(month, metrics_path, queue_path)
    paid = L.paid_entries_from(entries)
    settled = L.archive_settled_until(state_dir)
    cls = L.classify_minutes(rows, windows, paid, settled)
    rep = L.compute_report(rows, params["rate"], params["calibration"], params["offset_w"], params["baseline_w"], month)
    att = L.attribution_report(rows, cls, params["rate"], params["calibration"], params["offset_w"], rep["c_baseline_w"])
    qa = L.matching_qa(rows, cls, paid, windows)
    return {"rows": rows, "entries": entries, "windows": windows, "paid": paid, "settled": settled, "cls": cls,
            "rep": rep, "att": att, "qa": qa, "earn": L.summarize_earnings(entries), "totals": load_totals(state_dir)}


# ------------------------------------------------------------------ workbook
MIN_COLS = ["ts_utc (end of minute)", "interval_s", "power_ok", "raw_avg_w", "dbk_state", "class", "paid_share",
            "own_signal", "basis", "requests_delta", "tokens_delta", "own_tokens_est", "sampler_other_work",
            "calibrated_W", "kWh", "paid_kWh", "paid_incr_kWh", "own_signals_seen", "day_utc"]
# letters used by formulas
CB, CC, CD, CE, CF, CG, CH = "B", "C", "D", "E", "F", "G", "H"
CN, CO, CP, CQ, CS = "N", "O", "P", "Q", "S"


def style_header(ws, row=1, ncols=None):
    for c in ws[row][: ncols or ws.max_column]:
        c.font = BOLD
        c.fill = HDR_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")


def widths(ws, ws_widths):
    for i, w in enumerate(ws_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_minutes(wb, D):
    ws = wb.create_sheet("Minutes")
    ws.append(MIN_COLS)
    style_header(ws)
    for i, (r, x) in enumerate(zip(D["rows"], D["cls"]), start=2):
        ts = r["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
        ws.append([
            ts, r["interval_s"], 1 if r["power_ok"] else 0, r["avg_w"], r["state"], x["cls"], round(x["paid_frac"], 6),
            "none" if x["tier"] == "-" else x["tier"], x["basis"], r.get("req_d") or 0, r.get("tok_d") or 0,
            round(x["own_tok"], 1), 1 if r["other"] else 0,
            f"=IF(C{i}=1,MAX(0,Calibration*D{i}+OffsetW),0)",
            f"=N{i}*B{i}/3600000",
            f"=O{i}*G{i}",
            f"=IF(AND(G{i}>0,C{i}=1),(N{i}-BaselineW)*B{i}/3600000*G{i},0)",
            (r.get("other_procs") or "")[:120], ts[:10]])
    ws.freeze_panes = "A2"
    widths(ws, [22, 10, 9, 10, 10, 15, 10, 10, 60, 10, 10, 10, 10, 11, 11, 11, 12, 40, 11])
    return ws, len(D["rows"]) + 1


def build_earnings(wb, D):
    ws = wb.create_sheet("Earnings")
    ws.append(["created_at", "ledger_id", "kind", "model", "job_id", "prompt_tokens", "completion_tokens",
               "amount_micro_usd", "amount_usd", "day_utc"])
    style_header(ws)
    for i, e in enumerate(D["entries"], start=2):
        ts = L.entry_ts(e)
        ws.append([e.get("created_at") or ts.strftime("%Y-%m-%dT%H:%M:%SZ"), str(e.get("_id", "")), L.entry_kind(e),
                   e.get("model"), str(e.get("job_id", "")), int(e.get("prompt_tokens") or 0),
                   int(e.get("completion_tokens") or 0), round(L.entry_usd(e) * 1e6), f"=H{i}/1000000",
                   ts.strftime("%Y-%m-%d")])
    ws.freeze_panes = "A2"
    widths(ws, [28, 14, 8, 30, 44, 12, 12, 14, 12, 12])
    return ws, len(D["entries"]) + 1


def build_daily(wb, D, month, nmin, nearn):
    ws = wb.create_sheet("Daily")
    hdr = ["date_utc", "gross_base_usd", "gross_per_job_usd", "gross_total_usd", "covered_hours", "total_kWh",
           "off_kWh", "idle_kWh", "paid_kWh", "own_kWh", "both_kWh", "unattributed_kWh", "network_unpaid_kWh",
           "paid_incr_kWh (option 2)", "option2_cost_usd", "net_after_option2_usd"]
    ws.append(hdr)
    style_header(ws)
    days = sorted({r["ts"].strftime("%Y-%m-%d") for r in D["rows"]} | set(D["earn"]["by_day"]))
    M = lambda col: f"Minutes!${col}$2:${col}${max(nmin, 2)}"
    E = lambda col: f"Earnings!${col}$2:${col}${max(nearn, 2)}"
    for i, d in enumerate(days, start=2):
        row = [d,
               f'=SUMIFS({E("I")},{E("C")},"base",{E("J")},A{i})',
               f'=SUMIFS({E("I")},{E("C")},"job",{E("J")},A{i})',
               f"=B{i}+C{i}",
               f'=SUMIFS({M("B")},{M("S")},A{i},{M("C")},1)/3600',
               f'=SUMIFS({M("O")},{M("S")},A{i})']
        for cls in ("off", "idle", "paid", "own", "both", "unattributed", "network_unpaid"):
            row.append(f'=SUMIFS({M("O")},{M("S")},A{i},{M("F")},"{cls}")')
        row += [f'=SUMIFS({M("Q")},{M("S")},A{i})', f"=N{i}*Rate", f"=D{i}-O{i}"]
        ws.append(row)
    last = len(days) + 1
    ws.append(["TOTAL"] + [f"=SUM({get_column_letter(c)}2:{get_column_letter(c)}{last})" for c in range(2, 17)])
    for c in ws[last + 1]:
        c.font = BOLD
    ws.freeze_panes = "B2"
    widths(ws, [12] + [14] * 15)
    return ws


def build_summary(wb, D, month, params, final, nmin, nearn, status_note):
    ws = wb.active
    ws.title = "Summary"
    rep, att, qa, earn = D["rep"], D["att"], D["qa"], D["earn"]
    M = lambda col: f"Minutes!${col}$2:${col}${max(nmin, 2)}"
    E = lambda col: f"Earnings!${col}$2:${col}${max(nearn, 2)}"
    ws["A1"] = f"Darkbloom electricity vs income - {month} (UTC)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("FINAL - month complete (regenerated through the first 3 UTC days of the next month to pick up late ledger rows)"
                if final else "DRAFT - month in progress; figures change until the month closes")
    ws["A2"].font = Font(bold=True, color="C00000" if not final else "006100")
    ws["A3"] = NOT_ADVICE
    ws["A3"].font = Font(bold=True)
    ws["A3"].fill = WARN_FILL
    ws["A3"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A3:H3")
    ws.row_dimensions[3].height = 48
    ws["A5"] = "PARAMETERS (edit the yellow cells; formulas recalculate)"
    ws["A5"].font = BOLD
    P = [("Month (UTC)", month, None, ""),
         ("Electricity rate $/kWh", params["rate"], "Rate", "default 0.128; set to the actual NV Energy blended rate"),
         ("Calibration multiplier", params["calibration"], "Calibration", "wall_W = multiplier * SMC_W + offset (1.0/0 = uncalibrated; SMC is not wall power)"),
         ("Calibration offset W", params["offset_w"], "OffsetW", ""),
         ("Idle baseline W", round(rep["c_baseline_w"], 2), "BaselineW", rep["c_baseline_src"]),
         ("Generated (UTC)", L.utc_iso(dt.datetime.now(dt.timezone.utc).timestamp()), None, status_note)]
    for k, (lab, val, name, note) in enumerate(P, start=6):
        ws.cell(k, 1, lab)
        c = ws.cell(k, 2, val)
        ws.cell(k, 3, note)
        if name:
            c.fill = WARN_FILL
            wb.defined_names[name] = DefinedName(name, attr_text=f"Summary!$B${k}")
    r = 13
    ws.cell(r, 1, "GROSS DARKBLOOM INCOME (archived ledger rows)").font = BOLD
    for j, h in enumerate(["", "Excel formula", "Script value", "Check", "Note"], start=1):
        ws.cell(r + 1, j, h).font = BOLD
    gross_rows = [("Base reward", f'=SUMIFS({E("I")},{E("C")},"base")', earn["base"]),
                  ("Per-job income", f'=SUMIFS({E("I")},{E("C")},"job")', earn["job"]),
                  ("Total gross", f"=B{r+2}+B{r+3}", earn["gross"])]
    for k, (lab, f, v) in enumerate(gross_rows, start=r + 2):
        ws.cell(k, 1, lab)
        ws.cell(k, 2, f)
        ws.cell(k, 3, round(v, 6))
        ws.cell(k, 4, f'=IF(ABS(B{k}-C{k})<=0.005,"OK","DIFF")')
    gross_cell = f"$B${r+4}"
    ws.cell(r + 2, 5, f"{earn['n']} ledger rows archived for this month")
    ws.cell(r + 4, 5, "Compare with the Darkbloom 1099 / console; see DATA GAPS for what the archive does not cover")
    # --- class table
    r = 19
    ws.cell(r, 1, "WHERE THE ELECTRICITY WENT (per-minute classification; see Methodology)").font = BOLD
    for j, h in enumerate(["class", "hours", "kWh", "cost $", "minutes", "script kWh", "check", "meaning"], start=1):
        ws.cell(r + 1, j, h).font = BOLD
    meaning = {"off": "Darkbloom provider not running (machine baseline)",
               "idle": "provider up, no work seen (readiness / idle baseline power)",
               "paid": "network counters advanced and no own-work signal: PAID work",
               "own": "Penn's own queue/agent work (job window, local-endpoint connection, or weak process heuristic)",
               "both": "paid AND own in the same minute: energy split by completion-token share (paid share is used)",
               "unattributed": "cannot be assigned: not forced into paid or own",
               "network_unpaid": "network counters advanced but no ledger row (counters moved but nothing was credited): not offset-eligible"}
    first_cls = r + 2
    for k, cls in enumerate(L.CLASSES, start=first_cls):
        ws.cell(k, 1, cls)
        ws.cell(k, 2, f'=SUMIFS({M("B")},{M("F")},A{k},{M("C")},1)/3600')
        ws.cell(k, 3, f'=SUMIFS({M("O")},{M("F")},A{k})')
        ws.cell(k, 4, f"=C{k}*Rate")
        ws.cell(k, 5, f'=COUNTIFS({M("F")},A{k},{M("C")},1)')
        ws.cell(k, 6, round(att["kwh"][cls], 6))
        ws.cell(k, 7, f'=IF(ABS(C{k}-F{k})<=0.0005,"OK","DIFF")')
        ws.cell(k, 8, meaning[cls])
    tot = first_cls + len(L.CLASSES)
    ws.cell(tot, 1, "TOTAL").font = BOLD
    for col in "BCDE":
        ws[f"{col}{tot}"] = f"=SUM({col}{first_cls}:{col}{tot-1})"
    ws.cell(tot, 6, round(sum(att["kwh"].values()), 6))
    ws.cell(tot, 7, f'=IF(ABS(C{tot}-F{tot})<=0.0005,"OK","DIFF")')
    # --- paid vs own split
    r = tot + 2
    ws.cell(r, 1, "PAID vs OWN SPLIT of online energy (idle + paid + own + both + unattributed + network_unpaid)").font = BOLD
    cl = {c: f"$C${first_cls + i}" for i, c in enumerate(L.CLASSES)}
    online = f"({cl['idle']}+{cl['paid']}+{cl['own']}+{cl['both']}+{cl['unattributed']}+{cl['network_unpaid']})"
    split = [("Online kWh", f"={online}"),
             ("Paid-attributable kWh (paid + paid share of both)", f'=SUM({M("P")})'),
             ("Own kWh (own + own share of both)", f"={cl['own']}+{cl['both']}-SUMIFS({M('P')},{M('F')},\"both\")"),
             ("Idle (readiness) kWh", f"={cl['idle']}"),
             ("Unattributed + network_unpaid kWh", f"={cl['unattributed']}+{cl['network_unpaid']}")]
    for k, (lab, f) in enumerate(split, start=r + 1):
        ws.cell(k, 1, lab)
        ws.cell(k, 2, f)
        ws.cell(k, 3, f'=IF($B${r+1}>0,B{k}/$B${r+1},0)')
        ws.cell(k, 3).number_format = "0.0%"
    # --- offset options
    r = r + len(split) + 3
    ws.cell(r, 1, "ELECTRICITY COST vs GROSS INCOME UNDER EACH METHOD (all shown; none is advice)").font = BOLD
    for j, h in enumerate(["method", "kWh", "cost $", "gross income $", "net income $", "script cost $", "check", "what it counts"], start=1):
        ws.cell(r + 1, j, h).font = BOLD
    o = att["opt"]
    meth = [
        ("(a) total machine", f'=SUM({M("O")})', rep["a_total_cost"], "every kWh the Mac drew, incl. own work and idle (widest; includes personal-use load)"),
        ("(b) Darkbloom online state", f'=SUMIFS({M("O")},{M("E")},"idle")+SUMIFS({M("O")},{M("E")},"serving")', rep["b"]["online"]["cost"], "provider up (idle+serving), incl. own work that overlapped"),
        ("(b) Darkbloom serving state", f'=SUMIFS({M("O")},{M("E")},"serving")', rep["b"]["serving"]["cost"], "state 'serving' - NOTE: this flag is also true during own local requests"),
        ("(c) incremental, own work excluded [static]", rep["c"]["total"]["kwh"], rep["c"]["total"]["cost"], "online watts minus idle baseline, minutes with sampler other_work flag excluded (value from script)"),
        ("Option 1: paid, incremental, no own signal", f'=SUMIFS({M("Q")},{M("F")},"paid",{M("H")},"none")', att["opt_cost"][1], "narrowest: paid minutes with NO own-work signal of any tier, watts above idle baseline"),
        ("Option 2: paid, incremental", f'=SUM({M("Q")})', att["opt_cost"][2], "paid minutes (incl. weak-heuristic overlap) + paid share of 'both', watts above idle baseline"),
        ("Option 3: paid, gross watts", f'=SUM({M("P")})', att["opt_cost"][3], "all watts in paid minutes (includes the idle floor inside each minute)"),
        ("Option 4: paid gross + online-idle", f'=SUM({M("P")})+{cl["idle"]}', att["opt_cost"][4], "option 3 plus idle/readiness power (see Methodology)"),
    ]
    for k, (lab, kwh, script_cost, note) in enumerate(meth, start=r + 2):
        ws.cell(k, 1, lab)
        ws.cell(k, 2, kwh)
        ws.cell(k, 3, f"=B{k}*Rate")
        ws.cell(k, 4, f"={gross_cell}")
        ws.cell(k, 5, f"=D{k}-C{k}")
        ws.cell(k, 6, round(script_cost, 6))
        ws.cell(k, 7, f'=IF(ABS(C{k}-F{k})<=0.005,"OK","DIFF")')
        ws.cell(k, 8, note)
    meth_end = r + 1 + len(meth)
    ws.cell(meth_end + 1, 1, "Own-work power is NOT an offset candidate under options 1-4. Only (a)/(b) include it; they are shown for reference.").font = Font(italic=True)
    # --- data gaps
    r = meth_end + 3
    ws.cell(r, 1, "DATA GAPS AND ATTRIBUTION QUALITY").font = BOLD
    ndays = calendar.monthrange(*map(int, month.split("-")))[1]
    cov_h = rep["covered_s"] / 3600
    month_h = ndays * 24
    first_pw = D["rows"][0]["ts"].strftime("%Y-%m-%d %H:%M") if D["rows"] else "n/a"
    first_er = L.entry_ts(D["entries"][0]).strftime("%Y-%m-%d %H:%M") if D["entries"] else "n/a"
    lines = [
        f"Power coverage: {cov_h:.1f} h sampled of {month_h} h in the month; first power sample {first_pw} UTC (earlier hours were not recorded, their electricity is NOT in this workbook).",
        f"Rows with macmon unavailable: {rep['rows'] - rep['power_rows']} ({rep['power_unavailable_s']/3600:.1f} h, not billed).",
        f"Earnings archive: {earn['n']} ledger rows for this month, earliest {first_er} UTC. The API returns only the latest ~1000 rows, so income before the archiver started is not itemized here.",
    ]
    tot_snap = D["totals"][-1] if D["totals"] else None
    if tot_snap:
        arch_all = sum(L.entry_usd(e) for e in D["entries"])
        lines.append(f"Account lifetime total at last snapshot ({tot_snap.get('at')}): ${tot_snap.get('total_micro_usd', 0)/1e6:,.2f} over {tot_snap.get('count')} ledger rows; "
                     f"archived rows this month sum to ${arch_all:,.2f}. The difference is income that exists but is not itemized in this file: use the Darkbloom 1099/console for the true gross.")
    lines += [
        f"Attribution: {qa['counter_minutes']} minute(s) with network counter advance, {qa['counter_minutes_with_ledger']} matched to a ledger row; "
        f"{qa['ledger_rows_in_power_coverage']} ledger rows fall inside the power-sampled period, {qa['ledger_rows_matched']} matched to a counter minute, {qa['ledger_rows_orphan_in_coverage']} orphaned.",
        f"Counter tokens in matched minutes {qa['tokens_counter_matched_minutes']} vs ledger completion tokens {qa['tokens_ledger_matched_minutes']}.",
        f"Own-work evidence: {qa['own_windows']} job windows loaded; {qa['minutes_with_own_window']} minutes overlap one, {qa['own_window_minutes_with_sampler_flag']} of those also carry the sampler heuristic flag; "
        f"{qa['flag_only_minutes']} minutes carry ONLY the weak heuristic; {qa['active_own_only_minutes']} 'own' minutes had inference_active (that flag is true for local requests too).",
        f"Unattributed minutes: {qa['unattributed_minutes']}; network-unpaid minutes: {qa['network_unpaid_minutes']}; paid+own overlap minutes: {qa['both_minutes']}.",
    ]
    for k, t in enumerate(lines, start=r + 1):
        ws.cell(k, 1, t)
    ws.cell(r + len(lines) + 2, 1, NOT_ADVICE).font = BOLD
    widths(ws, [44, 16, 16, 16, 14, 14, 10, 90])
    return ws


METHODOLOGY = [
    ("Purpose", "Bookkeeping estimate of the electricity attributable to Darkbloom network (paid) work on this Mac Studio, to set against Darkbloom income. Not tax advice; a CPA decides whether and how to use it."),
    ("Power source", "macmon SMC all_power (whole-system watts, no sudo), sampled 1/s, averaged per minute by bloom-power-log.py. Logged RAW. Calibrated watts = Calibration * raw + OffsetW (Summary). SMC is not wall power (PSU loss, other rails); defaults 1.0 / 0 are uncalibrated. Gaps over 5 s (sleep) are not billed."),
    ("Energy per minute", "kWh = calibrated_W * interval_s / 3,600,000. Months/days are UTC."),
    ("Rate", "Summary!Rate, default $0.128/kWh. Replace with the actual blended utility rate from the bill."),
    ("Paid evidence (strongest)", "daemon-state.json stats.requests_served / tokens_generated deltas per minute. Measured on this machine: these counters did not move for 70+ local-endpoint calls by ollama-worker, and moved 1:1 with ledger rows (provider session of 2026-10-13: 20 requests = 20 per-job ledger rows; 68,848 tokens = the ledger's completion tokens exactly, e.g. one 64,000-token job whose ledger row was stamped within seconds of the counter advance). So they count network-side requests only."),
    ("Ledger rows (Earnings sheet)", "From the account-earnings API, archived every 15 min, deduped by ledger id. Rows carry created_at (settlement time), model, prompt/completion tokens, amount. They carry NO duration and NO start time, so they confirm a minute was paid but cannot place a job inside a minute. model=base_reward / job_id floor:* are per-epoch base rewards (not tied to any request)."),
    ("Matching", "Each per-job ledger row is assigned to exactly one counter-advance minute (the latest ending at/before the row's timestamp, within 120 s, else the one containing it). A counter-advance minute with no ledger row 900 s after it ended is 'network_unpaid' (not offset-eligible); newer ones stay 'paid' until settled."),
    ("Own evidence", "A: job window from ollama-worker dispatch-metrics.jsonl where host = 127.0.0.1:8000 (start = timestamp - wall_time_s, completion tokens) or a finished queue job in the Darkbloom lane (studio-db: launch time to log mtime). B: a non-Darkbloom process held a connection to 127.0.0.1:8000 during the minute (sampler). C: only the process-name / >150% CPU heuristic (weak: may not touch Darkbloom at all)."),
    ("Why inference_active is not used as 'paid'", "It is true while Darkbloom runs ANY request, including local ones. Observed: 100% of sampled minutes with local own jobs show inference_active, with zero network counter movement."),
    ("Classes", "off = provider not running. idle = up, nothing seen. paid = counters advanced, no strong own signal (A/B). own = A/B signal without counters, or weak C-only. both = counters AND strong own signal in one minute: energy split by completion-token share (network tokens_delta vs own job tokens apportioned by time overlap); no token data -> unattributed. unattributed = inference active with no counters and no own evidence (e.g. a long request whose counter has not advanced), or paid+own with no way to split. network_unpaid = see Matching."),
    ("Long requests", "Counters advance when a request finishes. An active minute with no counter/own evidence whose neighbouring minute has counters is classed paid; otherwise unattributed."),
    ("Idle baseline", "Idle baseline W (Summary!BaselineW) = median calibrated watts of clean Darkbloom-OFF minutes, else a flagged 10th-percentile estimate from online-idle minutes (which UNDERSTATES incremental power because loaded models are already resident). Edit the cell to override."),
    ("Idle / readiness power", "Shown as its own class. Offset options: (1/2) count only watts ABOVE the idle baseline during paid minutes (narrowest, most conservative); (3) all watts in paid minutes; (4) option 3 plus idle online power, on the view that being online and ready is part of earning the income. Which view is appropriate is for the CPA. Own-work and unattributed energy is never included in options 1-4."),
    ("Known limits", "1) Electricity before the sampler started is not in the workbook. 2) Ledger rows before the archiver started are not itemized (the API keeps ~1000 rows). 3) Minute resolution: a 2 s paid request inside a minute is credited with the whole minute's energy under option 3 (option 1/2 subtract the baseline). 4) Own tokens inside a minute are apportioned by time overlap of the job window, not true token timing. 5) Weak heuristic C may flag unrelated CPU load. 6) SMC watts are not metered wall power. 7) Ledger settlement lag is observed, not documented (Matching section of the Summary reports it once data exists). 8) Only completion tokens are used for the split; prompt processing power is not weighted."),
    ("Regeneration", "Idempotent: rerunning for a month rewrites that month's file. DRAFT while the month is open; FINAL once complete (the month is regenerated as FINAL on each run during the first 3 UTC days of the next month to absorb late ledger rows)."),
    ("Checks", "Headline cells have a static 'script value' and an OK/DIFF cell so a reviewer can see the Excel formulas reproduce the script's numbers. openpyxl does not store formula results, so values appear once the file is opened in Excel/Numbers/Sheets."),
    ("Disclaimer", NOT_ADVICE),
]


def build_methodology(wb):
    ws = wb.create_sheet("Methodology")
    ws.append(["Topic", "Assumption / source / limit"])
    style_header(ws)
    for t, d in METHODOLOGY:
        ws.append([t, d])
    for row in ws.iter_rows(min_row=2):
        row[0].font = BOLD
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    widths(ws, [30, 150])


def build_workbook(month, state_dir, params, final=None, now=None, metrics_path=None, queue_path=None):
    D = gather(month, state_dir, params, metrics_path, queue_path)
    final = month_is_final(month, now) if final is None else final
    wb = openpyxl.Workbook()
    ws_min, nmin = build_minutes(wb, D)
    ws_earn, nearn = build_earnings(wb, D)
    build_summary(wb, D, month, params, final, nmin, nearn, "FINAL" if final else "DRAFT")
    build_daily(wb, D, month, nmin, nearn)
    build_methodology(wb)
    wb._sheets = [wb["Summary"], wb["Daily"], wb["Minutes"], wb["Earnings"], wb["Methodology"]]
    wb["Summary"].sheet_properties.tabColor = "006100" if final else "C00000"
    wb.properties.title = f"Darkbloom electricity vs income {month} ({'FINAL' if final else 'DRAFT'})"
    wb.properties.description = "Bookkeeping estimate, not tax advice."
    return wb, D, final


def generate(month, state_dir, params, root, local_base=None, local_only=False, final=None, now=None,
             metrics_path=None, queue_path=None):
    """Build + write one month. Returns dict(local, proton, warning, final)."""
    wb, D, final = build_workbook(month, state_dir, params, final, now, metrics_path, queue_path)
    year = month[:4]
    name = f"Darkbloom-{month}.xlsx"
    lb = Path(local_base) if local_base else Path(state_dir) / L.POWER_DIRNAME / "workbooks"
    ldir = lb / year
    ldir.mkdir(parents=True, exist_ok=True)
    stage = ldir / f".{name}.build{os.getpid()}"
    wb.save(stage)
    local = ldir / name
    os.replace(stage, local)                            # atomic local master
    proton, warn = (None, None) if local_only else publish(local, root, year, name, state_dir)
    return {"local": local, "proton": proton, "warning": warn, "final": final, "data": D}


def month_before(month):
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--month", help="YYYY-MM (UTC)")
    ap.add_argument("--auto", action="store_true", help="current month DRAFT + previous month FINAL in the first 3 UTC days")
    ap.add_argument("--rate", type=float)
    ap.add_argument("--calibration", type=float)
    ap.add_argument("--offset-w", type=float)
    ap.add_argument("--baseline-w", type=float)
    ap.add_argument("--root", help="sync root (default: $BLOOM_WORKBOOK_ROOT, workbook.json, live Proton Drive root)")
    ap.add_argument("--state-dir", default=str(L.HOME_DIR))
    ap.add_argument("--local-only", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.state_dir)
    params = {k: (getattr(a, k) if getattr(a, k) is not None else cfg.get(k, DEFAULTS[k])) for k in DEFAULTS}
    root = resolve_root(a.root, a.state_dir)
    now = dt.datetime.now(dt.timezone.utc)
    if a.auto:
        months = [now.strftime("%Y-%m")]
        if now.day <= 3:
            months.insert(0, month_before(months[0]))
    elif a.month:
        try:
            calendar.monthrange(*map(int, a.month.split("-")))
        except (ValueError, TypeError):
            print("--month must be YYYY-MM", file=sys.stderr)
            return 2
        months = [a.month]
    else:
        months = [now.strftime("%Y-%m")]
    for m in months:
        res = generate(m, a.state_dir, params, root, local_only=a.local_only, now=now)
        print(f"{m} {'FINAL' if res['final'] else 'DRAFT'}: local {res['local']}"
              + (f" | proton {res['proton']}" if res["proton"] else " | proton: NOT WRITTEN"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
