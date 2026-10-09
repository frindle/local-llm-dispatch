# broker-guard

Self-hosted data-broker **monitoring + auto-removal + auto-escalation** loop
(self-hostable equivalent of the Incogni/Cloaked monitoring layer). Watches for
a person's own identity re-appearing across people-search / data-broker sites,
drives removals, and escalates when brokers miss statutory deadlines.

## Pipeline
profile -> brokers -> serpwatch -> playwright_checks -> state -> alert -> eraser_bridge -> health -> escalation -> orchestrator -> scheduler

- **profile**: identity model + config load + query/name variant generation
- **brokers**: load + normalize `data/brokers.json`
- **serpwatch**: build + run SearXNG queries per broker+identity, detect hits
- **playwright_checks**: per-site presence checks on top people-search sites
- **state**: SQLite presence history + new-appearance diff
- **alert**: notify on newly-detected appearance (batched per-run digest)
- **eraser_bridge**: invoke vendored `eraser` (github.com/drumandbytes/eraser, MIT) removal engine + re-verify
- **health**: SELF-MONITORING -- per-broker status state machine, failure
  classification (broker-side vs tool-side automation drift), a self-heal hook
  that emits dispatch-ready task files for drift into `needs-fix/`, heartbeat /
  dead-man's switch, and JSON-lines + human-readable status reports
- **escalation**: AUTOMATED LEGAL ESCALATION -- SLA timers stamped from
  `escalation-rules.json` (CCPA/CPRA, GDPR, VCDPA/CPA/CTDPA/UCPA/TDPSA, NV NRS
  603A, FCRA, DPPA, FTC backstop) per residency; auto-fill escalation letters;
  tiered auto-send (broker-facing follow-up = auto; regulator complaint =
  prepare-but-confirm, `auto_file_regulator_complaints: false` by default; FCRA
  freeze/dispute = human-confirm); full PII audit trail
- **orchestrator**: the full loop
- **scheduler**: launchd/cron wiring

## Reuse (do not reimplement)
- Removal engine: **eraser** (vendored under `vendor/eraser/`), called via CLI.
- Broker seed data: `data/brokers.json` (maintained in the SEPARATE PUBLIC repo; pulled at runtime; contains NO personal data).
- SERP engine: self-hosted **SearXNG**.

## Privacy split
- PUBLIC (separate repo): the anonymized broker dataset (`brokers.json`, opt-out methods) and `escalation-rules.json`.
- PRIVATE (this repo): the tool + everything tied to the owner's identity. `profile.local.json`, state DB, logs, serpwatch hits, snapshots, removal/escalation records, and the `needs-fix/` dispatch queue are all gitignored. Copy `profile.example.json` -> `profile.local.json`.

Module code is authored via the ollama-dispatch verify-gated pipeline (qwen on studio).
