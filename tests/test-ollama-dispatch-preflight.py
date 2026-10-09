#!/usr/bin/env python3
"""Both-ways proof for ollama-dispatch-preflight -- does the GATE discriminate?

A gate nobody proved can fail is the same defect as a verify nobody proved can
fail, one level up. It would emit GO on everything and read as a clean process.
So this builds a real git repo per case and asserts:

  * the GOOD scaffold gets GO (exit 0) -- otherwise the gate cries wolf, the
    coordinator stops trusting it, and it stops being a gate at all
  * each BROKEN scaffold gets NO-GO, blocked BY THE CHECK THAT OWNS THAT DEFECT

The second half is what makes this more than a smoke test. A gate that says
NO-GO for the wrong reason sends the coordinator to fix the wrong thing, and
"it failed, so the check works" is exactly the reasoning that ships a vacuous
verify. Each case therefore names its expected blocker.

  test-ollama-dispatch-preflight.py            # all cases
  test-ollama-dispatch-preflight.py -k spec    # cases matching a substring
  test-ollama-dispatch-preflight.py -v         # show each gate's full output
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent / "ollama-dispatch-preflight"

# ---------------------------------------------------------------------------
# The base scaffold: a small, honest dispatch that SHOULD pass the gate.
# Every broken case is this, with exactly one thing wrong, so a failure names
# one cause instead of a soup of them.
# ---------------------------------------------------------------------------

TARGET_BUGGY = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""

THRESHOLD = 3


def is_safe(item, flag):
    # BUG: no elapsed-time gate at all, so a freshly-created item is "safe".
    return bool(flag) and (item.get("count") or 0) > 0
'''

TARGET_FIXED = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""

THRESHOLD = 3


def is_safe(item, flag):
    if not flag:
        return False
    return (item.get("count") or 0) >= THRESHOLD * 2
'''

# A fix that is behaviourally correct but does NOT use the literal the spec
# demands. Used to prove the gate notices a verify that goes green without
# enforcing the spec.
TARGET_FIXED_NO_LITERAL = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""

THRESHOLD = 3


def is_safe(item, flag):
    if not flag:
        return False
    return (item.get("count") or 0) >= 6
'''

FIXTURE = '''\
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ADVERSARIAL cases: a benign fixture (flag=True, count=99) passes the buggy
# code too, and would certify broken work.

CASES = [
    ({"count": 1}, True, False),    # the actual bug: over zero, under threshold
    ({"count": 5}, True, False),    # just under
    ({"count": 6}, True, True),     # exactly at
    ({"count": 99}, True, True),
    ({"count": 99}, False, False),  # flag gates everything
    ({}, True, False),              # missing key must not explode
    ({"count": None}, True, False), # None must not compare against int
]

fails = 0
for item, flag, want in CASES:
    try:
        got = m.is_safe(item, flag)
    except Exception as e:
        print("  FAIL is_safe(%r, %r) raised %s" % (item, flag, e))
        fails += 1
        continue
    if bool(got) != want:
        print("  FAIL is_safe(%r, %r) -> %r, want %r" % (item, flag, got, want))
        fails += 1
sys.exit(1 if fails else 0)
'''

VERIFY = '''\
#!/bin/bash
# Behavioural verify: drives the real function against adversarial fixtures.
set -u
cd "$(dirname "$0")"

fails=0

echo "=== target parses ==="
python3 -c "import ast; ast.parse(open('target.py').read())" \\
  && echo "  ok: target.py parses" \\
  || { echo "  FAIL: target.py does not parse"; fails=$((fails+1)); }

echo "=== behavioural test ==="
if python3 test_fixture.py; then
  echo "  ok: adversarial cases pass"
else
  echo "  FAIL: adversarial cases failed"; fails=$((fails+1))
fi

echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''

TASK = '''\
# TASK -- gate `is_safe` on the count threshold

## The defect (CONFIRMED)
`is_safe()` in `target.py:6` returns True for any item with a count above zero,
so an item with `count=1` is treated as safe to hard-delete. Confirmed by
calling `is_safe({"count": 1}, True)`, which returns True; it must return False.

## The fix -- ONLY edit `target.py`
Require the count to have reached twice the module threshold.

```python
def is_safe(item, flag):
    if not flag:
        return False
    return (item.get("count") or 0) >= THRESHOLD * 2
```

A missing key and an explicit `None` must both behave as zero, not raise.

## Must contain (literal tokens)
- `def is_safe(item, flag):`
- `THRESHOLD * 2`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints
`VERIFY_OK`. Only edit `target.py`; do not edit `verify.sh` or `test_fixture.py`.
'''


def run(cmd, cwd=None, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          shell=isinstance(cmd, str), **kw)


def build(root: Path, *, target=TARGET_BUGGY, task=TASK, verify=VERIFY,
          fixture=FIXTURE, fixed=TARGET_FIXED, dirty=False):
    """A git repo whose TRACKED baseline is the buggy target, with the dispatch
    scaffold alongside it as untracked files -- the real shape of a worktree.

    `fixed` is the reference-impl target the patch encodes (defaults to the
    canonical TARGET_FIXED). A case can pass its own to exercise a refimpl that
    carries benign lines the relevance gate must ignore."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "target.py").write_text(target)
    (root / "requirements.txt").write_text("")
    run(["git", "init", "-q", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "t@t"], cwd=root)
    run(["git", "config", "user.name", "t"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-qm", "baseline"], cwd=root)

    (root / "TASK.md").write_text(task)
    (root / "verify.sh").write_text(verify)
    (root / "test_fixture.py").write_text(fixture)

    # The reference impl, as a patch against the committed baseline.
    (root / "target.py").write_text(fixed)
    diff = run(["git", "diff"], cwd=root).stdout
    (root / "fix.patch").write_text(diff)
    run(["git", "checkout", "--", "."], cwd=root)

    if dirty:
        (root / "target.py").write_text(TARGET_FIXED)
    return root


def gate(root: Path, *extra, refimpl="fix.patch", env=None):
    cmd = [sys.executable, str(GATE), str(root), "--json"]
    if refimpl:
        cmd += ["--refimpl", str(root / refimpl)]
    else:
        cmd += ["--no-refimpl"]
    cmd += list(extra)
    # A case may point the gate at a FIXTURE queue state; without this the gate
    # would read the operator's live queue and the test would be nondeterministic
    # (and, worse, its verdict would depend on what happens to be dispatched).
    kw = {}
    if env:
        kw["env"] = {**os.environ, **env}
    p = run(cmd, **kw)
    try:
        data = json.loads(p.stdout)
    except Exception:
        data = {"verdict": "PARSE-ERROR", "blockers": [], "unproven": [],
                "_raw": p.stdout + p.stderr}
    return p.returncode, data


# ---------------------------------------------------------------------------
# Cases. Each returns (root, expected_verdict, expected_blocker_or_None, extra)
# ---------------------------------------------------------------------------

def case_good(tmp):
    """A complete scaffold: GO, exit 0, and NOTHING left unproven.

    --require is supplied because without it the edit-landed guard is off, and
    the gate is right to say so. The clean case must be clean on every axis --
    if a fully-specified dispatch still reports unproven properties, the
    coordinator learns to read UNPROVEN as noise, which is how the loud outcome
    stops being loud.
    """
    return (build(tmp / "good"), "GO", None,
            ("--require", "THRESHOLD * 2"))


def case_baseline_clean(tmp):
    """A reference impl left applied. The model would start from the answer."""
    return build(tmp / "dirty", dirty=True), "NO-GO", "baseline-clean", ()


def case_vacuous_verify(tmp):
    """A verify that passes on the untouched tree certifies doing nothing."""
    v = VERIFY.replace("if python3 test_fixture.py; then",
                       "if python3 -c 'pass'; then")
    return build(tmp / "vacuous", verify=v), "NO-GO", "baseline-fails", ()


def case_benign_verify(tmp):
    """Verify goes GREEN while a required literal is absent.

    The relevance gap: the verify discriminates change (it fails at baseline)
    but does not enforce what the spec actually asked for, so a model can pass
    it without doing the job. Caught by holding the reference impl against the
    task's own "Must contain" list.
    """
    root = build(tmp / "benign")
    # Reference impl is behaviourally right but uses `>= 6`, not `THRESHOLD * 2`.
    root_target = root / "target.py"
    root_target.write_text(TARGET_FIXED_NO_LITERAL)
    diff = run(["git", "diff"], cwd=root).stdout
    (root / "fix.patch").write_text(diff)
    run(["git", "checkout", "--", "."], cwd=root)
    return root, "NO-GO", "refimpl-satisfies", ()


def case_literal_pre_exists_elsewhere(tmp):
    """THE REAL-REPO CASE. A benign refimpl, but the required literal already
    exists in an UNTOUCHED file elsewhere in the tree.

    Every fixture above is a purpose-built repo where the literal appears
    nowhere but the target, so a whole-tree scan looked like a working check.
    In a real repo it is inert: a "Must contain" literal names a symbol you are
    integrating with, so it is already present and the check passes no matter
    what the refimpl did. Found on the first real worktree (machine-config:
    `eval_arms_pending` already lived in bin/handoff-emit.py), where a
    deliberately benign verify was rendered GO. An inert check is worse than no
    check -- it looks like a check that passed.
    """
    root = build(tmp / "preexist")
    # An unrelated, untouched, TRACKED file that happens to carry the literal.
    (root / "other_module.py").write_text(
        "# unrelated helper\nSCALE = THRESHOLD * 2 if False else 1\n")
    # ONLY this file -- `git add -A` here would track the scaffold and fix.patch
    # too, and the `git checkout -- .` below would then revert fix.patch to the
    # literal-bearing version and the case would silently test nothing.
    run(["git", "add", "other_module.py"], cwd=root)
    run(["git", "commit", "-qm", "unrelated file naming the same literal"],
        cwd=root)
    # Behaviourally-right refimpl that never writes the literal into the target.
    (root / "target.py").write_text(TARGET_FIXED_NO_LITERAL)
    diff = run(["git", "diff"], cwd=root).stdout
    (root / "fix.patch").write_text(diff)
    run(["git", "checkout", "--", "."], cwd=root)
    return root, "NO-GO", "refimpl-satisfies", ()


def case_literal_only_in_fixture(tmp):
    """Same inertness by a second route: the literal is in the verify FIXTURE.

    A behavioural harness names the very tokens the spec lists under "Must
    contain" -- that is what makes it behavioural. So any scope that counts
    pre-existing untracked scaffold files is inert for the same reason.
    """
    root = build(tmp / "infixture",
                 fixture=FIXTURE + "\n# references THRESHOLD * 2 by name\n")
    (root / "target.py").write_text(TARGET_FIXED_NO_LITERAL)
    diff = run(["git", "diff"], cwd=root).stdout
    (root / "fix.patch").write_text(diff)
    run(["git", "checkout", "--", "."], cwd=root)
    return root, "NO-GO", "refimpl-satisfies", ()


def case_task_unchanged_only_for_task_blockers(tmp):
    """A NO-GO for a NON-task reason must NOT make the next run cry "edit did
    not land".

    `task-unchanged` fired on ANY previous NO-GO. But most NO-GOs are not the
    task's fault -- a dirty tree, a vacuous verify, a missing import -- and the
    correct response to those leaves TASK.md untouched. The check then put a
    false "your edit did NOT land" at the top of the verdict, above the real
    reason, on every such iteration. That is how the one check that must never
    be ignored gets trained into noise. It now fires only when the previous
    NO-GO named a blocker a TASK.md edit could actually clear.

    This case runs the gate TWICE: first dirty (NO-GO baseline-clean), then
    clean with the task byte-identical. The harness's own run is the second.
    """
    root = build(tmp / "unchanged", dirty=True)
    rc, first = gate(root, "--require", "THRESHOLD * 2")
    assert first["verdict"] == "NO-GO", first
    assert [b["check"] for b in first["blockers"]] == ["baseline-clean"], first
    run(["git", "checkout", "--", "."], cwd=root)
    return root, "GO", None, ("--require", "THRESHOLD * 2")


def case_spec_gap_superstring(tmp):
    """The task names a LONGER var that CONTAINS the one the fixture reads.

    `spec-gap` tested `var in task_text`, which passes on any superstring: a
    task mentioning OLLAMA_QUEUE_LOG_DIR was read as naming LOG_DIR, so the
    model is never told about LOG_DIR and every iteration dies on a KeyError
    before reaching the logic. That is exactly the OLLAMA_QUEUE_LOGS-class miss
    this check exists to catch, and the check was passing it.

    Same shape as the whole-tree scan in refimpl-satisfies: a substring test
    looks discriminating on a fixture that contains no confusable superstring,
    and a purpose-built fixture never contains one by accident. Found by
    auditing the gate for that pattern after the first real-worktree run.
    """
    task = TASK.replace(
        "## Must contain",
        "The queue writes to `OLLAMA_QUEUE_LOG_DIR`, unrelated to this task.\n\n"
        "## Must contain")
    fixture = "import os\nLOG_DIR = os.environ['LOG_DIR']\n" + FIXTURE
    return (build(tmp / "superstring", task=task, fixture=fixture),
            "NO-GO", "spec-gap", ())


def case_cwd_collision(tmp):
    """Another ACTIVE job already owns this worktree.

    Two hazards. The obvious one: a second job starting in a tree that carries
    the first job's edits verifies against a baseline that is not a baseline,
    and on two hosts they race on the same files mid-write.

    The silent one: THIS GATE MUTATES THE TREE. It applies a reference impl and
    reverts it. `baseline-clean` covers a job that has already written --  a
    dirty tree short-circuits. But a job in its first iteration has not written
    yet, so the tree is clean, the gate proceeds, and for the length of the
    refimpl run that job is looking at a tree CONTAINING THE ANSWER. A green
    verify then proves nothing about the model. So this check runs before
    baseline-clean and hard-stops.

    Reported by the Coordinator after enqueuing into a running job's worktree
    and having nothing stop it.
    """
    root = build(tmp / "collide")
    qs = tmp / "queue-state-collide.json"
    qs.write_text(json.dumps({"jobs": [
        {"id": "aaaaaaaaaaaa", "status": "done", "cwd": str(root)},
        {"id": "bbbbbbbbbbbb", "status": "running", "cwd": str(root),
         "label": "already-owns-this-tree"},
    ]}))
    return (root, "NO-GO", "cwd-exclusive", (),
            {"OLLAMA_QUEUE_STATE": str(qs)})


def case_cwd_state_unreadable(tmp):
    """Queue state unreadable -> UNPROVEN, never PASS.

    The collision is unmeasured, not absent. An inert check that reports PASS
    because it could not look is the failure this tool exists to avoid.
    """
    root = build(tmp / "noqueue")
    qs = tmp / "queue-state-broken.json"
    qs.write_text("not json{")
    return (root, "GO", None, ("--require", "THRESHOLD * 2"),
            {"OLLAMA_QUEUE_STATE": str(qs)})


def case_spec_gap_defaulted_in_target(tmp):
    """THE FALSE NO-GO. Defaulted config reads in the TARGET, reached because
    verify.sh legitimately names the target for its parse check.

    Two conflations, both live: SCOPE (followed_py_files() reaches the target,
    so the target's own module-scope config counted as fixture dependencies --
    45 vars on a real config-heavy module) and SEMANTICS
    (`os.environ.get(X, default)` CANNOT FAIL, so it is not an obligation on
    the spec). Blocked every dispatch against a config-heavy module; the
    Coordinator enqueued over it after judging it benign, which is exactly the
    "false PROBLEM teaches people to route around the gate" failure.
    """
    tgt = TARGET_BUGGY.replace(
        "THRESHOLD = 3",
        'import os\n'
        'ACTIVITY_LOG = os.environ.get("ACTIVITY_LOG", "/tmp/a.log")\n'
        'DELUGE_URL = os.getenv("DELUGE_URL", "http://x")\n'
        "THRESHOLD = 3")
    fix = TARGET_FIXED.replace(
        "THRESHOLD = 3",
        'import os\n'
        'ACTIVITY_LOG = os.environ.get("ACTIVITY_LOG", "/tmp/a.log")\n'
        'DELUGE_URL = os.getenv("DELUGE_URL", "http://x")\n'
        "THRESHOLD = 3")
    root = build(tmp / "sg-defaulted", target=tgt)
    (root / "target.py").write_text(fix)
    (root / "fix.patch").write_text(run(["git", "diff"], cwd=root).stdout)
    run(["git", "checkout", "--", "."], cwd=root)
    return (root, "GO", None, ("--require", "THRESHOLD * 2"))


def case_spec_gap_required_still_blocks(tmp):
    """ADVERSARIAL COMPANION -- the fix must not go vacuous. A fixture that
    REQUIRES a var (no default, so it KeyErrors) must still block."""
    fx = 'import os\nNEEDED = os.environ["MUST_BE_NAMED"]\n' + FIXTURE
    return build(tmp / "sg-required", fixture=fx), "NO-GO", "spec-gap", ()


def case_spec_gap_set_still_blocks(tmp):
    """The original OLLAMA_QUEUE_LOGS miss: a var the FIXTURE SETS and the task
    never names makes the model hardcode a path. Must still block."""
    fx = 'import os\nos.environ["QUEUE_LOG_DIR"] = "/tmp/q"\n' + FIXTURE
    return build(tmp / "sg-set", fixture=fx), "NO-GO", "spec-gap", ()


def case_draft_unconfirmed_blocks(tmp):
    """Model-drafted cases must not gate a dispatch until a human confirms.

    The gate proves a verify DISCRIMINATES; it cannot prove it is RELEVANT --
    a case that fails at baseline and asserts every literal can still test the
    wrong property. So "a human looked" is a real property and gets a real
    check rather than being trusted. ollama-dispatch-draft writes the marker;
    only its --confirm removes it.
    """
    root = build(tmp / "draftmark")
    fx = root / "test_fixture.py"
    fx.write_text("DRAFT_UNCONFIRMED = True\n" + fx.read_text())
    return (root, "NO-GO", "draft-confirmed", ("--require", "THRESHOLD * 2"))


def case_unsatisfiable(tmp):
    """The verify demands something the task never asks for -- the model would
    grind through every iteration and never converge."""
    v = VERIFY.replace(
        'if python3 test_fixture.py; then',
        'if python3 -c "import sys; sys.exit(0 if \'GATE_TOKEN\' in '
        'open(\'target.py\').read() else 1)"; then')
    return build(tmp / "unsat", verify=v), "NO-GO", "refimpl-passes", ()


def case_spec_gap(tmp):
    """A fixture reads an env var the task never names."""
    f = FIXTURE.replace(
        'import sys\nimport importlib.util',
        'import os\nimport sys\nimport importlib.util\n'
        'DATA_DIR = os.environ["FIXTURE_DATA_DIR"]')
    v = VERIFY.replace('fails=0', 'export FIXTURE_DATA_DIR=/tmp\nfails=0')
    return build(tmp / "specgap", fixture=f, verify=v), "NO-GO", "spec-gap", ()


def case_heredoc(tmp):
    """Unquoted heredoc delimiter: the shell eats `$` before python sees it."""
    v = VERIFY.replace('echo "=== behavioural test ==="', '''\
echo "=== behavioural test ==="
python3 - <<PY
import re
cost = "$100"
print(re.sub(r"\\\\$", "", cost))
PY''')
    return build(tmp / "heredoc", verify=v), "NO-GO", "heredoc-escaping", ()


def case_env_parity(tmp):
    """The verify dies on ImportError -- fails every iteration for a reason
    that has nothing to do with the task."""
    t = 'import nonexistent_dep_xyz  # noqa\n' + TARGET_BUGGY
    return build(tmp / "envparity", target=t), "NO-GO", "env-parity", ()


def case_target_parses(tmp):
    t = TARGET_BUGGY + "\ndef broken(:\n    pass\n"
    return build(tmp / "parse", target=t), "NO-GO", "target-parses", ()


def case_edit_did_not_land(tmp):
    """--require is the edit-landed guard: the literal is not in the task."""
    return (build(tmp / "editland"), "NO-GO", "required-literals",
            ("--require", "elapsed_seconds >= MIN_SEED"))


def case_proxy_fixture_blocks(tmp):
    """A fixture that inspects the QUERY the code builds, instead of driving the
    code, is a NO-GO.

    The scar: rt-pl-exclude (2026-09-17). A fixture pattern-matching a Prisma
    `where` shape PASSED the mutation gate -- a shape assertion notices every
    change to the filter, so it mutates perfectly -- while the P&L-breaking bug
    underneath it (`every` over an empty relation is vacuously true, so orders
    with no gift cards were dropped) survived. A human caught it, not the
    harness. This fixture never asserts on anything the target RETURNED.
    """
    f = '''\
import sys
CAPTURED = {"where": {"count": {"gt": 0}}, "select": {"count": True}}

def main():
    calls = [CAPTURED]
    for c in calls:
        assert c.where == {"count": {"gt": 0}}, "filter shape"
        assert c.select == {"count": True}
    print("  ok: query shape matches")
    return 0

sys.exit(main())
'''
    return (build(tmp / "proxyfix", fixture=f), "NO-GO", "harness-faithful", ())


def case_faithful_fixture_passes(tmp):
    """The control: the canonical scaffold fixture DRIVES the real symbol, so
    the same check must leave it alone. Without this half the check could be
    blocking every dispatch and the suite would still look green."""
    return build(tmp / "faithfulfix"), "GO", None, ()


def case_ctx_under_provisioned(tmp):
    """An under-provisioned --num-ctx is a NO-GO, not a warning.

    The scar: draft-wt-bfmr-relink-v3 was enqueued at a hand-passed 32768 whose
    prompt peaked at 31,946 tokens (97.5% of the window) and came back
    converged:false after 6 iterations. Nothing errored -- an agentic job that
    runs out of window read/analyze loops and reports non-convergence, which
    reads exactly like model incapacity. The task text here is padded so its
    computed requirement provably exceeds the tiny window we then pass.
    """
    big = TASK + "\n\n## Context\n" + ("x" * 200 + "\n") * 400   # ~80KB of prompt
    return (build(tmp / "ctxsmall", task=big), "NO-GO", "ctx-budget",
            ("--num-ctx", "8192"))


def case_ctx_adequate_is_go(tmp):
    """The control: the SAME oversized task with a window that covers it is GO.

    Without this, case_ctx_under_provisioned would also pass a check that
    blocked every dispatch -- the gate has to discriminate, not just refuse.
    """
    big = TASK + "\n\n## Context\n" + ("x" * 200 + "\n") * 400
    return (build(tmp / "ctxbig", task=big), "GO", None,
            ("--num-ctx", "131072"))


def case_waiver_is_unproven(tmp):
    """A waived both-ways proof must still be GO -- but LOUDLY unproven, never
    silently absent. This is the inert-check failure mode."""
    return build(tmp / "waived"), "GO", None, ()


# ---------------------------------------------------------------------------
# --review: POST-RUN scope audit. These gate the tree FIRST (recording the
# scope contract), then mutate it the way a model would, then review.
# ---------------------------------------------------------------------------

def review(root: Path):
    p = run([sys.executable, str(GATE), str(root), "--review", "--json"])
    try:
        data = json.loads(p.stdout)
    except Exception:
        data = {"verdict": "PARSE-ERROR", "blockers": [],
                "_raw": p.stdout + p.stderr}
    return p.returncode, data


def _gated(tmp, name):
    """A repo that has been gated, so .preflight-state.json holds the contract."""
    root = build(tmp / name)
    (root / "unrelated_test.py").write_text("def test_x(): assert True\n")
    run(["git", "add", "unrelated_test.py"], cwd=root)
    run(["git", "commit", "-qm", "an unrelated tracked test"], cwd=root)
    gate(root, "--require", "THRESHOLD * 2")
    return root


def test_review_out_of_scope_deletion(tmp):
    """The Sonarr reverse-pack failure: model DELETES an unrelated tracked file
    and the green verify hides it, because the deleted file is not part of this
    fix's verify. A less careful merge -- or an auto-approve -- ships a silent
    deletion of a tracked test."""
    root = _gated(tmp, "rev-del")
    (root / "target.py").write_text(TARGET_FIXED)      # the intended fix
    (root / "unrelated_test.py").unlink()              # the collateral damage
    rc, data = review(root)
    ids = [b["check"] for b in data.get("blockers", [])]
    ok = rc == 1 and "scope-respected" in ids
    return ok, f"rc={rc} blockers={ids}"


def test_review_scaffold_tampered(tmp):
    """Model edits its own verify -- a green verify then proves nothing."""
    root = _gated(tmp, "rev-tamper")
    (root / "target.py").write_text(TARGET_FIXED)
    with open(root / "verify.sh", "a") as f:
        f.write("\necho VERIFY_OK\nexit 0\n")
    rc, data = review(root)
    ids = [b["check"] for b in data.get("blockers", [])]
    ok = rc == 1 and "scaffold-untouched" in ids
    return ok, f"rc={rc} blockers={ids}"


def test_review_in_scope_passes(tmp):
    """A fix confined to the declared target must NOT be flagged."""
    root = _gated(tmp, "rev-clean")
    (root / "target.py").write_text(TARGET_FIXED)
    rc, data = review(root)
    ok = rc == 0 and data.get("verdict") == "GO" and not data.get("blockers")
    return ok, f"rc={rc} verdict={data.get('verdict')}"


def test_review_ungated_is_unproven(tmp):
    """Never gated -> UNPROVEN, never a silent PASS. The scope is unmeasured,
    not respected."""
    root = build(tmp / "rev-ungated")
    (root / "target.py").write_text(TARGET_FIXED)
    rc, data = review(root)
    un = [u["check"] for u in data.get("unproven", [])]
    ok = "scope-respected" in un and "scaffold-untouched" in un
    return ok, f"unproven={un}"


def test_review_is_read_only(tmp):
    """--review must never mutate the tree or rewrite the contract it judges."""
    root = _gated(tmp, "rev-ro")
    (root / "target.py").write_text(TARGET_FIXED)
    before = (run(["git", "status", "--porcelain"], cwd=root).stdout,
              (root / ".preflight-state.json").read_text())
    review(root)
    after = (run(["git", "status", "--porcelain"], cwd=root).stdout,
             (root / ".preflight-state.json").read_text())
    return before == after, "tree/contract mutated" if before != after else "ok"


def case_task_unchanged_not_self_sustaining(tmp):
    """`task-unchanged` must not perpetuate ITSELF.

    It used to list its own id in TASK_FIXABLE_BLOCKERS, so once it fired the
    prior blocker was "task-unchanged", which made it fire again, and the only
    escape was editing TASK.md even when TASK.md was never the problem. Found
    when a NO-GO cleared by re-freezing check_literals.py (not by touching the
    task) kept reporting "your edit did not land".

    Run 1: NO-GO on a bogus --require. Run 2: same, so task-unchanged fires.
    Run 3: correct --require, TASK.md still untouched -> must GO.
    """
    root = build(tmp / "unchanged-loop")
    # 1: a real, task-fixable NO-GO.
    gate(root, "--require", "NOT_IN_THE_TASK")
    # 2: fixed OUTSIDE TASK.md (the --require was simply wrong), so the task is
    #    correctly unchanged -- task-unchanged fires, which is the behaviour
    #    being narrowed, not removed.
    rc2, second = gate(root, "--require", "THRESHOLD * 2")
    fired = "task-unchanged" in [b["check"] for b in second["blockers"]]
    # 3: nothing left to fix. If task-unchanged counted ITSELF as a task-fixable
    #    prior blocker, this would NO-GO forever.
    rc3, third = gate(root, "--require", "THRESHOLD * 2")
    ids = [b["check"] for b in third.get("blockers", [])]
    ok = fired and rc3 == 0 and third.get("verdict") == "GO"
    return ok, f"fired_on_run2={fired} run3_rc={rc3} run3_blockers={ids}"


def case_draft_unreadable_is_unproven(tmp):
    """An UNREADABLE fixture must not silence the human-review gate.

    check_draft_confirmed swallowed the read error, so `drafted` stayed empty
    and the check returned adding NO result at all -- no PASS, no FAIL, no
    UNPROVEN. An unconfirmed model-drafted fixture became invisible to the gate
    and reached GO. Third failure of the same invariant ("a human read the
    cases"), after mark_drafted's no-op re.sub silently marking nothing.

    An absence of evidence is not evidence of absence.
    """
    root = build(tmp / "draftunread")
    fx = root / "test_fixture.py"
    fx.write_text("DRAFT_UNCONFIRMED = True\n" + fx.read_text())
    fx.chmod(0o000)
    rc, data = gate(root, "--require", "THRESHOLD * 2")
    un = [u["check"] for u in data.get("unproven", [])]
    fx.chmod(0o644)
    ok = "draft-confirmed" in un
    return ok, f"unproven={un}"


REVIEW_TESTS = [
    ("task-unchanged-not-self-sustaining", case_task_unchanged_not_self_sustaining),
    ("draft-unreadable-is-unproven", case_draft_unreadable_is_unproven),
    ("review-out-of-scope-deletion", test_review_out_of_scope_deletion),
    ("review-scaffold-tampered",     test_review_scaffold_tampered),
    ("review-in-scope-passes",       test_review_in_scope_passes),
    ("review-ungated-is-unproven",   test_review_ungated_is_unproven),
    ("review-is-read-only",          test_review_is_read_only),
]

# ---------------------------------------------------------------------------
# verify-relevance: the gate now measures whether the verify tests the
# PROPERTY or a proxy. Three facts, each asserted on the gate's own output:
#   * a relevant verify PASSes the check and the ledger records it
#   * a benign-but-discriminating verify is a hard NO-GO (FAIL), recorded "low",
#     with survivors NAMED so the author knows which case to add. (This was
#     GO+WARN when the gate landed in shadow mode -- 40eb8d9; 47f6cc1 hardened
#     LOW relevance to a blocking NO-GO because signoff.py auto-approves off this
#     evidence and a broken fix must not survive its own verify. This test was
#     the one artifact that still encoded the old shadow-mode contract.)
#   * the waiver is loud (UNPROVEN), never silent
# Every case here points OLLAMA_PREFLIGHT_LEDGER at a temp dir: a test that
# writes the operator's real ledger is not a test.
# ---------------------------------------------------------------------------

# Discriminates (fails at baseline: count=1 -> True there) but tests almost
# nothing about the boundary or the flag. This is the shape the memory
# "verify relevance, not just discrimination" is about, and it passes every
# OTHER check in this gate.
FIXTURE_BENIGN = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = 0
if m.is_safe({"count": 99}, True) is not True: fails += 1
if m.is_safe({"count": 1}, True) is not False: fails += 1
sys.exit(1 if fails else 0)
'''


def _ledger_entry(ledger: Path, root: Path):
    import hashlib
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    f = ledger / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else None


def test_relevance_relevant_and_ledger(tmp):
    root = build(tmp / "rel-good")
    ledger = tmp / "ledger-good"
    rc, data = gate(root, "--require", "THRESHOLD * 2",
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    checks = {c["check"]: c["status"] for c in data.get("checks", [])}
    vr = data.get("verify_relevance") or {}
    entry = _ledger_entry(ledger, root) or {}
    ok = (data.get("verdict") == "GO" and checks.get("verify-relevance") == "PASS"
          and vr.get("verdict") == "relevant"
          and (entry.get("verify_relevance") or {}).get("verdict") == "relevant"
          and entry.get("scaffold_sha256", {}).get("verify.sh")
          and entry.get("verify_failed_at_baseline") is True)
    return ok, (f"verdict={data.get('verdict')} check={checks.get('verify-relevance')} "
                f"score={vr.get('score')} ledger={'yes' if entry else 'MISSING'}")


def test_relevance_benign_blocks(tmp):
    root = build(tmp / "rel-benign", fixture=FIXTURE_BENIGN)
    ledger = tmp / "ledger-benign"
    rc, data = gate(root, "--require", "THRESHOLD * 2",
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    checks = {c["check"]: c["status"] for c in data.get("checks", [])}
    vr = data.get("verify_relevance") or {}
    entry = _ledger_entry(ledger, root) or {}
    # Hard NO-GO (FAIL): a LOW-relevance verify BLOCKS the dispatch -- a broken
    # fix must not survive its own verify, and signoff.py auto-approves off this
    # evidence. Recorded low, with survivors NAMED so the author knows which
    # case to add. (Shadow-mode 40eb8d9 rendered this GO+WARN; 47f6cc1 made it
    # blocking. See the section comment above.)
    ok = (data.get("verdict") == "NO-GO" and rc == 1
          and checks.get("verify-relevance") == "FAIL"
          and vr.get("verdict") == "low"
          and len(vr.get("survivors") or []) > 0
          and (entry.get("verify_relevance") or {}).get("verdict") == "low")
    # ...and the tree is back at baseline afterwards.
    st = run(["git", "status", "--porcelain"], cwd=root).stdout
    clean = not any(l[:2] != "??" for l in st.splitlines() if l.strip())
    return ok and clean, (f"verdict={data.get('verdict')} check="
                          f"{checks.get('verify-relevance')} score={vr.get('score')} "
                          f"survivors={len(vr.get('survivors') or [])} clean={clean}")


def test_relevance_waiver_is_unproven(tmp):
    root = build(tmp / "rel-waived")
    ledger = tmp / "ledger-waived"
    rc, data = gate(root, "--require", "THRESHOLD * 2", "--no-relevance",
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    unproven = [u["check"] for u in data.get("unproven", [])]
    entry = _ledger_entry(ledger, root) or {}
    ok = ("verify-relevance" in unproven
          and (entry.get("verify_relevance") is None)
          and "verify-relevance" in (entry.get("unproven") or []))
    return ok, f"unproven={unproven} ledger_unproven={entry.get('unproven')}"


def test_relevance_no_refimpl_is_unproven(tmp):
    root = build(tmp / "rel-norefimpl")
    ledger = tmp / "ledger-norefimpl"
    rc, data = gate(root, "--require", "THRESHOLD * 2", refimpl=None,
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    unproven = [u["check"] for u in data.get("unproven", [])]
    return "verify-relevance" in unproven, f"unproven={unproven}"


REVIEW_TESTS += [
    ("relevance-relevant-ledger",  test_relevance_relevant_and_ledger),
    ("relevance-benign-blocks",    test_relevance_benign_blocks),
    ("relevance-waiver-unproven",  test_relevance_waiver_is_unproven),
    ("relevance-norefimpl-unproven", test_relevance_no_refimpl_is_unproven),
]

# ---------------------------------------------------------------------------
# BENIGN-MUTATION ALLOWLIST (pain point #2) + its REVERT-TEST.
#
# The allowlist lets a refimpl carry lines that are UNOBSERVABLE by construction
# -- resource cleanup (`.close()`), a prose message kwarg, or a line the author
# marked `# relevance: unobservable` -- without their equivalent-mutant
# survivors dragging the verify to a spurious LOW/NO-GO. The danger is that such
# a loosening also stops the gate biting a REAL hole. Two tests, and the second
# is the one that matters:
#   1. allowlist-benign-passes: a refimpl whose ONLY survivors would be benign
#      lines reaches GO/relevant, and no survivor names a benign line.
#   2. allowlist-still-bites (REVERT-TEST): the SAME refimpl, but with a genuine
#      behavioural assertion removed from the fixture, is still a hard NO-GO on
#      verify-relevance -- proving the allowlist narrowed the evidence set to
#      the benign lines only, never to the behavioural ones.
# ---------------------------------------------------------------------------

# A refimpl that fixes the real bug AND adds three benign lines: a dead-store
# marked unobservable, a resource-cleanup close(), and a prose-message kwarg.
# Every one would otherwise mint equivalent mutants that survive any fixture.
# A shared stub (present at baseline AND in the fix) keeps the diff to the
# is_safe body only. `_slack` is a dead store marked unobservable; `.close()`
# is a bare cleanup call the allowlist skips by name -- both would otherwise
# mint equivalent mutants that survive any fixture.
_BENIGN_HEAD = '''\
"""Toy target: gate is_safe, with benign lines the relevance gate must ignore."""

THRESHOLD = 3


class _Null:
    def close(self):
        pass
'''

TARGET_FIXED_BENIGN = _BENIGN_HEAD + '''

def is_safe(item, flag, resource=_Null()):
    if not flag:
        return False
    _slack = THRESHOLD * 999  # relevance: unobservable
    result = (item.get("count") or 0) >= THRESHOLD * 2
    resource.close()
    return result
'''

TARGET_BUGGY_BENIGN = _BENIGN_HEAD + '''

def is_safe(item, flag, resource=_Null()):
    # BUG: no threshold gate at all.
    return bool(flag) and (item.get("count") or 0) > 0
'''

TASK_BENIGN = '''\
# TASK -- gate `is_safe` on the count threshold

## The defect (CONFIRMED)
`is_safe()` returns True for any item with count above zero. Confirmed by
calling `is_safe({"count": 1}, True)`, which returns True; it must return False.

## The fix -- ONLY edit `target.py`
Require the count to have reached twice the module threshold.

## Must contain (literal tokens)
- `def is_safe(item, flag, resource=_Null()):`
- `THRESHOLD * 2`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints
`VERIFY_OK`. Only edit `target.py`; do not edit `verify.sh` or `test_fixture.py`.
'''

# Thorough fixture: kills every behavioural mutant on the real gate line.
FIXTURE_BENIGN_STRONG = '''\
import sys
import importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

CASES = [
    ({"count": 1}, True, False),
    ({"count": 5}, True, False),
    ({"count": 6}, True, True),
    ({"count": 99}, True, True),
    ({"count": 99}, False, False),
    ({}, True, False),
    ({"count": None}, True, False),
]
fails = 0
for item, flag, want in CASES:
    try:
        got = m.is_safe(item, flag)
    except Exception as e:
        print("  FAIL is_safe(%r, %r) raised %s" % (item, flag, e)); fails += 1; continue
    if bool(got) != want:
        print("  FAIL is_safe(%r, %r) -> %r, want %r" % (item, flag, got, want)); fails += 1
sys.exit(1 if fails else 0)
'''

# The SAME as above but with the count-boundary cases removed -- it still passes
# the fixed code (flag gate + a high count) yet no longer pins the threshold, so
# the behavioural mutants on the real gate line survive.
FIXTURE_BENIGN_WEAK = '''\
import sys
import importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# The revert: the flag-gate case and the mid-boundary cases are DELETED, so the
# real gate line is barely exercised. Still fails at baseline (count=1 is True
# on the buggy tree) and passes on the fix -- but the behavioural mutants on the
# gate line now survive.
CASES = [
    ({"count": 99}, True, True),
    ({"count": 1}, True, False),
]
fails = 0
for item, flag, want in CASES:
    got = m.is_safe(item, flag)
    if bool(got) != want:
        print("  FAIL is_safe(%r, %r) -> %r, want %r" % (item, flag, got, want)); fails += 1
sys.exit(1 if fails else 0)
'''


def _benign_root(root, fixture):
    return build(root, target=TARGET_BUGGY_BENIGN, task=TASK_BENIGN,
                 fixture=fixture, fixed=TARGET_FIXED_BENIGN)


def test_allowlist_benign_passes(tmp):
    root = _benign_root(tmp / "allow-benign", FIXTURE_BENIGN_STRONG)
    ledger = tmp / "ledger-allow-benign"
    rc, data = gate(root, "--require", "THRESHOLD * 2",
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    checks = {c["check"]: c["status"] for c in data.get("checks", [])}
    vr = data.get("verify_relevance") or {}
    survivors = vr.get("survivors") or []
    # No survivor may sit on a benign line -- if the allowlist did nothing, the
    # dead store / close() / prose kwarg would show up here.
    benign_hit = [s for s in survivors
                  if any(tok in (s.get("snippet") or "")
                         for tok in ("_slack", "resource.close", ".close()"))]
    ok = (data.get("verdict") == "GO"
          and checks.get("verify-relevance") == "PASS"
          and vr.get("verdict") == "relevant"
          and not benign_hit)
    return ok, (f"verdict={data.get('verdict')} check={checks.get('verify-relevance')} "
                f"score={vr.get('score')} survivors={len(survivors)} "
                f"benign_survivors={len(benign_hit)}")


def test_allowlist_still_bites(tmp):
    """REVERT-TEST: remove a genuine behavioural assertion -> still NO-GO.

    This is the proof the allowlist did not neuter the gate. The refimpl is
    identical to the passing case; only the fixture lost its boundary cases."""
    root = _benign_root(tmp / "allow-bites", FIXTURE_BENIGN_WEAK)
    ledger = tmp / "ledger-allow-bites"
    rc, data = gate(root, "--require", "THRESHOLD * 2",
                    env={"OLLAMA_PREFLIGHT_LEDGER": str(ledger)})
    checks = {c["check"]: c["status"] for c in data.get("checks", [])}
    vr = data.get("verify_relevance") or {}
    # The gate must STILL bite: NO-GO, verify-relevance FAIL, verdict low, and a
    # survivor on the REAL gate line (not a benign one).
    survivors = vr.get("survivors") or []
    real_hit = [s for s in survivors
                if "THRESHOLD * 2" in (s.get("snippet") or "")
                or ">=" in (s.get("snippet") or "")
                or "not flag" in (s.get("snippet") or "")]
    ok = (data.get("verdict") == "NO-GO" and rc == 1
          and checks.get("verify-relevance") == "FAIL"
          and vr.get("verdict") == "low"
          and len(real_hit) > 0)
    return ok, (f"verdict={data.get('verdict')} check={checks.get('verify-relevance')} "
                f"score={vr.get('score')} survivors={len(survivors)} "
                f"real_line_survivors={len(real_hit)}")


REVIEW_TESTS += [
    ("allowlist-benign-passes",  test_allowlist_benign_passes),
    ("allowlist-still-bites",    test_allowlist_still_bites),
]


# ---------------------------------------------------------------------------
# FIX 2b both-ways proof: an ABSENT target file must NO-GO on target-parses when
# the TASK is an EDIT (the "repo-edit scaffolded as --new-project" defect), and
# must still PASS when the TASK explicitly declares a creation task. Built by
# hand (no target.py committed) because build() always writes the target.
# ---------------------------------------------------------------------------

def _harness_only_repo(root: Path, task_text):
    """A git repo carrying the dispatch harness but NO target.py -- the exact
    shape of a repo-edit worktree that is missing its real target file."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "requirements.txt").write_text("")
    (root / "TASK.md").write_text(task_text)
    (root / "verify.sh").write_text(VERIFY)
    (root / "test_fixture.py").write_text(FIXTURE)  # loads target.py by path
    run(["git", "init", "-q", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "t@t"], cwd=root)
    run(["git", "config", "user.name", "t"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-qm", "harness only, no target"], cwd=root)
    return root


# A creation TASK: same shape, but it explicitly says the file is created.
TASK_CREATION = TASK.replace(
    "## The defect (CONFIRMED)",
    "## The defect (CONFIRMED)\n`target.py` does not exist yet -- create it "
    "from scratch as a new file.\n")


def test_absent_target_edit_blocks(tmp):
    """Fix 2b: fixture references target.py, the worktree does NOT contain it,
    and the TASK is an ordinary edit -> NO-GO owned by target-parses. Without
    the fix the creation-task inference PASSed it and the coding job recreated
    the real file from scratch."""
    root = _harness_only_repo(tmp / "absent-edit", TASK)
    rc, data = gate(root, "--require", "THRESHOLD * 2", refimpl=None)
    ids = [b["check"] for b in data.get("blockers", [])]
    ok = rc == 1 and "target-parses" in ids
    return ok, f"rc={rc} blockers={ids}"


def test_absent_target_creation_ok(tmp):
    """Fix 2b did NOT over-tighten: an absent target whose TASK explicitly
    declares a from-scratch creation still PASSes target-parses."""
    root = _harness_only_repo(tmp / "absent-create", TASK_CREATION)
    rc, data = gate(root, "--require", "THRESHOLD * 2", refimpl=None)
    checks = {c["check"]: c["status"] for c in data.get("checks", [])}
    blockers = [b["check"] for b in data.get("blockers", [])]
    ok = checks.get("target-parses") == "PASS" and "target-parses" not in blockers
    return ok, f"target-parses={checks.get('target-parses')} blockers={blockers}"


REVIEW_TESTS += [
    ("absent-target-edit-blocks",  test_absent_target_edit_blocks),
    ("absent-target-creation-ok",  test_absent_target_creation_ok),
]

CASES = [
    ("good-scaffold",       case_good),
    ("dirty-baseline",      case_baseline_clean),
    ("cwd-collision",       case_cwd_collision),
    ("cwd-state-unreadable", case_cwd_state_unreadable),
    ("vacuous-verify",      case_vacuous_verify),
    ("benign-verify",       case_benign_verify),
    ("literal-pre-exists",  case_literal_pre_exists_elsewhere),
    ("literal-in-fixture",  case_literal_only_in_fixture),
    ("task-unchanged-scope", case_task_unchanged_only_for_task_blockers),
    ("unsatisfiable-task",  case_unsatisfiable),
    ("spec-gap-env-var",    case_spec_gap),
    ("spec-gap-superstring", case_spec_gap_superstring),
    ("spec-gap-defaulted-target", case_spec_gap_defaulted_in_target),
    ("spec-gap-required-blocks", case_spec_gap_required_still_blocks),
    ("spec-gap-set-blocks",      case_spec_gap_set_still_blocks),
    ("heredoc-escaping",    case_heredoc),
    ("env-parity",          case_env_parity),
    ("target-syntax-error", case_target_parses),
    ("edit-did-not-land",   case_edit_did_not_land),
    ("draft-unconfirmed",   case_draft_unconfirmed_blocks),
    ("waiver-is-unproven",  case_waiver_is_unproven),
    ("proxy-fixture-blocks",  case_proxy_fixture_blocks),
    ("faithful-fixture-passes", case_faithful_fixture_passes),
    ("ctx-under-provisioned", case_ctx_under_provisioned),
    ("ctx-adequate-is-go",    case_ctx_adequate_is_go),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only cases matching this substring")
    ap.add_argument("-v", action="store_true", help="print each gate's output")
    ap.add_argument("--keep", action="store_true", help="keep the temp repos")
    a = ap.parse_args()

    if not GATE.exists():
        sys.exit(f"ERROR: {GATE} not found")

    tmp = Path(tempfile.mkdtemp(prefix="preflight-test-"))
    failures, ran = [], 0
    try:
        for name, fn in CASES:
            if a.k and a.k not in name:
                continue
            ran += 1
            spec = fn(tmp)
            root, want_verdict, want_blocker, extra = spec[:4]
            case_env = spec[4] if len(spec) > 4 else None
            waived = name == "waiver-is-unproven"
            rc, data = gate(root, *extra,
                            refimpl=None if waived else "fix.patch",
                            env=case_env)
            got = data.get("verdict")
            blockers = [b["check"] for b in data.get("blockers", [])]
            unproven = [u["check"] for u in data.get("unproven", [])]

            problems = []
            if got != want_verdict:
                problems.append(f"verdict {got!r}, want {want_verdict!r}")
            if want_blocker and want_blocker not in blockers:
                problems.append(f"blocked by {blockers or '(nothing)'}, "
                                f"want {want_blocker!r}")
            want_rc = 1 if want_verdict == "NO-GO" else 0
            if rc != want_rc:
                problems.append(f"exit {rc}, want {want_rc}")
            # The waiver must be LOUD, not silently absent.
            if waived and "refimpl-passes" not in unproven:
                problems.append("a waived both-ways proof was not reported "
                                f"UNPROVEN (unproven={unproven})")
            # The clean case must not be muddied by unproven properties.
            if name == "good-scaffold" and unproven:
                problems.append(f"a complete scaffold left {unproven} unproven")
            # Whatever happened, the gate must leave the tree as it found it.
            st = run(["git", "status", "--porcelain"], cwd=root).stdout
            if any(l[:2] != "??" for l in st.splitlines() if l.strip()):
                if name != "dirty-baseline":
                    problems.append("gate left the tree modified: "
                                    + st.strip().replace("\n", "; "))

            mark = "ok  " if not problems else "FAIL"
            detail = f"{got}" + (f" [{','.join(blockers)}]" if blockers else "")
            print(f"  {mark} {name:<22} {detail}")
            for p in problems:
                print(f"       -> {p}")
            if a.v:
                print("       raw:", json.dumps(data)[:1500])
            if problems:
                failures.append(name)

        # --review tests: these gate first (to record the scope contract), then
        # mutate the tree the way a model would, then audit. They assert on the
        # review verdict rather than the pre-flight one, so they run separately.
        for name, fn in REVIEW_TESTS:
            if a.k and a.k not in name:
                continue
            ran += 1
            ok, why = fn(tmp)
            print(f"  {'ok  ' if ok else 'FAIL'} {name:<22} {why}")
            if not ok:
                failures.append(name)
    finally:
        if a.keep:
            print(f"\n(kept: {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n--- {len(failures)} of {ran} failed ---")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("SELF_CHECK_OK: the gate discriminates, and names the right cause")
    return 0


if __name__ == "__main__":
    sys.exit(main())
