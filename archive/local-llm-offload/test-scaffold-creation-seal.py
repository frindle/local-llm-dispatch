#!/usr/bin/env python3
"""Both-ways proof that --seal-baseline TRACKS a creation task's target stub.

On an EDIT task the target is tracked at HEAD and seal deliberately leaves it
alone. On a CREATION task the scaffold seeds an untracked stub, and leaving THAT
untracked broke two things at once:

  * verify-relevance UNPROVEN -- the refimpl writes an ADDED file, `git diff -U0`
    has nothing to mutate, and the gate bails with "the reference impl produced no
    tracked diff to mutate (added files only?)". The verify is never measured
    against the property; it is waived, not proven.
  * the stub is CLOBBERED -- a successful preflight applies the refimpl into the
    stub, and refimpl-reverted cannot restore untracked content, so the real
    implementation is left on disk. The enqueue-time preflight then reads
    "baseline PASSES" and the dispatched job is vacuous (three Amazon auto-order
    dispatches, 2026-09-14).

Asserted here:
  1. creation task: the stub is UNTRACKED before seal
  2. creation task: seal TRACKS it, and HEAD carries the stub (not an impl)
  3. creation task: after seal, an edit to the target is a real tracked DIFF --
     which is the thing the mutation gate needs to exist at all
  4. creation task: `git checkout -- <target>` restores the stub (revertibility)
  5. EDIT task: an already-tracked target is NOT re-committed by seal -- the
     no-op half, so this is a discriminating test and not a blanket "commit
     everything"

Run: test-scaffold-creation-seal.py [-v]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
import os
SCAFFOLD = Path(os.environ.get("SCAFFOLD_SRC") or BIN / "ollama-dispatch-scaffold")

EXISTING_TARGET = '''\
def is_safe(item, flag):
    return bool(flag) and (item.get("count") or 0) > 0
'''


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd and str(cwd), capture_output=True, text=True)


def git(root, *a):
    return run(["git", "-C", str(root), *a])


def make_repo(root: Path, with_target: bool):
    """A repo whose HEAD has a placeholder file, and optionally the target."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# fixture repo\n")
    if with_target:
        (root / "target.py").write_text(EXISTING_TARGET)
    run(["git", "init", "-q", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "t@t"], cwd=root)
    run(["git", "config", "user.name", "t"], cwd=root)
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-qm", "baseline"], cwd=root)
    return root


def scaffold(repo: Path, dest: Path, target: str, label: str):
    return run([sys.executable, str(SCAFFOLD), "--repo", str(repo),
                "--dest", str(dest), "--label", label, "--lang", "python",
                "--target", target, "--kind", "symbol", "--symbol", "is_safe"])


def tracked(root: Path, path: str) -> bool:
    return git(root, "ls-files", "--error-unmatch", "--", path).returncode == 0


def main():
    verbose = "-v" in sys.argv
    failures = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<32} {got!r}"
              + ("" if ok else f"  (want {want!r})"))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # --- CREATION task -------------------------------------------------
        repo = make_repo(root / "creation_repo", with_target=False)
        wt = root / "wt_creation"
        p = scaffold(repo, wt, "newthing.py", "creationseal")
        if not wt.is_dir():
            print(f"  FAIL scaffold-creation  no worktree: "
                  f"{(p.stdout + p.stderr)[-400:]}")
            print("\n--- 1 of 1 failed ---")
            return 1
        if verbose:
            print(p.stdout + p.stderr)

        check("creation: stub exists", (wt / "newthing.py").is_file(), True)
        check("creation: stub untracked pre-seal", tracked(wt, "newthing.py"), False)
        stub_text = (wt / "newthing.py").read_text()

        s = run([sys.executable, str(SCAFFOLD), "--seal-baseline", str(wt)])
        if verbose:
            print(s.stdout + s.stderr)
        check("creation: seal announces it", "creation task" in (s.stdout + s.stderr), True)
        check("creation: stub TRACKED post-seal", tracked(wt, "newthing.py"), True)
        check("creation: HEAD carries the STUB",
              git(wt, "show", "HEAD:newthing.py").stdout, stub_text)
        check("creation: launch tree is clean",
              git(wt, "status", "--porcelain", "--", "newthing.py").stdout.strip(), "")

        # The property the mutation gate actually needs: an implementation
        # written over the stub is a TRACKED diff, not an added file.
        (wt / "newthing.py").write_text(stub_text + "\n\ndef is_safe(i, f):\n    return False\n")
        diff = git(wt, "diff", "-U0", "--", "newthing.py").stdout
        check("creation: refimpl yields a tracked diff", bool(diff.strip()), True)
        check("creation: diff is a modification, not an add",
              "new file mode" not in diff, True)

        # And it reverts, which is what stops the preflight clobbering the stub.
        git(wt, "checkout", "--", "newthing.py")
        check("creation: git revert restores the stub",
              (wt / "newthing.py").read_text(), stub_text)

        # --- CREATION task whose stub was CLOBBERED before the seal ----------
        # (2026-10-06, rt-egift-link-s1 s1-parse-link: the re-seal after authoring
        # committed a 49-line implementation as the "clean launch baseline"; the
        # verify was green at baseline and the slicer skipped the slice.)
        repo3 = make_repo(root / "clobber_repo", with_target=False)
        wt3 = root / "wt_clobber"
        os.environ["OLLAMA_DISPATCH_HOME"] = str(root / "odhome")
        p3 = scaffold(repo3, wt3, "newthing.py", "clobberseal")
        if not wt3.is_dir():
            failures.append("scaffold-clobber")
        else:
            stub3 = (wt3 / "newthing.py").read_text()
            impl = "def is_safe(item, flag):\n    return bool(flag) and item.get('count', 0) > 0\n" * 3
            (wt3 / "newthing.py").write_text(impl)
            s3 = run([sys.executable, str(SCAFFOLD), "--seal-baseline", str(wt3)])
            if verbose:
                print(s3.stdout + s3.stderr)
            check("clobber: HEAD carries the STUB, not the impl",
                  git(wt3, "show", "HEAD:newthing.py").stdout, stub3)
            aside = list((root / "odhome" / "presealed").rglob("newthing.py.*"))
            check("clobber: impl kept aside outside the tree",
                  bool(aside) and aside[0].read_text() == impl, True)
            check("clobber: seal says why", "holds an IMPLEMENTATION" in (s3.stdout + s3.stderr), True)

        # --- EDIT task (the no-op half) ------------------------------------
        repo2 = make_repo(root / "edit_repo", with_target=True)
        wt2 = root / "wt_edit"
        p2 = scaffold(repo2, wt2, "target.py", "editseal")
        if not wt2.is_dir():
            print(f"  FAIL scaffold-edit  no worktree: "
                  f"{(p2.stdout + p2.stderr)[-400:]}")
            failures.append("scaffold-edit")
        else:
            head_before = git(wt2, "rev-parse", "HEAD:target.py").stdout.strip()
            s2 = run([sys.executable, str(SCAFFOLD), "--seal-baseline", str(wt2)])
            if verbose:
                print(s2.stdout + s2.stderr)
            check("edit: target already tracked", tracked(wt2, "target.py"), True)
            check("edit: seal does NOT re-commit the target",
                  git(wt2, "rev-parse", "HEAD:target.py").stdout.strip(), head_before)
            check("edit: seal stays silent about creation",
                  "creation task" in (s2.stdout + s2.stderr), False)

    print(f"\n--- {len(failures)} failed ---")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("SELF_CHECK_OK: a creation task's stub is sealed into the baseline "
          "(relevance is provable, the stub is revertible); an edit task's "
          "target is untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
