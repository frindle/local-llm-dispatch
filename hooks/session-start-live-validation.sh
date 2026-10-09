#!/bin/bash
# SessionStart: surface SHIPPED-BUT-UNVALIDATED fixes -- ones whose correctness
# only a live run can confirm (a scraper that must read a real page, a dashboard
# header validated "on the next sync"). Sibling of session-start-handoff-panel.sh:
# the handoff board tracks a fix to merge, this tracks it to its LIVE check.
#
# FAILED items (a shipped fix that live data proved wrong) are the loud case and
# always surface. Pending items are a gentle reminder. Zero open items -> silent,
# no noise. Read-only: the ledger's `list --json` writes nothing.
# Degrades to silence on any error -- it must never throw into a session.
set -euo pipefail
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HOME/bin/live-validation-ledger.py" ] || exit 0

msg="$(python3 "$HOME/bin/live-validation-ledger.py" list --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    op = d.get("open") or []
    failed  = [i for i in op if i.get("status") == "failed"]
    pending = [i for i in op if i.get("status") in ("pending", "pending-fix")]
    if not failed and not pending:
        sys.exit(0)  # silent
    lines = ["LIVE-VALIDATION LEDGER: shipped fixes still awaiting their live check "
             "(a fix is not truly done until its live validation passes)."]
    if failed:
        lines.append("")
        lines.append("*** %d FAILED live validation -- a shipped fix that did NOT work. Fix or re-dispatch: ***" % len(failed))
        for i in failed:
            lines.append("  - [%s] %s (%s): %s" % (i.get("id",""), i.get("label",""), i.get("repo",""), i.get("what","")))
            if i.get("resolution_note"):
                lines.append("      why it failed: %s" % i["resolution_note"])
    if pending:
        lines.append("")
        lines.append("%d awaiting live validation (gentle reminder -- check when the live event next occurs):" % len(pending))
        for i in pending:
            tag = " [FIX NOT SHIPPED YET]" if i.get("status") == "pending-fix" else ""
            lines.append("  - [%s] %s (%s)%s -- how: %s" % (i.get("id",""), i.get("label",""), i.get("repo",""), tag, i.get("how","")))
    lines.append("")
    lines.append("Mark each once checked: python3 ~/bin/live-validation-ledger.py pass|fail <id> --note \"...\"")
    print("\n".join(lines))
except SystemExit:
    raise
except Exception:
    pass  # any parse trouble -> emit nothing
' 2>/dev/null || true)"

# No open items (or any failure above) -> stay silent.
[ -n "$msg" ] || exit 0

jq -n --arg m "$msg" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $m
  }
}'
exit 0
