# Review: lib/bfmrVanished.ts

## VERDICT: PASS
_nothing survived verification._

**No defects found.**

1 candidate finding(s) were raised during review and every one was rejected by the verification pass as not a real defect. On a clean change this is the expected result.

## Rejected by verification

Raised during review, then knocked down when checked against the code by a pass that did not write them. Listed for audit, not for action.

- ~~The code incorrectly uses row.reserveId directly instead of computing its head as in the lookupItems processing~~ — harness override: the verifier's own probe showed identical results for old and new code at every value it tested. The code uses row.reserveId directly without normalizing its case, while the lookupItems processing norma

## How this was produced

- Model `qwen3:14b`, num_ctx 6,144, think=off — 2 calls, 90s of model time
- Wall clock 90s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario, 1 rejected by verification

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*