# local-llm-dispatch

A single-folder copy of the owner's local-LLM dispatch pipeline, assembled so it can be reviewed as one unit
(`/code-review ultra` bundles the local branch; no remote needed). **This is a COPY.** Nothing live was moved,
re-symlinked or restarted. The live code still runs from `~/bin` (see "Where it runs from").

## Layout
| dir | contents |
|---|---|
| `bin/` | pipeline scripts, original filenames (no `.py` on the `ollama-dispatch-*` / `diagnosis-*` tools). Also helper dirs imported by them: `verify-claims/`, `dispatch-fixes/`, `dispatch-templates/`, `ts-mutator/` (JS/TS AST mutator, no `node_modules`), `ollama-dispatch-plan.fixtures/` |
| `dashboard/` | the `ollama-queue-dashboard` repo: `src/` (`ollama-queue-api.py` HTTP API + UI, `bundle_view.py`, `dashboard_chat.py`, `runstatus_retention.py`), `tests/`, docs, Docker + deploy files, `bin/` (only the two files unique to that repo: `backend-dispatch.py`, `servers_config.py`) |
| `tests/` | every non-symlinked `~/bin/test-*.py` plus the fixture dirs they use. Dashboard tests live in `dashboard/tests/` |
| `config/` | `defaults.json`, `model-ladder.json`, signoff rules, diagnostics catalog, model prompt/template files |
| `launchd/` | LaunchAgent plists: queue daemon, queue API, ack-reconcile, escalation-watcher |
| `hooks/` | Claude Code hooks that guard/remind the dispatch workflow (troubleshoot + hand-author gates, worktree guard, session-start panels) |
| `harness/` | older/auxiliary material: `ollama-dispatch-pkg/` (early package-style orchestrator), `bakeoff/` + `ollama-bakeoff/` (model bake-off harness, designs, preregistrations; result data omitted), `agent-dispatch/` (agent-dispatch harness + MCP; results/fixtures omitted) |
| `docs/` | proposals (`~/.ollama-dispatch/proposals`) and the earlier `llm-dispatch` migration note |

Top level: `MANIFEST.txt` (live path for every tracked piece), `SYNC.sh`, `scrub.sh`, `run-tests.sh`, `.gitignore`.

## How the pieces connect
```
 task spec + verify ──> ollama-dispatch-slice ──(per slice)──> ollama-dispatch-auto
                         (omnibus_slice.py, slice_obligations.py)      │ scaffold -> draft -> preflight (both-ways verify,
                                                                       │ verify-relevance.py) -> enqueue
                                                                       v
   ollama-queue.py (daemon, `run`)  <── enqueue / status / resolve / resume (also qctl, queue-submit, HTTP API)
     per-lane serialization, state JSON, GPU-exclusive scheduling, Darkbloom/Unraid lanes
        │ launches
        v
   ollama-worker.py  ── drives the model (Darkbloom / Ollama, OpenAI-compatible) in the job worktree, runs verify
        │ on completion (queue hook)
        v
   gate-on-complete.py ── pregate + relevance/completeness/identity checks (gate_identity.py, gate_finding_check.py),
        │ auto-fix via model ladder (config/model-ladder.json); PASS -> ollama-dispatch-integrate (land), FAIL -> retry/escalate
        v
   handoff-emit.py ── run-status / hand-off records; dispatch-escalation-watcher.py + dispatch-self-heal.py +
                      dispatch-ack-reconcile.py + dispatch-worktree-reap keep it moving unattended
   dashboard/src/ollama-queue-api.py ── reads queue state + bundle_view.py; web UI on :7684
```
Supporting: `harness_lint.py` (pre-run spec validator, docs/harness-lint.md), `pipeline-canary.py` (end-to-end canary), `live-validation-ledger.py`, `dispatch-diagnostics.py`,
`diagnosis-*` (root-cause harness), `goose-queue-proxy.py` (Goose through the queue), bake-off tools, `unraid-*.sh`.
`hooks/` enforce "troubleshoot/author through the queue (qwen), not by hand".

## Where it runs from
- Pipeline scripts: `~/bin/<name>` (real files), including `ollama-queue.py`, `ollama-worker.py`, `ollama-dispatch-*`, `gate-on-complete.py`, `handoff-emit.py`.
- Dashboard: `~/bin/ollama-queue-api.py`, `bundle_view.py`, `dashboard_chat.py`, `runstatus_retention.py` and several `test-*.py` are **symlinks** into `~/Desktop/GitHub Projects/ollama-queue-dashboard/src|tests`; here they appear under `dashboard/`.
- LaunchAgents: `~/Library/LaunchAgents/com.example.*.plist`. Hooks: `~/.claude/hooks/`. Config/state: `~/.ollama-dispatch/` (only config and proposals are copied; state, worktrees, decisions, logs are not).
- Tests are written to sit beside the scripts (as in `~/bin`); use `./run-tests.sh` (builds a sandboxed overlay under a fake `$HOME`).

