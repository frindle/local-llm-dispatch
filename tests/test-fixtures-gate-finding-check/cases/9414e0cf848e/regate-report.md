# Review: src/scheduled_runner.py

## VERDICT: PASS WITH CAVEATS
_1 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `src/scheduled_runner.py:75` | The change introduces a new dependency on the name `SeatsAeroClient` in the module scope of `src/sch |

## Uncertain — needs a human

### [HIGH] The change introduces a new dependency on the name `SeatsAeroClient` in the module scope of `src/scheduled_runner.py`; if that name is not imported or defined in this module, the function will raise `NameError` on every call where `client` is falsy (i.e., the default `None`), which is the exact case the change is meant to fix.
`src/scheduled_runner.py:75`

```
c = client or SeatsAeroClient()
```
**How it fails:** A caller invokes `search_schedule({"origin": "LHR", "destination": "JFK"})` with the default `client=None`. The expression `client or SeatsAeroClient()` evaluates `SeatsAeroClient()`. If `SeatsAeroClient` is not in the module's global namespace (e.g., it was never imported in `scheduled_runner.py`), Python raises `NameError: name 'SeatsAeroClient' is not defined` instead of returning a list of search results. The original code raised `AttributeError` on `None.search`; the new code raises `NameError` on an undefined symbol — both are crashes, and the fix does not achieve its stated intent of returning a real search result.

## How this was produced

- Model `qwen3.8:27b-q4_K_M`, num_ctx 98,304, think=off — 2 calls, 41s of model time
- Wall clock 41s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*