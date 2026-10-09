# Review: lib/rivian.ts

## VERDICT: PASS WITH CAVEATS
_1 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `lib/rivian.ts:1432` | normalizeRivianServiceState does not validate that input.threads is an array before passing it to pi |

## Uncertain — needs a human

### [HIGH] normalizeRivianServiceState does not validate that input.threads is an array before passing it to pickActiveWorkOrder, causing a runtime TypeError if threads is null, undefined, or a non-array object.
`lib/rivian.ts:1432`

```
const active = pickActiveWorkOrder(input.threads);
```
**How it fails:** A user has no active work order and the API returns `threads: null` (or `undefined`). The call to `pickActiveWorkOrder(null)` executes `for (const thread of threads)` which throws `TypeError: threads is not iterable`. The UI receives an unhandled exception instead of a valid snapshot with `inService: false` and empty requests, potentially crashing the service status view.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 113s of model time
- Wall clock 113s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*