## Refreshing: `./SYNC.sh [--dry-run]`
One-way live -> repo using `MANIFEST.txt` (`<repo dir>|<live path>`). Strips `*.bak*`, `__pycache__`, `node_modules`, logs, locks, `.env*`; then runs `scrub.sh` and aborts if private-key/token-shaped strings appear. Then `git diff`, commit.

`scrub.sh [dir]` (idempotent; also safe to run by hand) removes personal data from the tree: the owner's name (prose becomes "the owner"; identifiers such as the sign-off reviewer become `owner`), the account name and home directory (`/Users/user`), launchd label prefixes (`com.example.`; files named after the old labels are renamed to match, so `launchd/` holds `com.example.*.plist`), personal host names, private domains (`*.example.com`), and every private IPv4 address (10/8, 172.16/12, 192.168/16), which is mapped by a stable hash into the RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) so the same input always yields the same placeholder and no real address is stored in the script. `MANIFEST.txt` uses globs (`com.*.<name>.plist`, `notify-[a-z]*.py`) for live files whose names carry such labels, so the tracked tree never contains them.

## Deliberately NOT included
Queue state/DBs, logs, livelogs, run state (`decisions.jsonl`, bundle-attention/superseded, stall/peak caches), worktrees, `*.bak*`/`.orig`, `~/.ollama-dispatch/hosts.json` (real host IP; see `harness/ollama-dispatch-pkg/hosts.json.example`), stale worker snapshots (`ollama-worker-v7/v8.py`), stale Docker-deploy copies in the dashboard repo's `bin/`, the old `llm-dispatch` snapshot (superseded by `~/bin`), bake-off result data, personal/resell/media/vault tools.

## Safety notes
No secrets are tracked. Code reads credentials at runtime from the Keychain or `~/.darkbloom/local.json`; test files contain obvious placeholders only. Internal LAN addresses are replaced by documentation-range placeholders (192.0.2.x, 198.51.100.x, 203.0.113.x); set the real hosts through config/env when running this outside the original environment.

## Test status at snapshot (2026-10-08)
`./run-tests.sh`: 199 of 207 sandboxed tests pass. The 8 failures are the known non-hermetic ones listed in the header of `run-tests.sh` (they read live `~/.ollama-dispatch` plans/logs or `.bak` files, or are too slow for the 90s default). Skipped by design (run by hand): heavier/side-effecting tests and `test-selfcheck-hang.py` (~20 min).

## Added in the 2026-10-09 update
CPU lane fixes (relevance stage now eligible and correct on the runner: shipped args file, the git-excluded `.dispatch-harness.json` restored there; a CPU-only chain stage such as preflight no longer holds the GPU lanes, `cpu_wait` park with a yield-back to the returning bundle), escalation-review bundling (a `needs-opus-auto-X` placeholder is born with its real bundle, `ollama-queue.py retag-bundles [--apply]` janitor, esc-review rows never own the lanes; test `test-esc-review-bundle.py`), and the agent-budget policies (`worker_budget.py`, `budget-policy-ab*`, `docs/budget-policies.md`). Bounded, evidence-gated escalation reviews (no model spawned without evidence; token cap, repeat-call and huge-`list_files` bounds; `REVIEW CAPPED` / `NO EVIDENCE AVAILABLE` reviews never acted on; `test-esc-review-bounds.py`) and the default-OFF sampling escalation ladder A/B arm (`sampling_escalation:` in `model_profiles.yaml`, `SamplingLadder`, `docs/sampling-escalation-ab.md`, `test-sampling-ladder.py`).

## Added in the 2026-10-08 update
Worker robustness (`worker_robust.py`, `model_profile.py` + `model_profiles.yaml`), harness gates (`dispatch_harness_gates.py`), BloomGauge/Darkbloom queue hooks (`bloom_control.py`), the CPU lane (`cpu_lane.py`, `cpu_dispatch.py`, `cpu_job.py`, `cpu_stage.py`, `cpu_vr_measure.py`), CPU prefetch (`cpu-prefetch.py`: starts queued work's CPU prep early on free CPU so the GPU never waits; `docs/cpu-prefetch.md`), `handoff-triage.py`, and their tests and docs (`docs/`). The CPU runner container is its own public repo: https://github.com/frindle/dispatch-cpu-runner (image `ghcr.io/frindle/dispatch-cpu-runner`).
