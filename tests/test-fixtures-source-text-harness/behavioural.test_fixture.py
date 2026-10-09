# NEGATIVE CONTROL (Python): imports the target and asserts outputs; the one
# read of the source is an ABSENCE constraint, which is allowed.
import pathlib
import sys

sys.path.insert(0, ".")
from app.sync import sync_message  # noqa: E402

CASES = [
    ("webError -> error", lambda: sync_message({"webError": "x"}), {"error": "BFMR tracker-id backfill failed: x"}),
    ("webNeeded 0 -> plain", lambda: sync_message({"synced": 3, "webNeeded": 0}), {"message": "Synced 3"}),
    ("webRows 0 -> failed", lambda: sync_message({"synced": 3, "webNeeded": 1, "webRows": 0}),
     {"error": "BFMR returned 0 tracker rows"}),
]


def main():
    fails = sum(1 for _d, thunk, want in CASES if thunk() != want)
    assert "import prisma" not in pathlib.Path("app/sync.py").read_text()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
