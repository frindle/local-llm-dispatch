#!/usr/bin/env python3
"""vault-graph.py - build a private, self-contained interactive graph of the vault.

Reads the LIVE vault through the vault shim REST API (read-only: only GET), parses
[[wikilinks]], markdown links to .md notes and Basic Memory relation lines, and
writes ONE html file (d3 inlined, no network needed to view) to
~/vault-graph/graph.html (mode 600 inside a 700 dir).

Usage:
  vault-graph.py [--open] [--refresh] [--include-sessionindex] [--include-pending]
                 [--out PATH] [--from-dir DIR]

Only counts are ever printed; note content and the auth token never are.
d3 v7 is downloaded once (BUILD time) from cdnjs and cached at ~/vault-graph/d3.min.js.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
GRAPH_DIR = os.path.join(HOME, "vault-graph")
D3_URL = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
SKIP_PENDING = "Claude/Pending-Import/"
SKIP_SESSION = "Claude/SessionIndex/"

# --------------------------------------------------------------------------- parsing

FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
WIKI_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REL_PREFIX_RE = re.compile(r"^\s*[-*+]\s+([A-Za-z_][\w \-]*?)\s+$")
ATTACH_RE = re.compile(r"\.(png|jpe?g|gif|svg|pdf|webp|mp4|mp3|m4a|wav|csv|json|txt|zip|heic)$", re.I)


def strip_code(text):
    """Blank out fenced code blocks and inline code (keeps line structure)."""
    out = []
    fence = None  # (char, length)
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                out.append("")
                continue
            out.append(INLINE_CODE_RE.sub("", line))
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] \
                    and line.strip().strip(fence[0]) == "":
                fence = None
            out.append("")
    return "\n".join(out)


def split_frontmatter(text):
    if text.startswith("---"):
        m = re.match(r"^---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)", text, re.S)
        if m:
            return m.group(1), text[m.end():]
    return "", text


def parse_links(text):
    """Return list of (raw_target, kind, relation) from note text.

    kind: 'wiki' | 'md'.  relation: Basic Memory relation type for lines such as
    '- implements [[Target]]' else None.  Links inside code are ignored; non-note
    targets (images, pdfs, urls) are dropped."""
    clean = strip_code(text)
    found = []
    for line in clean.split("\n"):
        for m in WIKI_RE.finditer(line):
            inner = m.group(2)
            target = re.split(r"\\?\|", inner, maxsplit=1)[0]
            target = target.rstrip("\\").strip()
            target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
            if not target:
                continue  # [[#heading]] self link
            if ATTACH_RE.search(target):
                continue  # embed / attachment
            rel = None
            if not m.group(1):
                pm = REL_PREFIX_RE.match(line[:m.start()])
                if pm:
                    rel = pm.group(1).strip()
            found.append((target, "wiki", rel))
        for m in MDLINK_RE.finditer(line):
            if m.group(1) == "!":
                continue
            dest = m.group(2).strip()
            if dest.startswith("<") and dest.endswith(">"):
                dest = dest[1:-1]
            if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", dest) or dest.startswith("#"):
                continue  # url / mailto / anchor
            dest = urllib.parse.unquote(dest.split("#", 1)[0].split("?", 1)[0])
            if not dest.lower().endswith(".md"):
                continue
            found.append((dest, "md", None))
    return found


def norm_key(s):
    """Obsidian-ish loose key: case-insensitive, space/hyphen/underscore equivalent."""
    s = s.strip()
    if s.lower().endswith(".md"):
        s = s[:-3]
    return re.sub(r"[\s_\-]+", " ", s.lower()).strip()


class Resolver:
    def __init__(self, paths):
        self.paths = sorted(paths)
        self.exact = {p: p for p in self.paths}
        self.exact_noext = {p[:-3]: p for p in self.paths if p.lower().endswith(".md")}
        self.by_norm_path = {}
        self.by_base = {}
        for p in self.paths:
            self.by_norm_path.setdefault(norm_key(p), []).append(p)
            self.by_base.setdefault(norm_key(os.path.basename(p)), []).append(p)

    @staticmethod
    def _pick(cands, src):
        if len(cands) == 1:
            return cands[0]
        sdir = os.path.dirname(src) if src else ""
        same = [c for c in cands if os.path.dirname(c) == sdir]
        pool = same or cands
        return sorted(pool, key=lambda c: (c.count("/"), len(c), c))[0]

    def resolve(self, target, src=None, kind="wiki"):
        """Return resolved vault path or None (ghost)."""
        t = target.strip().replace("\\", "/").lstrip("/")
        if kind == "md" and src is not None:
            rel = os.path.normpath(os.path.join(os.path.dirname(src), t)).replace("\\", "/")
            if rel in self.exact:
                return rel
        t = re.sub(r"^\./", "", t)
        if t in self.exact:
            return t
        if t in self.exact_noext:
            return self.exact_noext[t]
        k = norm_key(t)
        if k in self.by_norm_path:
            return self._pick(self.by_norm_path[k], src)
        if "/" in k:  # folder-qualified suffix, e.g. [[Reference/Note]]
            cands = [p for nk, ps in self.by_norm_path.items()
                     if nk.endswith("/" + k) for p in ps]
            if cands:
                return self._pick(cands, src)
            base = norm_key(os.path.basename(t))
            if base in self.by_base:
                folder = k.rsplit("/", 1)[0]
                c2 = [p for p in self.by_base[base] if folder in norm_key(os.path.dirname(p))]
                return self._pick(c2, src) if c2 else None
            return None
        if k in self.by_base:
            return self._pick(self.by_base[k], src)
        return None


def note_title(path, text):
    fm, body = split_frontmatter(text)
    m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", fm, re.M)
    h = re.search(r"^#[ \t]+(.+?)\s*#*\s*$", strip_code(body), re.M)
    if h:
        return h.group(1).strip()[:160]
    if m:
        return m.group(1).strip()[:160]
    return os.path.basename(path)[:-3] if path.lower().endswith(".md") else os.path.basename(path)


def note_snippet(text, n=300):
    _, body = split_frontmatter(text)
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"^#{1,6}[ \t]+", "", body, flags=re.M)
    body = re.sub(r"\[\[([^\]|#]*)[^\]|]*?(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:n]


def build_graph(notes):
    """notes: dict path -> text.  Returns dict(nodes, edges, folders, stats)."""
    paths = sorted(notes)
    res = Resolver(paths)
    idx = {p: i for i, p in enumerate(paths)}
    nodes = []
    for p in paths:
        top = p.split("/", 1)[0] if "/" in p else "(root)"
        nodes.append({"t": note_title(p, notes[p]), "p": p, "f": top,
                      "s": note_snippet(notes[p]), "g": 0})
    ghost_idx = {}
    edges = set()
    for p in paths:
        for target, kind, _rel in parse_links(notes[p]):
            r = res.resolve(target, p, kind)
            if r is not None:
                j = idx[r]
            else:
                gk = norm_key(target)
                if not gk:
                    continue
                if gk not in ghost_idx:
                    ghost_idx[gk] = len(nodes)
                    nodes.append({"t": target.strip().rsplit("/", 1)[-1][:160],
                                  "p": target.strip()[:200], "f": "(unresolved)", "s": "", "g": 1})
                j = ghost_idx[gk]
            if j != idx[p]:
                edges.add((idx[p], j))
    edges = sorted(edges)
    deg = [0] * len(nodes)
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1
    real = len(paths)
    counts = {}
    for n in nodes:
        if not n["g"]:
            counts[n["f"]] = counts.get(n["f"], 0) + 1
    folders = sorted(counts, key=lambda f: (-counts[f], f))
    stats = {"nodes": real, "edges": len(edges), "ghosts": len(nodes) - real,
             "orphans": sum(1 for i in range(real) if deg[i] == 0)}
    return {"nodes": nodes, "edges": [list(e) for e in edges], "folders": folders, "stats": stats}


# --------------------------------------------------------------------------- html

def safe_json(obj):
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_html(graph, d3_src):
    data = {k: graph[k] for k in ("nodes", "edges", "folders", "stats")}
    d3_src = re.sub(r"^//[^\n]*\n", "", d3_src)          # drop banner comment (has a URL)
    d3_src = d3_src.replace("</script", "<\\/script")
    out = TEMPLATE.replace("/*__D3__*/", d3_src, 1)
    return out.replace("/*__DATA__*/", safe_json(data), 1)  # data last: nothing re-substituted


# --------------------------------------------------------------------------- sources

class DirSource:
    def __init__(self, root):
        self.root = root

    def list_files(self):
        out = []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for f in fns:
                out.append(os.path.relpath(os.path.join(dp, f), self.root).replace(os.sep, "/"))
        return out

    def read(self, path):
        with open(os.path.join(self.root, path), encoding="utf-8", errors="replace") as fh:
            return fh.read()


class HttpSource:
    def __init__(self, url, auth, cache_dir, refresh=False, workers=8, skip_dirs=()):
        self.url = url.rstrip("/")
        self._auth = auth  # never printed
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.workers = workers
        self.skip_dirs = tuple(skip_dirs)
        self.cache_hits = 0
        self.fetched = 0

    def _req(self, path, headers=None):
        h = {"Authorization": self._auth, "Accept": "application/json"}
        h.update(headers or {})
        return urllib.request.Request(self.url + "/vault/" + urllib.parse.quote(path), headers=h, method="GET")

    def _get(self, path, headers=None, retries=3):
        last = None
        for _ in range(retries):
            try:
                with urllib.request.urlopen(self._req(path, headers), timeout=30) as r:
                    return r.status, r.headers.get("ETag"), r.read()
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, None, b""
                last = e
                if e.code in (401, 403, 404):
                    break
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError("GET failed for a vault path: %s" % type(last).__name__)

    def _list_dir(self, d):
        _, _, body = self._get(d)
        return json.loads(body.decode("utf-8")).get("files", [])

    def list_files(self):
        files, pending = [], [""]
        with ThreadPoolExecutor(self.workers) as ex:
            while pending:
                results = list(ex.map(self._list_dir, pending))
                nxt = []
                for d, entries in zip(pending, results):
                    for e in entries:
                        full = d + e
                        if e.endswith("/"):
                            if full in self.skip_dirs:
                                continue
                            nxt.append(full)
                        else:
                            files.append(full)
                pending = nxt
        return files

    def _cache_path(self, path):
        return os.path.join(self.cache_dir, hashlib.sha1(path.encode()).hexdigest() + ".json")

    def read(self, path):
        cp = self._cache_path(path)
        cached = None
        if not self.refresh and os.path.exists(cp):
            try:
                with open(cp, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception:  # noqa: BLE001
                cached = None
        hdr = {"Accept": "text/markdown"}
        if cached and cached.get("etag"):
            hdr["If-None-Match"] = cached["etag"]
        status, etag, body = self._get(path, hdr)
        if status == 304 and cached:
            self.cache_hits += 1
            return cached["text"]
        text = body.decode("utf-8", errors="replace")
        if cached and etag and cached.get("etag") == etag:
            self.cache_hits += 1
            return cached["text"]
        self.fetched += 1
        fd = os.open(cp + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"path": path, "etag": etag, "text": text}, fh)
        os.replace(cp + ".tmp", cp)
        return text


def vault_conf(what):
    return subprocess.check_output(["python3", os.path.join(HOME, "bin", "vault_conf.py"), what],
                                   text=True).strip()


def d3_ok(s):
    return (s.startswith("// https://d3js.org v7") and "forceSimulation" in s
            and "forceManyBody" in s and "zoomIdentity" in s and len(s) > 200000)


def ensure_d3(d3_path):
    if os.path.exists(d3_path):
        with open(d3_path, encoding="utf-8") as fh:
            s = fh.read()
        if d3_ok(s):
            return s
    with urllib.request.urlopen(D3_URL, timeout=60) as r:
        s = r.read().decode("utf-8")
    if not d3_ok(s):
        raise RuntimeError("downloaded d3.min.js failed verification; not embedding")
    fd = os.open(d3_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(s)
    return s


def prepare_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)


def write_private(path, content):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def collect(source, include_session=False, include_pending=False):
    skip = []
    if not include_pending:
        skip.append(SKIP_PENDING)
    if not include_session:
        skip.append(SKIP_SESSION)
    files = source.list_files()
    md = [f for f in files if f.lower().endswith(".md") and not any(f.startswith(s) for s in skip)]
    with ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(source.read, md))
    return dict(zip(md, texts))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--open", action="store_true", help="open result in default browser")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, refetch every note")
    ap.add_argument("--include-sessionindex", action="store_true")
    ap.add_argument("--include-pending", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--from-dir", help="read notes from a local directory instead of the vault API (testing)")
    ap.add_argument("--graph-dir", default=GRAPH_DIR)
    a = ap.parse_args(argv)
    out = a.out or os.path.join(a.graph_dir, "graph.html")

    prepare_dir(a.graph_dir)
    d3_src = ensure_d3(os.path.join(a.graph_dir, "d3.min.js"))
    if a.from_dir:
        src = DirSource(a.from_dir)
    else:
        cache = os.path.join(a.graph_dir, ".cache")
        prepare_dir(cache)
        skip_dirs = []
        if not a.include_pending:
            skip_dirs.append(SKIP_PENDING)
        if not a.include_sessionindex:
            skip_dirs.append(SKIP_SESSION)
        src = HttpSource(vault_conf("url"), vault_conf("auth"), cache, refresh=a.refresh, skip_dirs=skip_dirs)
    notes = collect(src, a.include_sessionindex, a.include_pending)
    g = build_graph(notes)
    prepare_dir(os.path.dirname(os.path.abspath(out)) if a.out is None else os.path.dirname(os.path.abspath(out)))
    write_private(out, render_html(g, d3_src))
    s = g["stats"]
    print("nodes=%d edges=%d orphans=%d ghosts=%d" % (s["nodes"], s["edges"], s["orphans"], s["ghosts"]))
    if isinstance(src, HttpSource):
        print("fetched=%d cached=%d" % (src.fetched, src.cache_hits))
    print("wrote", out)
    if a.open:
        subprocess.run(["open", out], check=False)
    return 0


# --------------------------------------------------------------------------- template

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>Vault graph</title>
<style>
:root{--bg:#1e1e1e;--panel:#262626;--line:#3a3a3a;--fg:#dcddde;--dim:#8a8a8f;--acc:#a78bfa}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;overflow:hidden}
canvas{position:fixed;inset:0;width:100%;height:100%;display:block;touch-action:none;cursor:grab}
#menuBtn{position:fixed;top:10px;left:10px;z-index:5;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:inherit}
#controls{position:fixed;top:48px;left:10px;z-index:4;width:250px;max-height:calc(100% - 60px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;display:none}
#controls.open{display:block}
#controls label{display:flex;align-items:center;gap:6px;margin:4px 0}
#controls input[type=search]{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 8px;font:inherit}
#controls input[type=range]{width:100%}
#controls select,#controls button.sm{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;font:inherit;padding:2px 6px}
#results{margin:4px 0 6px}
#results button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--fg);padding:4px 6px;border-radius:4px;font:inherit;cursor:pointer}
#results button:hover,#results button.on{background:#383838}
#results small,#panel small{color:var(--dim);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
h4{margin:10px 0 4px;color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
#legend .it{display:flex;align-items:center;gap:7px;padding:2px 0;cursor:pointer;user-select:none}
#legend .it.off{opacity:.35}
#legend .sw{width:11px;height:11px;border-radius:50%;flex:none}
#legend .ct{margin-left:auto;color:var(--dim)}
#stats{color:var(--dim);margin-top:8px;font-size:12px}
#panel{position:fixed;top:10px;right:10px;bottom:10px;width:340px;z-index:4;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;display:none}
#panel.open{display:block}
#panel h2{margin:0 24px 2px 0;font-size:16px;word-break:break-word}
#panel .path{color:var(--dim);font-size:12px;word-break:break-all;margin-bottom:8px}
#panel .snip{white-space:pre-wrap;word-break:break-word;color:#c4c4c8}
#panel ul{list-style:none;margin:0;padding:0}
#panel li button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--acc);padding:3px 4px;border-radius:4px;font:inherit;cursor:pointer}
#panel li button:hover{background:#383838}
#close{position:absolute;top:8px;right:8px;background:none;border:0;color:var(--dim);font-size:18px;cursor:pointer}
#tip{position:fixed;pointer-events:none;z-index:6;background:#000c;color:#fff;padding:3px 7px;border-radius:4px;font-size:12px;display:none;max-width:260px}
@media (max-width:700px){
 #controls{width:calc(100% - 20px);max-height:60%}
 #panel{top:auto;left:10px;right:10px;bottom:10px;width:auto;height:46%}
}
</style></head><body>
<canvas id="c"></canvas>
<button id="menuBtn" type="button">Menu</button>
<div id="controls">
 <input id="q" type="search" placeholder="Search title or path" autocomplete="off">
 <div id="results"></div>
 <label><input type="checkbox" id="tOrph" checked> Show orphans</label>
 <label><input type="checkbox" id="tGhost"> Show ghost (unresolved) nodes</label>
 <label><input type="checkbox" id="tLabel" checked> Show labels</label>
 <label><input type="checkbox" id="tLocal"> Local graph
  <select id="depth"><option>1</option><option>2</option><option>3</option></select> hops</label>
 <h4>Link distance <span id="vDist"></span></h4><input type="range" id="sDist" min="8" max="160" value="40">
 <h4>Repel <span id="vRep"></span></h4><input type="range" id="sRep" min="0" max="300" value="60">
 <div><button class="sm" id="fit" type="button">Fit view</button> <button class="sm" id="reheat" type="button">Re-run layout</button></div>
 <h4>Folders (click to filter)</h4><div id="legend"></div>
 <div id="stats"></div>
</div>
<div id="panel"><button id="close" type="button" aria-label="Close">x</button>
 <h2 id="pTitle"></h2><div class="path" id="pPath"></div><div class="snip" id="pSnip"></div>
 <h4>Outgoing links <span id="pOutN"></span></h4><ul id="pOut"></ul>
 <h4>Backlinks <span id="pInN"></span></h4><ul id="pIn"></ul></div>
<div id="tip"></div>
<script>/*__D3__*/</script>
<script>
window.__DATA=/*__DATA__*/;
</script>
<script>
(function(){
"use strict";
var D=window.__DATA,nodes=D.nodes,N=nodes.length;
var $=function(id){return document.getElementById(id)};
nodes.forEach(function(n,i){n.i=i;n.out=[];n.inn=[]});
D.edges.forEach(function(e){nodes[e[0]].out.push(e[1]);nodes[e[1]].inn.push(e[0])});
nodes.forEach(function(n){n.deg=n.out.length+n.inn.length;n.r=n.g?3:3.5+Math.min(24,Math.sqrt(n.inn.length)*2.4);n.nb=new Set(n.out.concat(n.inn))});
var PAL=["#a78bfa","#60a5fa","#34d399","#fbbf24","#f87171","#f472b6","#22d3ee","#fb923c","#a3e635","#c084fc"];
var folderColor={};D.folders.forEach(function(f,i){folderColor[f]=i<PAL.length?PAL[i]:d3.hsl((i*137.5)%360,.55,.62).formatHex()});
folderColor["(unresolved)"]="#6b7280";
function colorOf(n){return folderColor[n.f]||"#888"}

var canvas=$("c"),ctx=canvas.getContext("2d"),W=0,H=0,dpr=1,T=d3.zoomIdentity;
var S={sel:null,hover:null,off:new Set(),orph:true,ghost:false,label:true,local:false,depth:1};
var vis=[],links=[],dirty=false;
var linkF=d3.forceLink([]).distance(40).strength(.7);
var charge=d3.forceManyBody().strength(-60).distanceMax(500).theta(.9);
var sim=d3.forceSimulation([]).force("link",linkF).force("charge",charge)
 .force("x",d3.forceX().strength(.04)).force("y",d3.forceY().strength(.04))
 .force("collide",d3.forceCollide(function(n){return n.r+1.5}).strength(.7))
 .alphaDecay(.03).velocityDecay(.4).on("tick",req).on("end",function(){$("stats").dataset.settled="1";stats()});
function req(){if(!dirty){dirty=true;requestAnimationFrame(draw)}}
function resize(){dpr=window.devicePixelRatio||1;W=window.innerWidth;H=window.innerHeight;canvas.width=W*dpr;canvas.height=H*dpr;req()}
window.addEventListener("resize",resize);

function computeVisible(){
 var base=nodes.filter(function(n){
  if(n.g)return S.ghost;
  if(S.off.has(n.f))return false;
  if(!S.orph&&n.deg===0)return false;
  return true});
 if(S.local&&S.sel!=null){
  var ok=new Set(base.map(function(n){return n.i}));
  ok.add(S.sel);
  var seen=new Set([S.sel]),fr=[S.sel];
  for(var d=0;d<S.depth;d++){var nx=[];fr.forEach(function(i){nodes[i].nb.forEach(function(j){if(ok.has(j)&&!seen.has(j)){seen.add(j);nx.push(j)}})});fr=nx}
  base=Array.from(seen).map(function(i){return nodes[i]});
 }
 return base}
function applyFilters(reheat){
 vis=computeVisible();
 var on=new Set(vis.map(function(n){return n.i}));
 links=[];D.edges.forEach(function(e){if(on.has(e[0])&&on.has(e[1]))links.push({source:nodes[e[0]],target:nodes[e[1]]})});
 nodes.forEach(function(n){n.vis=on.has(n.i)});
 sim.nodes(vis);linkF.links(links);
 if(reheat!==false){$("stats").dataset.settled="";sim.alpha(.5).restart();
  if(S.local&&S.sel!=null){for(var w=0;w<60;w++)sim.tick();fit()}}
 stats();buildLegend();req()}
function stats(){$("stats").textContent=vis.length+" nodes, "+links.length+" links"+($("stats").dataset.settled?" (settled)":" (simulating)")}

function buildLegend(){
 var el=$("legend");el.textContent="";
 var cnt={};nodes.forEach(function(n){if(!n.g)cnt[n.f]=(cnt[n.f]||0)+1});
 D.folders.forEach(function(f){
  var it=document.createElement("div");it.className="it"+(S.off.has(f)?" off":"");
  var sw=document.createElement("span");sw.className="sw";sw.style.background=folderColor[f];
  var nm=document.createElement("span");nm.textContent=f;
  var ct=document.createElement("span");ct.className="ct";ct.textContent=cnt[f]||0;
  it.appendChild(sw);it.appendChild(nm);it.appendChild(ct);
  it.addEventListener("click",function(){if(S.off.has(f))S.off.delete(f);else S.off.add(f);applyFilters()});
  el.appendChild(it)})}

function neighborhood(i){var s=new Set(nodes[i].nb);s.add(i);return s}
function draw(){
 dirty=false;
 ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle="#1e1e1e";ctx.fillRect(0,0,W,H);
 ctx.save();ctx.translate(T.x,T.y);ctx.scale(T.k,T.k);
 var focus=S.hover!=null?S.hover:S.sel,fs=focus!=null?neighborhood(focus):null,k=T.k;
 var x0=-T.x/k,y0=-T.y/k,x1=(W-T.x)/k,y1=(H-T.y)/k;
 ctx.lineWidth=1/k;
 ctx.strokeStyle=fs?"rgba(150,150,160,.07)":"rgba(150,150,160,.30)";ctx.beginPath();
 for(var a=0;a<links.length;a++){var l=links[a];if(fs&&(l.source.i===focus||l.target.i===focus))continue;ctx.moveTo(l.source.x,l.source.y);ctx.lineTo(l.target.x,l.target.y)}
 ctx.stroke();
 if(fs){ctx.strokeStyle="rgba(167,139,250,.9)";ctx.beginPath();
  for(var b=0;b<links.length;b++){var m=links[b];if(m.source.i===focus||m.target.i===focus){ctx.moveTo(m.source.x,m.source.y);ctx.lineTo(m.target.x,m.target.y)}}ctx.stroke()}
 for(var v=0;v<vis.length;v++){var n=vis[v];
  ctx.globalAlpha=fs&&!fs.has(n.i)?.1:(n.g?.5:1);
  ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,6.2832);ctx.fillStyle=colorOf(n);ctx.fill();
  if(n.i===S.sel){ctx.lineWidth=2/k;ctx.strokeStyle="#fff";ctx.stroke();ctx.lineWidth=1/k}}
 ctx.globalAlpha=1;
 if(S.label||fs){
  ctx.font=(11/k)+"px -apple-system,Segoe UI,Roboto,sans-serif";ctx.textAlign="center";ctx.textBaseline="top";
  for(var q=0;q<vis.length;q++){var t=vis[q];
   if(t.x<x0||t.x>x1||t.y<y0||t.y>y1)continue;
   var inFocus=fs&&fs.has(t.i);
   if(fs&&!inFocus)continue;
   if(!inFocus&&!(S.label&&k*Math.sqrt(t.inn.length+(t.g?0:1))>2.2))continue;
   var s=t.t.length>30?t.t.slice(0,29)+"...":t.t;
   ctx.fillStyle=t.i===focus?"#fff":"rgba(220,221,222,.9)";ctx.fillText(s,t.x,t.y+t.r+2/k)}}
 ctx.restore()}

function pick(px,py){var p=T.invert([px,py]);return sim.find(p[0],p[1],12/T.k)||null}
var zoom=d3.zoom().scaleExtent([.03,8]).on("zoom",function(e){T=e.transform;req()});
var dragged=false,sx=0,sy=0;
var drag=d3.drag().container(canvas).clickDistance(4)
 .subject(function(e){var p=T.invert([e.x,e.y]);return sim.find(p[0],p[1],14/T.k)})
 .on("start",function(e){if(!e.active)sim.alphaTarget(.25).restart();e.subject.fx=e.subject.x;e.subject.fy=e.subject.y;dragged=false;sx=e.x;sy=e.y})
 .on("drag",function(e){var p=T.invert([e.x,e.y]);e.subject.fx=p[0];e.subject.fy=p[1];if(Math.abs(e.x-sx)+Math.abs(e.y-sy)>4)dragged=true})
 .on("end",function(e){if(!e.active)sim.alphaTarget(0);e.subject.fx=null;e.subject.fy=null;if(!dragged)select(e.subject.i,false)});
d3.select(canvas).call(drag).call(zoom).on("dblclick.zoom",null);
canvas.addEventListener("click",function(ev){if(!pick(ev.offsetX,ev.offsetY))select(null)});
var tip=$("tip");
canvas.addEventListener("mousemove",function(ev){
 var n=pick(ev.offsetX,ev.offsetY),id=n?n.i:null;
 if(id!==S.hover){S.hover=id;canvas.style.cursor=n?"pointer":"grab";req()}
 if(n){tip.textContent=n.t;tip.style.display="block";tip.style.left=(ev.clientX+12)+"px";tip.style.top=(ev.clientY+12)+"px"}else tip.style.display="none"});
canvas.addEventListener("mouseleave",function(){S.hover=null;tip.style.display="none";req()});

function centerOn(i){
 var n=nodes[i];if(n.x==null)return;
 var pw=($("panel").classList.contains("open")&&W>700)?350:0,k=Math.max(T.k,1.4);
 d3.select(canvas).transition().duration(450).call(zoom.transform,d3.zoomIdentity.translate((W-pw)/2,H/2).scale(k).translate(-n.x,-n.y))}
function addLi(ul,i){
 var li=document.createElement("li"),b=document.createElement("button"),s=document.createElement("small");
 b.type="button";b.appendChild(document.createTextNode(nodes[i].t));s.textContent=nodes[i].g?"unresolved":nodes[i].p;
 b.appendChild(s);b.addEventListener("click",function(){focusNode(i)});li.appendChild(b);ul.appendChild(li)}
function fillPanel(i){
 var n=nodes[i];
 $("pTitle").textContent=n.t;$("pPath").textContent=n.g?"unresolved link target: "+n.p:n.p;
 $("pSnip").textContent=n.s;
 var o=$("pOut"),b=$("pIn");o.textContent="";b.textContent="";
 var os=n.out.slice().sort(function(a,c){return nodes[a].t.localeCompare(nodes[c].t)});
 var bs=n.inn.slice().sort(function(a,c){return nodes[a].t.localeCompare(nodes[c].t)});
 os.forEach(function(j){addLi(o,j)});bs.forEach(function(j){addLi(b,j)});
 $("pOutN").textContent="("+os.length+")";$("pInN").textContent="("+bs.length+")";
 $("panel").classList.add("open")}
function select(i,center){
 var had=S.sel;S.sel=i;
 if(i==null){$("panel").classList.remove("open")}else fillPanel(i);
 if(S.local&&had!==i)applyFilters();
 if(i!=null&&center)centerOn(i);
 req()}
function syncUI(){$("tOrph").checked=S.orph;$("tGhost").checked=S.ghost}
function focusNode(i){
 var n=nodes[i],ch=false;
 if(n.g&&!S.ghost){S.ghost=true;ch=true}
 if(!n.g&&S.off.has(n.f)){S.off.delete(n.f);ch=true}
 if(!n.g&&n.deg===0&&!S.orph){S.orph=true;ch=true}
 if(ch)syncUI();
 S.sel=i;
 if(ch||!n.vis||S.local)applyFilters(true);
 fillPanel(i);centerOn(i);req()}

// search
function fuzzy(q,n){
 var s=(n.t+" "+n.p).toLowerCase(),at=s.indexOf(q);
 if(at>=0)return 1000-at-(n.t.toLowerCase().indexOf(q)===0?0:50)-s.length/100;
 var j=0,gap=0,last=-1;
 for(var c=0;c<s.length&&j<q.length;c++){if(s[c]===q[j]){if(last>=0)gap+=c-last-1;last=c;j++}}
 return j===q.length?500-gap-s.length/100:null}
var hits=[];
function doSearch(){
 var q=$("q").value.trim().toLowerCase(),r=$("results");r.textContent="";hits=[];
 if(!q)return;
 var sc=[];nodes.forEach(function(n){var s=fuzzy(q,n);if(s!=null)sc.push([s,n.i])});
 sc.sort(function(a,b){return b[0]-a[0]});hits=sc.slice(0,8).map(function(x){return x[1]});
 hits.forEach(function(i,ix){
  var b=document.createElement("button"),s=document.createElement("small");b.type="button";
  b.appendChild(document.createTextNode(nodes[i].t));s.textContent=nodes[i].g?"unresolved":nodes[i].p;b.appendChild(s);
  if(ix===0)b.className="on";b.addEventListener("click",function(){focusNode(i);r.textContent="";if(W<=700)$("controls").classList.remove("open")});r.appendChild(b)})}
$("q").addEventListener("input",doSearch);
$("q").addEventListener("keydown",function(e){
 if(e.key==="Enter"&&hits.length){focusNode(hits[0]);$("results").textContent=""}
 else if(e.key==="Escape"){$("q").value="";doSearch()}});

// controls
$("menuBtn").addEventListener("click",function(){$("controls").classList.toggle("open")});
if(window.innerWidth>700)$("controls").classList.add("open");
$("close").addEventListener("click",function(){select(null)});
$("tOrph").addEventListener("change",function(){S.orph=this.checked;applyFilters()});
$("tGhost").addEventListener("change",function(){S.ghost=this.checked;applyFilters()});
$("tLabel").addEventListener("change",function(){S.label=this.checked;req()});
$("tLocal").addEventListener("change",function(){S.local=this.checked;applyFilters()});
$("depth").addEventListener("change",function(){S.depth=+this.value;if(S.local)applyFilters()});
function setForces(){linkF.distance(+$("sDist").value);charge.strength(-(+$("sRep").value));$("vDist").textContent=$("sDist").value;$("vRep").textContent=$("sRep").value}
function onSlide(){setForces();$("stats").dataset.settled="";sim.alpha(.4).restart()}
$("sDist").addEventListener("input",onSlide);$("sRep").addEventListener("input",onSlide);
$("reheat").addEventListener("click",function(){$("stats").dataset.settled="";sim.alpha(.8).restart()});
function fit(){
 if(!vis.length)return;
 var xs=vis.map(function(n){return n.x}),ys=vis.map(function(n){return n.y});
 var a=d3.min(xs),b=d3.max(xs),c=d3.min(ys),d=d3.max(ys),pw=W>700&&$("panel").classList.contains("open")?350:0;
 var k=Math.min(.95*(W-pw)/Math.max(b-a,50),.95*H/Math.max(d-c,50),4);
 d3.select(canvas).call(zoom.transform,d3.zoomIdentity.translate((W-pw)/2,H/2).scale(k).translate(-(a+b)/2,-(c+d)/2))}
$("fit").addEventListener("click",fit);
window.addEventListener("keydown",function(e){if(e.key==="Escape"&&document.activeElement!==$("q"))select(null)});

setForces();resize();
applyFilters(false);
for(var w=0;w<120;w++)sim.tick();
fit();sim.alpha(.3).restart();
window.__graph={nodes:nodes,sim:sim,state:S,select:focusNode,fit:fit,visible:function(){return vis.length},count:function(){return N}};
})();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
