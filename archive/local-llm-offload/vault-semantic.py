#!/usr/bin/env python3
"""Semantic search + near-duplicate detection over the Obsidian Claude/ vault.

Embeds every markdown section (by heading) with Ollama `nomic-embed-text` running on
Unraid (idle box, tiny 0.3GB model -- real offload), reading the LIVE vault via the
Obsidian Local REST API (the on-disk mirrors lag, so REST is the only authoritative
source). Caches vectors so re-indexing only re-embeds changed notes.

  vault-semantic.py index                 # build/update the embedding cache
  vault-semantic.py search "query" [-k N]  # top-N most similar sections
  vault-semantic.py dupes [-t 0.90]        # near-duplicate section pairs (consolidation targets)

Cache: ~/bin/vault-embeddings.json. Config below; env overrides supported.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import urllib.request
from pathlib import Path

# Endpoint/token: env VAULT_URL / VAULT_TOKEN_FILE -> ~/.config/vault/env -> legacy defaults
# (vault_conf.py; the Basic Memory write-shim speaks the same REST on :27123).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_conf  # noqa: E402
OBSIDIAN = vault_conf.vault_url("http://10.0.3.209:27123")
OBSIDIAN_TOKEN = vault_conf.vault_token()
# --- Embedding host: Unraid, resolved from the SHARED host table (2026-10-01) ---
# This used to hardcode http://10.0.7.143:11434. The dispatch pipeline keeps the
# one authoritative Unraid URL (and its usable VRAM) in ~/.ollama-dispatch/hosts.json
# -- the same file ollama-worker/ollama-queue read -- so the IP lived in two places
# and a re-addressed box would have silently sent every embedding to a dead port
# (or, worse, nowhere obvious). Resolve from that table, keep the old literal as
# the fallback so this never hard-fails on a missing/garbled file, and keep EMBED_HOST
# as the explicit override.
#
# FLAG: VAULT_EMBED_HOST_LANE (default "unraid"). "local" sends embeddings to the
# Mac's own Ollama instead -- an escape hatch for Unraid being down, not a routing
# decision anyone should need. Unraid is the right home for this work and is ON by
# default: nomic-embed-text is 0.27GB of weights against Unraid's 10.3GB usable
# gate (~2.7% -- the one genuinely free-lunch offload in the whole pipeline), the
# box is otherwise idle, and it keeps the Mac's GPU entirely for dispatch.
HOSTS_JSON = Path(os.environ.get("OLLAMA_DISPATCH_HOSTS")
                  or (Path.home() / ".ollama-dispatch" / "hosts.json"))
_UNRAID_FALLBACK = "http://10.0.7.143:11434"
EMBED_LANE = os.environ.get("VAULT_EMBED_HOST_LANE", "unraid").strip().lower()


def resolve_embed_host(lane: str = None, hosts_json: Path = None) -> str:
    """The base URL to send /api/embed to. PURE apart from reading hosts.json, so
    --self-test drives the exact resolution the commands use.

    lane "local" -> the Mac's own Ollama. Any other lane name is looked up in
    hosts.json; "unraid" additionally falls back to its known address rather than
    silently degrading to localhost (which would pull a GPU load onto the Mac --
    the opposite of what this offload is for)."""
    lane = (lane or EMBED_LANE)
    if lane == "local":
        return "http://127.0.0.1:11434"
    url = ""
    try:
        tbl = json.loads(Path(hosts_json or HOSTS_JSON).read_text())
        url = str(((tbl.get("hosts") or {}).get(lane) or {}).get("url") or "")
    except Exception:
        url = ""
    if url:
        return url.rstrip("/")
    if lane == "unraid":
        return _UNRAID_FALLBACK
    raise SystemExit(f"vault-semantic: no url for host lane {lane!r} in {hosts_json or HOSTS_JSON}")


EMBED_HOST = os.environ.get("EMBED_HOST") or resolve_embed_host()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
ROOT = os.environ.get("VAULT_ROOT", "Claude")
CACHE = Path.home() / "bin" / "vault-embeddings.json"
MIN_CHUNK_CHARS = 60


def _obs(path):
    req = urllib.request.Request(f"{OBSIDIAN}/vault/{path}",
                                 headers={"Authorization": f"Bearer {OBSIDIAN_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def list_notes(dir_path):
    """Recursively list .md files under dir_path (vault-relative)."""
    out = []
    try:
        listing = json.loads(_obs(dir_path + "/" if not dir_path.endswith("/") else dir_path))
    except Exception as e:
        print(f"  ! list {dir_path}: {e}", file=sys.stderr)
        return out
    for name in listing.get("files", []):
        full = f"{dir_path.rstrip('/')}/{name}"
        if name.endswith("/"):
            out += list_notes(full.rstrip("/"))
        elif name.endswith(".md"):
            out.append(full)
    return out


def chunk_note(path, text):
    """Split a note into sections by markdown heading. Each chunk carries the note
    name + heading trail as context so the embedding knows where it came from."""
    title = path.split("/")[-1][:-3]
    chunks, cur_head, cur_lines = [], "", []

    def flush():
        body = "\n".join(cur_lines).strip()
        if len(body) >= MIN_CHUNK_CHARS or (cur_head and body):
            head = cur_head.lstrip("#").strip()
            label = f"{title} :: {head}" if head else title
            chunks.append({"heading": head, "text": f"{label}\n{body}".strip()})

    for line in text.splitlines():
        if line.lstrip().startswith("#") and line.lstrip().lstrip("#").startswith(" "):
            flush()
            cur_head, cur_lines = line.strip(), []
        else:
            cur_lines.append(line)
    flush()
    if not chunks and text.strip():
        chunks = [{"heading": "", "text": f"{title}\n{text.strip()}"}]
    return chunks


def embed(texts):
    """Embed a list of strings via Ollama /api/embed (batched)."""
    if not texts:
        return []
    data = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(f"{EMBED_HOST}/api/embed", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {"model": EMBED_MODEL, "notes": {}}


def save_cache(cache):
    CACHE.write_text(json.dumps(cache))


def cmd_index(args):
    cache = load_cache()
    if cache.get("model") != EMBED_MODEL:
        cache = {"model": EMBED_MODEL, "notes": {}}
    notes = list_notes(ROOT)
    print(f"{len(notes)} notes under {ROOT}/")
    changed = embedded = 0
    for i, path in enumerate(notes, 1):
        try:
            text = _obs(path)
        except Exception as e:
            print(f"  ! read {path}: {e}", file=sys.stderr)
            continue
        h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        entry = cache["notes"].get(path)
        if entry and entry.get("hash") == h and not args.force:
            continue
        chunks = chunk_note(path, text)
        if not chunks:
            continue
        vecs = embed([c["text"] for c in chunks])
        for c, v in zip(chunks, vecs):
            c["vec"] = v
        cache["notes"][path] = {"hash": h, "chunks": chunks}
        changed += 1
        embedded += len(chunks)
        print(f"  [{i}/{len(notes)}] {path}  ({len(chunks)} sections)")
    # drop deleted notes
    for gone in set(cache["notes"]) - set(notes):
        del cache["notes"][gone]
    save_cache(cache)
    total = sum(len(n["chunks"]) for n in cache["notes"].values())
    print(f"indexed: {changed} notes changed, {embedded} sections embedded; cache has {total} sections")


def all_chunks(cache):
    for path, n in cache["notes"].items():
        for c in n["chunks"]:
            yield path, c


def cmd_search(args):
    cache = load_cache()
    qv = embed([args.query])[0]
    scored = [(cosine(qv, c["vec"]), path, c) for path, c in all_chunks(cache) if "vec" in c]
    scored.sort(reverse=True, key=lambda x: x[0])
    for score, path, c in scored[:args.k]:
        snippet = " ".join(c["text"].split())[:200]
        print(f"\n{score:.3f}  {path}" + (f"  ## {c['heading']}" if c["heading"] else ""))
        print(f"       {snippet}")


def cmd_dupes(args):
    import numpy as np
    cache = load_cache()
    items = [(path, c) for path, c in all_chunks(cache) if "vec" in c]
    if not args.include_history:
        # _history/ holds archived copies that duplicate live notes -- pure noise here.
        items = [(p, c) for p, c in items if "/_history/" not in p]
    if len(items) < 2:
        print("nothing to compare (index first with: vault-semantic.py index)")
        return
    M = np.asarray([c["vec"] for _, c in items], dtype=np.float32)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    sim = M @ M.T                      # full cosine matrix (fast; ~50MB at a few k sections)
    iu = np.triu_indices(len(items), k=1)
    sc = sim[iu]
    keep = np.where(sc >= args.threshold)[0]
    keep = keep[np.argsort(-sc[keep])]
    pairs = []
    for idx in keep:
        i, j = int(iu[0][idx]), int(iu[1][idx])
        pi, ci = items[i]; pj, cj = items[j]
        if not args.within_note and pi == pj:
            continue
        pairs.append((float(sc[idx]), pi, ci, pj, cj))
    print(f"{len(pairs)} near-duplicate section pairs at cosine >= {args.threshold}\n")
    for s, pi, ci, pj, cj in pairs[:args.limit]:
        print(f"{s:.3f}")
        print(f"   A: {pi}" + (f"  ## {ci['heading']}" if ci["heading"] else ""))
        print(f"   B: {pj}" + (f"  ## {cj['heading']}" if cj["heading"] else ""))


def _self_test() -> bool:
    """Unit-test the host resolution + chunking. No network, no GPU; writes only
    into a throwaway temp dir. Same PASS/FAIL convention as the dispatch tools."""
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    td = Path(tempfile.mkdtemp(prefix="vault-semantic-selftest-"))
    good = td / "hosts.json"
    good.write_text(json.dumps({"hosts": {"unraid": {
        "url": "http://10.0.7.99:11434", "usable_bytes": 10307921510}}}))
    check("embeddings resolve to the UNRAID url from the shared host table",
          resolve_embed_host("unraid", good), "http://10.0.7.99:11434")
    # REVERT TEST for the whole point of this change: a re-addressed box must move
    # the embeddings with it. If resolution were still hardcoded, this would return
    # the old literal.
    check("...and a re-addressed Unraid moves the embeddings with it (not the old literal)",
          resolve_embed_host("unraid", good) != _UNRAID_FALLBACK, True)
    bad = td / "missing.json"
    check("a missing/garbled host table falls back to Unraid, never silently to localhost",
          resolve_embed_host("unraid", bad), _UNRAID_FALLBACK)
    check("lane 'local' is the explicit escape hatch to the Mac",
          resolve_embed_host("local", good), "http://127.0.0.1:11434")
    try:
        resolve_embed_host("nosuchbox", good)
        check("an unknown lane is a hard error, not a silent localhost fallback", False, True)
    except SystemExit:
        check("an unknown lane is a hard error, not a silent localhost fallback", True, True)
    check("the shipped default lane is unraid", EMBED_LANE, "unraid")
    check("the embedding model is the 0.27GB nomic model that fits Unraid trivially",
          EMBED_MODEL, "nomic-embed-text")
    chunks = chunk_note("Claude/Reference/x.md",
                        "i" * 80 + "\n\n## Head A\n" + "a" * 80)
    check("chunking still labels sections with note :: heading",
          [c["heading"] for c in chunks], ["", "Head A"])
    import shutil
    shutil.rmtree(td, ignore_errors=True)
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


def main():
    if "--self-test" in sys.argv[1:]:
        return 0 if _self_test() else 1
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_index)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("-k", type=int, default=8); p.set_defaults(func=cmd_search)
    p = sub.add_parser("dupes"); p.add_argument("-t", "--threshold", type=float, default=0.90)
    p.add_argument("--within-note", action="store_true"); p.add_argument("--include-history", action="store_true",
        help="include Claude/_history/ archives (excluded by default -- they duplicate live notes)")
    p.add_argument("--limit", type=int, default=40); p.set_defaults(func=cmd_dupes)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
