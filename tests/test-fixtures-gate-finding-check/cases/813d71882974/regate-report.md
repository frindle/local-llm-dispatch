# Review: broker_guard/brokers.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `broker_guard/brokers.py:30` | The code assumes `out["id"]` is a string and calls `.lower()` on it, but the contract explicitly sta |
| 2 | MEDIUM | `broker_guard/brokers.py:19 (removed)` | The guard `elif rid not in seen:` was deleted, making `raise ValueError(` unconditional. The fall-th |

## Defects

### [HIGH] The code assumes `out["id"]` is a string and calls `.lower()` on it, but the contract explicitly states that non-string values (e.g. numbers) pass through unchanged, so a numeric `id` will raise an `AttributeError` instead of being preserved.
`broker_guard/brokers.py:30`

```
out["id"] = out["id"].lower()
```
**How it fails:** A broker record with `"id": 123` (an integer) passes the required-field check (since `"id"` is in the record), the normalization loop leaves `out["id"]` as the integer `123`, and then `out["id"].lower()` raises `AttributeError: 'int' object has no attribute 'lower'` instead of returning the record with `id` as `123`.

### [MEDIUM] The guard `elif rid not in seen:` was deleted, making `raise ValueError(` unconditional. The fall-through path it protected is now unreachable.
`broker_guard/brokers.py:19 (removed)`

```
elif rid not in seen:
```
**How it fails:** Previously, when `elif rid not in seen:` was false, control fell through to the code after this block; now it never does. Whatever that fall-through reached no longer runs for any input, and any local used only by the deleted condition is now dead. CHECK THE FALL-THROUGH TARGET to judge severity: if it merely reached another equivalent return this is dead code, but if it reached a refresh, retry, fetch, or a different return shape, that behaviour is lost. The harness can prove the guard is gone; it cannot prove from this hunk alone what the lost path did.
**Verified trigger:** any input for which `elif rid not in seen:` was previously false

## Rejected by verification

Raised during review, then knocked down when checked against the code by a pass that did not write them. Listed for audit, not for action.

- ~~The pre-gate finding is REFUTED. The deleted line was part of a deduplication mechanism (keeping the first occurrence of each id and discarding subsequent duplicates). The new code does not deduplicate, but the stated task contract explicitly requires validation and normalization, not deduplication. The new code correctly raises ValueError for missing required fields and normalizes strings/ids as specified. The absence of deduplication is not a defect introduced by this change because the new contract does not require it, and the old deduplication logic was replaced by the new validation/normalization logic as intended by the task. The pre-gate's claim that this deletion introduces a defect is incorrect because the new behavior aligns with the required change specification.~~ — The finding itself states that the deletion is consistent with the stated intent and that no defect is introduced. The task contract explicitly requires validation and normalization, not deduplication. The new code corre

## How this was produced

- Model `qwen3.8:27b-q4_K_M`, num_ctx 98,304, think=off — 4 calls, 86s of model time
- Wall clock 86s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario, 1 rejected by verification

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*