# TASK: aw-sched-runner-s11-client-none-seatsaerocli

## Confirmed defect (observed, not suspected)

`search_schedule(sched)` with the default `client=None` raises
`AttributeError: 'NoneType' object has no attribute 'search'`. Reproduced by
calling `src/scheduled_runner.py::search_schedule({"origin": "LHR",
"destination": "JFK"})`: line 74 executes `client.search(...)` on the default
`client=None`, so any caller that omits `client` crashes instead of getting a
real search.

## Entry point

src/scheduled_runner.py:72 (`def search_schedule(sched: Dict, client=None, today=None)`)

## Required change

In src/scheduled_runner.py, fix search_schedule(sched, client=None, today=None): currently it calls client.search(...) directly, which raises AttributeError when client is None (the default). Change it to use `client or SeatsAeroClient()` so callers that omit client get a real SeatsAeroClient instance instead of crashing.

Behaviour that must NOT change:
- An explicitly passed `client` is still the one used for `.search(...)` -- no new client may be constructed when one was provided, and its result list is returned unchanged.
- The schedule fields (`origin`, `destination`, `start_date`, `end_date`, `cabins`, `programs`) are forwarded to `client.search(...)` in the same order as before; missing keys still forward as `None`.
- The return value stays a `list` of whatever `search` returned.
- Everything else in the module (`select_results`, `run_cycle`, `search_seats_aero`, `get_trip`) keeps working exactly as it does now.

## Must contain

- `client or SeatsAeroClient()`
- `def search_schedule(sched: Dict, client=None, today=None) -> List[Dict]:`

## Scope

Only edit `src/scheduled_runner.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

The one new line (`c = client or SeatsAeroClient()`) is asserted from both
sides by the fixture: with an explicit client it must NOT construct a new one,
and with `client=None` it must construct exactly one and use its result. Do not
split the fix across extra lines that no case asserts.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
