# Worker model-output robustness (Phase 3)

`bin/worker_robust.py` (pure helpers) + seams in `bin/ollama-worker.py`. Tests:
`bin/test-worker-robust.py` (`--revert-check` mutates the worker/module and requires RED),
canary seams `robustrequery`, `robustloop`, `robustrunaway` (proven via `pipeline-canary.py --prove`).
Knobs live in `bin/model_profiles.yaml` under `robust_defaults:` (per-model `robust:` overrides);
`get_profile(...)["robust"]` returns the merged dict.

## 1. Layered tool-call repair
Native `tool_calls` first. Otherwise the content is parsed, in order: the worker's legacy parser
(inline JSON / Mistral `[TOOL_CALLS]` / complete Qwen3-Coder XML), then `repair_tool_calls`:
Qwen3-Coder XML (tolerates missing `</parameter>`, `</function>`, `</tool_call>`), Hermes JSON in
`<tool_call>` (tolerates trailing text, a missing closer, JSON cut mid-string), inline JSON with
string-encoded `arguments` / `function` wrappers, and an OPTIONAL small-model extractor
(`robust.tool_extractor: true`, off by default; only runs when everything else found nothing and the
text looks like a call attempt). Closed `<think>` blocks, an unterminated `<think>`, and a stray
`</think>` prefix are stripped before parsing (a call quoted in reasoning is not a call). A call that
ran to end-of-input with no closer is only accepted for read-only tools; a truncated `write_file`
/ `run_bash` / `edit_file` is REFUSED (never applied) and re-prompted.

## 2. Format-error requery
An unparseable attempt (truncated, unknown tool, bad JSON, dangling `</tool_call>`, ...) gets a
templated error naming the exact problem, plus the correct format. It does NOT consume an iteration
(`i -= 1`) for up to `robust.format_requery_max` (3) consecutive errors; the stored bad call is
replaced by a stub (no lock-in, no context bloat). The 4th ends the run: **`repeated_format_error`**.

## 3. Loop / convergence detector
`LoopDetector` watches repeated identical reads (a mutation resets the count), repeated identical
writes, A->B->A write thrash, and N iterations with nothing new; the job's own verify is exempt.
First detection injects one `[loop detected]` message; the next ends the run: **`loop_detected`**
(an ignored warning that produced genuinely new bytes is treated as progress). Defaults
`repeat_read 5, repeat_write 4, aba_returns 3, no_progress_iters 8, min_iter 4`, `verify_spin 0` (off);
override per profile (`robust.loop_detector`) or env `WORKER_LOOP_<KEY>` (`WORKER_LOOP_ENABLED=0` off).
Calibration (3,242 recorded transcripts): ~0.7% of converged runs would be stopped, ~2-3% of
non-converged ones are caught (~10 iterations saved each) -- loops are a minority of non-convergence;
most of it is wandering, which the stop-gate/spec side addresses.

## 4. Stop-gate
`task_complete` is accepted only if whatever the existing verify gate decided AND the spec's required
files (the `## Entry point` file, `## Required files`) exist and are non-empty AND every `## Must contain`
literal is present (bare bullet: the entry file or any file changed this run; `in <path>:` pins it).
All criteria are re-checked on every claim. Refusals name exactly what failed; `robust.stop_gate_max`
(3) refusals end the run: **`stop_gate_failed`** (and the accept-on-verify-pass rescue does not override
it). `WORKER_STOP_GATE=0` disables. No stated criteria -> the gate stays out of the way.

## 5. Runaway reasoning
The streaming lanes abort a turn that spent `robust.reasoning_budget_chars` (24000) thinking with no
content and no tool call (`ChatAbortedForReasoningRunaway`); the turn is retried ONCE with thinking
disabled (profile non-thinking mode) and a "decide now" message. A second runaway ends the run:
**`reasoning_runaway`**. A think-cap truncation (non-streaming) likewise recovers with thinking off.

## 6. Bash-only mode (`robust.bash_only: true`, off, coding tasks)
One ```bash block per turn becomes a `run_bash` call; `echo TASK_COMPLETE` alone becomes
`task_complete` (all gates apply); zero/multiple blocks are format errors (free requery). Uses the
manual-tools plumbing; no native tools are sent.

## Exit reasons
`repeated_format_error`, `loop_detected`, `stop_gate_failed`, `reasoning_runaway` are printed as
`[worker] TERMINAL REASON: <name>`. `failure_ledger.classify` tags them (`format-error-loop`,
`loop-detected`, `stop-gate-failed`, `reasoning-runaway`) with repeat-failure prompt hints. The queue's
`classify_failure` (`ollama-queue.py` `_FAILURE_CONTEXT_REASONS`) must list them so they classify as
`context` (not `model`).
