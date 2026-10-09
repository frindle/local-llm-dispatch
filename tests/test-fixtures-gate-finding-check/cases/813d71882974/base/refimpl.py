#!/usr/bin/env python3
"""Reference impl for: bg-brokers-s1-load-s1-url-else-valueerror-norm

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Writes the complete solution into broker_guard/brokers.py.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/brokers.py'
p.parent.mkdir(parents=True, exist_ok=True)

SOLUTION = '''"""Load and normalize broker records from a brokers.json file."""
import json


def load_brokers(path):
    """Read the JSON document at *path* and return its normalized brokers.

    The top level must be an object with a 'brokers' list; every entry must
    carry id, name and url. String values are stripped of surrounding
    whitespace and each id is lowercased. Raises ValueError otherwise.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "brokers" not in data:
        raise ValueError("expected an object with a 'brokers' list")
    brokers = []
    for record in data["brokers"]:
        missing = [k for k in ("id", "name", "url") if k not in record]
        if missing:
            raise ValueError(
                "broker is missing required field(s): " + ", ".join(missing))
        out = {}
        for key, value in record.items():
            if isinstance(value, str):
                value = value.strip()
            out[key] = value
        out["id"] = out["id"].lower()
        brokers.append(out)
    return brokers
'''

p.write_text(SOLUTION)
print("refimpl applied")
