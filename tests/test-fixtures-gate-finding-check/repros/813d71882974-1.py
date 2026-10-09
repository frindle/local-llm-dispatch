# Claim (813d71882974, MEDIUM broker_guard/brokers.py:19): the `elif rid not in seen:`
# guard was deleted, making `raise ValueError(` unconditional -- i.e. a valid record
# can no longer load. Hand-written ground truth: a valid, complete record must load.
import json, os, tempfile
from broker_guard import brokers


def reproduce():
    p = os.path.join(tempfile.mkdtemp(), "brokers.json")
    with open(p, "w") as fh:
        json.dump({"brokers": [{"id": " AcMe ", "name": " Acme ", "url": "https://acme"}]}, fh)
    out = brokers.load_brokers(p)
    assert out == [{"id": "acme", "name": "Acme", "url": "https://acme"}], out
