#!/usr/bin/env python3
"""Audit Deluge for supersede MISSES — redundant torrents that should have been
marked superseded but are still seeding. Read-only; changes nothing.

Two classes:
  1. pack-over-singles: a season PACK and individual episode singles of the same
     season both seeding (the singles should be superseded by the pack).
  2. duplicate-episode: the same SxxExx seeding 2+ times (a REPACK/better release
     that should have superseded the original).

Deluge web pw from keychain: `security find-generic-password -s deluge-password -w`.
Usage: DPW=$(security find-generic-password -s deluge-password -w) python3 deluge-supersede-scan.py
"""
import os, json, urllib.request, http.cookiejar, re, collections

URL = os.environ.get("DELUGE_URL", "http://10.0.0.2:8112") + "/json"
PW = os.environ.get("DPW") or ""
LABEL_SUBSTR = os.environ.get("LABEL", "sonarr")  # which label to audit

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
def rpc(method, params):
    r = urllib.request.Request(URL, data=json.dumps({"method": method, "params": params, "id": 1}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(op.open(r, timeout=30).read())

rpc("auth.login", [PW])
tor = rpc("core.get_torrents_status", [{}, ["name", "label", "progress", "state"]])["result"]
print("total torrents:", len(tor))

def parse(name):
    m = re.search(r'[. _](S(\d{2}))(E(\d{2}))?[. _]', name, re.I)
    if not m:
        return None
    series = re.sub(r'[._]+', ' ', name[:m.start()]).strip().lower()
    series = re.sub(r'\b(19|20)\d{2}\b', '', series).strip()  # drop year for grouping
    return series, int(m.group(2)), m.group(4)

groups = collections.defaultdict(lambda: {"packs": [], "singles": collections.defaultdict(list)})
for h, t in tor.items():
    if LABEL_SUBSTR not in (t.get("label") or "").lower():
        continue
    if t.get("progress", 0) < 99:
        continue
    p = parse(t["name"])
    if not p:
        continue
    series, season, ep = p
    g = groups[(series, season)]
    (g["packs"] if ep is None else g["singles"][ep]).append((h, t["name"]))

print("\n===== PACK-OVER-SINGLES MISSES =====")
n1 = 0
for (series, season), g in sorted(groups.items()):
    if g["packs"] and g["singles"]:
        n1 += 1
        nsing = sum(len(v) for v in g["singles"].values())
        print(f"* {series.title()} S{season:02d}: {len(g['packs'])} pack(s) + {nsing} single(s)")
        for _, nm in g["packs"]:
            print(f"    PACK  {nm}")
        for ep, items in sorted(g["singles"].items()):
            for _, nm in items:
                print(f"    ep    {nm}")
print(f"  --> {n1} pack-over-singles season(s)")

print("\n===== DUPLICATE-EPISODE MISSES =====")
n2 = 0
for (series, season), g in sorted(groups.items()):
    for ep, items in sorted(g["singles"].items()):
        if len(items) > 1:
            n2 += 1
            print(f"* {series.title()} S{season:02d}E{ep}: {len(items)} copies")
            for _, nm in items:
                print(f"    {nm}")
print(f"  --> {n2} duplicated episode(s)")
