#!/usr/bin/env python3
"""Regenerate frindle/briefd-knowledge from frindle/obsidian-vault-backup.

briefd requires a knowledge repo laid out as domain/ conventions/ projects/<name>/
at the root (see SPEC.md in the briefd-trial checkout). The real vault uses its
own Claude/<Topic> layout, so this script mirrors it into that shape on every run
and pushes. Git is still the source of truth for the ACTUAL vault (this script
never writes back to obsidian-vault-backup) -- this is a one-way, regenerate-from-
scratch projection, safe to force-push every time.

Mapping (edit here, not by hand-patching the output repo):
  Claude/Reference, Claude/Gating          -> conventions/
  Claude/Homelab, Claude/Ollama,
  Claude/Operations, Claude/Research,
  Claude/API-Docs                          -> domain/
  Claude/Projects/<name>.md                -> projects/<slug>/index.md
  Everything else (Handoff, SessionIndex,
  _history, To-Do.md, Waiting-on-Penn.md,
  Session-Brief.md, Agent-Dispatch-Log.md,
  "Notes from Penn.md" -- Penn-only per
  feedback_notes_from_penn_penn_only)      -> excluded (ephemeral/operational
                                               logs and Penn-only notes, not
                                               stable knowledge)
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

VAULT_SOURCE = "https://github.com/frindle/obsidian-vault-backup.git"
MIRROR_REPO = "https://github.com/frindle/briefd-knowledge.git"
WORK = Path.home() / ".briefd" / "sync-work"
VAULT_CLONE = WORK / "vault-source"
MIRROR_CLONE = WORK / "mirror-build"

CONVENTIONS_DIRS = ["Reference", "Gating"]
DOMAIN_DIRS = ["Homelab", "Ollama", "Operations", "Research", "API-Docs"]
PROJECTS_DIR = "Projects"


def run(cmd, cwd=None, check=True):
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def slugify(name: str) -> str:
    stem = name[:-3] if name.endswith(".md") else name
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "untitled"


def sync_vault():
    WORK.mkdir(parents=True, exist_ok=True)
    if VAULT_CLONE.exists():
        run(["git", "-C", str(VAULT_CLONE), "fetch", "origin"])
        run(["git", "-C", str(VAULT_CLONE), "reset", "--hard", "origin/HEAD"])
    else:
        run(["git", "clone", "--depth", "1", VAULT_SOURCE, str(VAULT_CLONE)])
    return get_commit(VAULT_CLONE)


def get_commit(repo: Path) -> str:
    return run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def rebuild_mirror_tree(src_claude: Path, dest: Path):
    for sub in ("domain", "conventions", "projects"):
        shutil.rmtree(dest / sub, ignore_errors=True)

    for d in CONVENTIONS_DIRS:
        s = src_claude / d
        if s.is_dir():
            shutil.copytree(s, dest / "conventions" / d)

    for d in DOMAIN_DIRS:
        s = src_claude / d
        if s.is_dir():
            shutil.copytree(s, dest / "domain" / d)

    proj_src = src_claude / PROJECTS_DIR
    if proj_src.is_dir():
        for f in sorted(proj_src.glob("*.md")):
            slug = slugify(f.name)
            out_dir = dest / "projects" / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, out_dir / "index.md")


def main():
    vault_commit = sync_vault()

    WORK.mkdir(parents=True, exist_ok=True)
    if not (MIRROR_CLONE / ".git").exists():
        shutil.rmtree(MIRROR_CLONE, ignore_errors=True)
        r = run(["git", "clone", MIRROR_REPO, str(MIRROR_CLONE)], check=False)
        if r.returncode != 0:
            # empty repo, no commits yet -- init fresh and set the remote
            MIRROR_CLONE.mkdir(parents=True, exist_ok=True)
            run(["git", "init"], cwd=MIRROR_CLONE)
            run(["git", "remote", "add", "origin", MIRROR_REPO], cwd=MIRROR_CLONE)
            run(["git", "checkout", "-b", "main"], cwd=MIRROR_CLONE)

    rebuild_mirror_tree(VAULT_CLONE / "Claude", MIRROR_CLONE)

    readme = MIRROR_CLONE / "README.md"
    readme.write_text(
        "# briefd-knowledge\n\n"
        "Auto-generated mirror of frindle/obsidian-vault-backup, restructured "
        "into the domain/conventions/projects layout briefd requires. Do not "
        "edit by hand -- regenerated (force-pushed) by "
        "~/bin/briefd-knowledge-sync.py from the vault's Claude/ folder.\n\n"
        f"Source vault commit: {vault_commit}\n"
    )

    run(["git", "add", "-A"], cwd=MIRROR_CLONE)
    diff = run(["git", "diff", "--cached", "--stat"], cwd=MIRROR_CLONE)
    if not diff.stdout.strip():
        print("no change since last sync")
        return
    run(["git", "-c", "user.name=briefd-sync", "-c", "user.email=briefd@localhost",
         "commit", "-m", f"sync from obsidian-vault-backup@{vault_commit[:12]}"],
        cwd=MIRROR_CLONE)
    run(["git", "push", "origin", "main"], cwd=MIRROR_CLONE)
    print(f"pushed: mirror updated from vault commit {vault_commit[:12]}")


if __name__ == "__main__":
    main()
