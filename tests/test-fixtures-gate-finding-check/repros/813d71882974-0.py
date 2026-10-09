# Claim (813d71882974, HIGH broker_guard/brokers.py:30): out["id"] is assumed to be
# a string and .lower() is called on it; the contract says invalid input -> ValueError.
import json, os, tempfile
from broker_guard import brokers


def reproduce():
    p = os.path.join(tempfile.mkdtemp(), "brokers.json")
    with open(p, "w") as fh:
        json.dump({"brokers": [{"id": 123, "name": "n", "url": "https://x"}]}, fh)
    try:
        brokers.load_brokers(p)
    except ValueError:
        return              # the documented rejection
    raise AssertionError("a non-string id was accepted instead of raising ValueError")
