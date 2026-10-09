# Claim (9414e0cf848e, HIGH src/scheduled_runner.py:75): `client or SeatsAeroClient()`
# raises NameError because SeatsAeroClient is not imported in the module.
# (src/__init__ drags in the whole CLI, so load the one file with its real
# seats_aero sibling and stub only the unrelated siblings -- as the repo's own
# fixture does.)
import importlib.util, sys, types


def _load():
    pkg = types.ModuleType("awpkg")
    pkg.__path__ = ["src"]
    sys.modules["awpkg"] = pkg
    for name, attrs in {"alert_filters": ["passes_filters"], "deeplinks": ["seats_aero_url"],
                        "pushover": ["send_award_notification"],
                        "scheduled_searches": ["effective_programs", "is_due", "load_schedules",
                                               "query_legs", "upsert_schedule"]}.items():
        m = types.ModuleType("awpkg." + name)
        for a in attrs:
            setattr(m, a, lambda *x, **k: None)
        sys.modules["awpkg." + name] = m
    spec = importlib.util.spec_from_file_location("awpkg.scheduled_runner",
                                                  "src/scheduled_runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["awpkg.scheduled_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def reproduce():
    m = _load()
    class Fake:
        def __init__(self, *a, **k):
            pass
        def search(self, *a, **k):
            return iter([])
    m.SeatsAeroClient = Fake if hasattr(m, "SeatsAeroClient") else None
    assert m.SeatsAeroClient is not None, "SeatsAeroClient is not bound in the module"
    assert m.search_schedule({"origin": "LHR", "destination": "JFK"}) == []
