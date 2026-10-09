#!/usr/bin/env python3
"""Both-ways proof for the prisma-migration apply gate spliced into TS_BOOTSTRAP
by `ollama-dispatch-scaffold` (the owner 2026-09-17).

Root cause it guards: job 4ee80100599a (rt-saved-address-schema) shipped a
migration whose SQL was INVALID SQLite -- a `UNIQUE INDEX ... (...)` declared
INSIDE `CREATE TABLE(...)` (valid form is a SEPARATE `CREATE UNIQUE INDEX`).
`prisma validate` gates the SCHEMA, not the migration file, so the gate went
GREEN on a migration `sqlite3` rejects with a parse error.

The gate: whenever THIS dispatch creates/changes any prisma/migrations/**/
migration.sql, replay the whole chain into a throwaway sqlite db and FAIL on any
error; then (bonus) assert no drift vs schema.prisma via `prisma migrate diff`.

This test extracts the gate from TS_BOOTSTRAP and runs it in throwaway git repos:

  * BAD migration (the exact SavedAddress defect) -> sqlite apply FAILS, fails++
  * GOOD migration (separate CREATE UNIQUE INDEX)  -> applies cleanly
  * migration UNCHANGED by the dispatch            -> gate is a no-op (fails==0)
  * drift bonus (only asserted when prisma actually runs): a migration that
    applies but does NOT match schema.prisma -> migrate diff FAILS

Run: test-scaffold-migration-gate.py [-v]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
SCAFFOLD = BIN / "ollama-dispatch-scaffold"

# The exact defect from the affected job: UNIQUE INDEX inline in CREATE TABLE.
BAD_MIG = '''\
CREATE TABLE "SavedAddress" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "retailer" TEXT NOT NULL,
    "addressKey" TEXT NOT NULL,
    UNIQUE INDEX "SavedAddress_addressKey_key" ("addressKey")
);
'''
# The corrected form: separate CREATE UNIQUE INDEX after the table.
GOOD_MIG = '''\
CREATE TABLE "SavedAddress" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "retailer" TEXT NOT NULL,
    "addressKey" TEXT NOT NULL
);
CREATE UNIQUE INDEX "SavedAddress_addressKey_key" ON "SavedAddress"("addressKey");
'''
# schema.prisma whose canonical SQLite is exactly GOOD_MIG (no drift).
SCHEMA_MATCHES_GOOD = '''\
datasource db {
  provider = "sqlite"
  url      = "file:./dev.db"
}
model SavedAddress {
  id         Int     @id @default(autoincrement())
  retailer   String
  addressKey String  @unique
}
'''
# schema.prisma with an EXTRA column the migration does not create -> drift.
SCHEMA_DRIFTS = SCHEMA_MATCHES_GOOD.replace(
    "  addressKey String  @unique\n",
    "  addressKey String  @unique\n  city       String?\n")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def load_gate() -> str:
    """Exec the scaffold module (without running main) and return the migration
    gate fragment extracted from TS_BOOTSTRAP, wrapped so it is runnable."""
    src = SCAFFOLD.read_text()
    g = {"__name__": "not_main", "__file__": str(SCAFFOLD)}
    exec(compile(src, str(SCAFFOLD), "exec"), g)  # noqa: S102 -- our own tool
    tb = g["TS_BOOTSTRAP"]
    marker = "# --- prisma migration apply gate"
    if marker not in tb:
        raise SystemExit("migration gate marker not found in TS_BOOTSTRAP -- "
                         "the gate is missing from the scaffold")
    return "fails=0\n" + tb[tb.index(marker):] + '\necho "RESULT_FAILS:$fails"\n'


GATE = None  # set in main()


def make_repo(root: Path, schema: str, new_mig_sql: str | None,
              commit_the_mig: bool = False) -> Path:
    """A git repo with a committed baseline (prisma/ tracked) and, optionally, a
    NEW migration left UNTRACKED -- the exact shape of a real dispatch worktree.
    commit_the_mig=True instead commits it (so it is NOT a change this dispatch
    made) to prove the no-op path."""
    root.mkdir(parents=True)
    (root / "prisma").mkdir()
    (root / "prisma" / "schema.prisma").write_text(schema)
    # a pre-existing, committed baseline migration so prisma/migrations is tracked
    base = root / "prisma" / "migrations" / "00000000000000_init"
    base.mkdir(parents=True)
    (base / "migration.sql").write_text(
        'CREATE TABLE "Existing" ("id" INTEGER PRIMARY KEY);\n')
    if new_mig_sql is not None and commit_the_mig:
        d = root / "prisma" / "migrations" / "20260914213902_add_saved_address"
        d.mkdir(parents=True)
        (d / "migration.sql").write_text(new_mig_sql)
    run(["git", "init", "-q", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "t@t"], cwd=root)
    run(["git", "config", "user.name", "t"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-qm", "baseline"], cwd=root)
    if new_mig_sql is not None and not commit_the_mig:
        d = root / "prisma" / "migrations" / "20260914213902_add_saved_address"
        d.mkdir(parents=True)
        (d / "migration.sql").write_text(new_mig_sql)  # untracked = this dispatch
    return root


def run_gate(root: Path):
    r = run(["bash", "-c", GATE], cwd=root)
    out = r.stdout + r.stderr
    fails = None
    for line in out.splitlines():
        if line.startswith("RESULT_FAILS:"):
            fails = int(line.split(":", 1)[1])
    return out, fails


def case_bad_migration_fails(tmp):
    root = make_repo(tmp / "bad", SCHEMA_MATCHES_GOOD, BAD_MIG)
    out, fails = run_gate(root)
    bit = "FAIL: migration does not apply to sqlite" in out
    fired = "prisma migrations apply to throwaway sqlite" in out
    ok = fired and bit and (fails is not None and fails >= 1)
    return ok, f"fired={fired} sqlite_fail={bit} fails={fails}"


def case_good_migration_passes(tmp):
    root = make_repo(tmp / "good", SCHEMA_MATCHES_GOOD, GOOD_MIG)
    out, fails = run_gate(root)
    fired = "prisma migrations apply to throwaway sqlite" in out
    clean = "ok: all migrations apply cleanly to sqlite" in out
    no_sqlite_fail = "FAIL: migration does not apply" not in out
    ok = fired and clean and no_sqlite_fail
    return ok, f"fired={fired} sqlite_ok={clean}"


def case_unchanged_migration_noop(tmp):
    # the new migration is COMMITTED (not a change this dispatch made) -> no-op
    root = make_repo(tmp / "noop", SCHEMA_MATCHES_GOOD, GOOD_MIG,
                     commit_the_mig=True)
    out, fails = run_gate(root)
    fired = "prisma migrations apply to throwaway sqlite" in out
    ok = (not fired) and fails == 0
    return ok, f"fired={fired} fails={fails} (expected no-op)"


def case_drift_bites(tmp):
    # applies cleanly but schema.prisma has an extra column -> migrate diff drift.
    # Only ASSERTED when prisma actually ran (offline npx -> WARN, then skip).
    root = make_repo(tmp / "drift", SCHEMA_DRIFTS, GOOD_MIG)
    out, fails = run_gate(root)
    if "could not run" in out or "migrate diff (migrations vs schema" not in out:
        return True, "prisma unavailable -> drift assertion skipped (WARN path)"
    if "no drift" in out:
        # prisma ran and saw no drift -> our drift fixture failed to drift; that
        # is a test-fixture problem, not a gate bug, but flag it.
        return False, "prisma ran but reported no drift (fixture did not drift)"
    ok = "FAIL: migrations drift from schema.prisma" in out
    return ok, f"drift_detected={ok}"


CASES_LIST = [
    ("bad-migration-fails",     case_bad_migration_fails),
    ("good-migration-passes",   case_good_migration_passes),
    ("unchanged-migration-noop", case_unchanged_migration_noop),
    ("drift-bonus-bites",       case_drift_bites),
]


def main():
    global GATE
    if run(["sqlite3", "--version"]).returncode != 0:
        print("sqlite3 not on PATH -- cannot run the migration-gate tests")
        return 2
    GATE = load_gate()
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, (name, fn) in enumerate(CASES_LIST):
            ok, detail = fn(tmp / str(i))
            print(f"  {'ok  ' if ok else 'FAIL'} {name:<26} {detail}")
            if not ok:
                fails += 1
    print(f"\n--- {fails} of {len(CASES_LIST)} failed ---")
    if fails == 0:
        print("MIGRATION_GATE_OK: bad migration fails, good passes, unchanged "
              "is a no-op, and drift bites when prisma is available")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
