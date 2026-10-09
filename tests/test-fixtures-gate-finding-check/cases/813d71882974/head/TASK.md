# TASK: bg-brokers-s1-load-s1-url-else-valueerror-norm

## Confirmed defect (observed, not suspected)

`load_brokers` in `broker_guard/brokers.py` currently takes an already-parsed
dict and only dedupes by id. It never validates that each broker carries the
required fields and never normalizes anything. Reproduced: feeding it a payload
whose entries lack `name`/`url` returns those broken records unchanged, and a
padded entry like `"id": "  ABC "` comes back unstripped and unlowercased
instead of being normalized to `"abc"`.

## Entry point

broker_guard/brokers.py:5 (`load_brokers`)

## Required change

In broker_guard/brokers.py: `load_brokers(path)` reads the JSON document at
`path` (shaped like `{"brokers": [...]}`) and returns the list of normalized
broker objects. Exact contract:

- The top level must be a JSON object with a `'brokers'` key; anything else --
  a bare list, or an object without that key -- raises `ValueError`.
- Every entry in the list must have `id`, `name` and `url`; if any of the three
  is missing, raise `ValueError`.
- Normalize each entry: strip surrounding whitespace from every string value in
  the record, and lowercase `id`. Non-string values (e.g. numbers) pass through
  unchanged; extra per-broker keys are kept.

Behaviour that must NOT change:
- A valid document with an empty `'brokers'` list returns `[]` without raising.
- Extra per-broker fields (`category`, `optout_url`, ...) survive normalization,
  only whitespace-stripped when they are strings.

## Must contain

- `def load_brokers(path):`
- `"brokers"`
- `ValueError`
- `.strip()`
- `.lower()`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

## Scope

Only edit `broker_guard/brokers.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
