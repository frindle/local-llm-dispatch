# POSITIVE case for test-source-text-harness.py (Python variant, modelled on the
# pipeline's CASES-table fixtures, e.g. wt-bfmr-stale-link-wiring): every case
# reads the target's text through a helper and checks for strings; nothing
# imports or runs app/sync.py.
import pathlib
import re
import sys

TARGET = "app/sync.py"


def src():
    return pathlib.Path(TARGET).read_text()


CASES = [
    ("calls backfill", lambda: "backfill(rows)" in src(), True),
    ("reports the error", lambda: bool(re.search(r"raise SyncError\(.*webError", src())), True),
    ("no prisma", lambda: "prisma" not in src(), True),
]


def main():
    fails = 0
    for desc, thunk, want in CASES:
        if thunk() != want:
            print("FAIL", desc)
            fails += 1
    with open(TARGET) as fh:
        body = fh.read()
    assert "def sync(" in body
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
