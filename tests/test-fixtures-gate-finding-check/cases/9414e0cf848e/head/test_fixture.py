"""Adversarial fixture for: aw-sched-runner-s11-client-none-seatsaerocli

The target uses relative imports (`from .seats_aero import ...`) and two
sibling modules that do not exist in this slice (alert_filters,
scheduled_searches), so it cannot be exec'd as a bare top-level module. We
load it inside a synthetic package whose sibling slots are stubbed; the real
SeatsAeroClient is replaced per-case with a recording fake so no network or
credentials are touched and we can assert WHICH client was used.

Each case: (description, callable_returning_actual, expected)
"""
import importlib.util
import sys
import types


def _load_target():
    pkg = types.ModuleType("awpkg")
    pkg.__path__ = ["src"]
    sys.modules["awpkg"] = pkg

    stubs = {
        "alert_filters": {"passes_filters": lambda r, f: True},
        "deeplinks": {"seats_aero_url": lambda *a, **k: ""},
        "pushover": {"send_award_notification": lambda *a, **k: None},
        "scheduled_searches": {n: (lambda *a, **k: None) for n in (
            "effective_programs", "is_due", "load_schedules",
            "query_legs", "upsert_schedule")},
    }
    # seats_aero is stubbed too so the module-level import binds a name we can
    # patch per-case; the real client must never be constructed unpatched.
    class _UnpatchedClient:
        def __init__(self, *a, **k):
            raise AssertionError("real SeatsAeroClient constructed without a test patch")
    stubs["seats_aero"] = {"SeatsAeroClient": _UnpatchedClient}

    for name, attrs in stubs.items():
        m = types.ModuleType("awpkg." + name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules["awpkg." + name] = m

    spec = importlib.util.spec_from_file_location(
        "awpkg.scheduled_runner", "src/scheduled_runner.py")
    target = importlib.util.module_from_spec(spec)
    # REGISTER BEFORE EXEC so the module has a stable identity in sys.modules.
    sys.modules["awpkg.scheduled_runner"] = target
    spec.loader.exec_module(target)
    return target


target = _load_target()


class _FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results


def _patch_client(factory):
    """Swap target.SeatsAeroClient for `factory`; returns a restore callable."""
    orig = target.SeatsAeroClient
    target.SeatsAeroClient = factory
    return lambda: setattr(target, "SeatsAeroClient", orig)


def case_explicit_client_is_used():
    fake = _FakeClient([{"program": "sas"}])
    restore = _patch_client(lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not construct a client when one was passed")))
    try:
        got = target.search_schedule(
            {"origin": "LHR", "destination": "JFK"}, client=fake)
    finally:
        restore()
    assert got == [{"program": "sas"}], "explicit client result not returned"
    assert len(fake.calls) == 1, "explicit client.search was not called once"
    args = fake.calls[0][0]
    assert args[:2] == ("LHR", "JFK"), \
        "sched origin/destination not forwarded: {!r}".format(args)
    return True


def case_default_client_is_constructed_and_used():
    built = []
    fake = _FakeClient([{"a": 1}, {"b": 2}])

    def factory(*a, **k):
        built.append(1)
        return fake

    restore = _patch_client(factory)
    try:
        got = target.search_schedule({"origin": "LHR", "destination": "JFK"})
    finally:
        restore()
    assert built == [1], \
        "client=None must construct exactly one SeatsAeroClient, built={!r}".format(built)
    assert got == [{"a": 1}, {"b": 2}], \
        "default client's search result not returned as a list: {!r}".format(got)
    return True


def case_empty_sched_default_client():
    fake = _FakeClient([])
    restore = _patch_client(lambda *a, **k: fake)
    try:
        got = target.search_schedule({})
    finally:
        restore()
    assert got == [], "empty result must come back as []"
    args = fake.calls[0][0]
    assert args[:2] == (None, None), \
        "missing sched keys must forward as None, got {!r}".format(args)
    return True


def case_run_cycle_regression():
    # Pre-existing behaviour that must keep working: run_cycle drives a
    # search_fn per alert and keeps filtered results.
    alerts = {"a1": {"filters": {}}}
    out = target.run_cycle(alerts, lambda alert: [{"program": "sas"}])
    assert set(out) == {"a1"}, "run_cycle keys changed: {!r}".format(out)
    assert len(out["a1"]) == 1 and out["a1"][0]["program"] == "sas"
    return True


CASES = [
    ("explicit client is used, not a new one", case_explicit_client_is_used, True),
    ("client=None constructs exactly one SeatsAeroClient and uses it",
     case_default_client_is_constructed_and_used, True),
    ("empty sched with default client forwards None fields, returns []",
     case_empty_sched_default_client, True),
    ("regression: run_cycle still drives search_fn per alert",
     case_run_cycle_regression, True),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
