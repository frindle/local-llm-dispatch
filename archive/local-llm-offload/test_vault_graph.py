#!/usr/bin/env python3
"""Tests for vault-graph.py. Run: python3 -m unittest test_vault_graph -v"""
import importlib.util
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("vg", os.path.join(HERE, "vault-graph.py"))
vg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vg)

FAKE_D3 = "// https://d3js.org v7.9.0\n" + "/*pad*/" * 30000 + \
    "forceSimulation forceManyBody zoomIdentity\n"


def targets(text):
    return [(t, k, r) for t, k, r in vg.parse_links(text)]


class Parser(unittest.TestCase):
    def test_alias_heading_folder(self):
        t = [x[0] for x in vg.parse_links("[[A|alias]] [[B#Head]] [[Reference/C]] [[D#h|al]] [[E^blk]]")]
        self.assertEqual(t, ["A", "B", "Reference/C", "D", "E"])

    def test_self_heading_and_embed_dropped(self):
        self.assertEqual(vg.parse_links("[[#local]] ![[pic.png]] [[doc.pdf]]"), [])

    def test_code_fence_ignored(self):
        txt = "[[Real]]\n```\n[[InFence]]\n```\n~~~\n[[Tilde]]\n~~~\nand `[[Inline]]` ok [[After]]"
        self.assertEqual([x[0] for x in vg.parse_links(txt)], ["Real", "After"])

    def test_unclosed_fence_swallows_rest(self):
        self.assertEqual([x[0] for x in vg.parse_links("[[A]]\n```\n[[B]]")], ["A"])

    def test_md_links(self):
        t = vg.parse_links("[x](other.md) [y](sub/Note%20Two.md#h) [z](https://e.com/a.md) ![i](a.md) [w](p.png) [v](#h)")
        self.assertEqual([(a, k) for a, k, _ in t], [("other.md", "md"), ("sub/Note Two.md", "md")])

    def test_bm_relations(self):
        t = vg.parse_links("- implements [[Spec]]\n- relates_to [[Other|x]]\ntext [[Plain]]")
        self.assertEqual([(a, r) for a, _, r in t], [("Spec", "implements"), ("Other", "relates_to"), ("Plain", None)])

    def test_table_escaped_pipe(self):
        self.assertEqual(vg.parse_links(r"| [[Note\|alias]] |")[0][0], "Note")


class Resolver(unittest.TestCase):
    def setUp(self):
        self.r = vg.Resolver(["Projects/My Note.md", "Reference/my-note.md", "Claude/Deep/Other_Thing.md",
                              "Top.md", "Reference/Top.md"])

    def test_exact_path(self):
        self.assertEqual(self.r.resolve("Projects/My Note"), "Projects/My Note.md")
        self.assertEqual(self.r.resolve("Projects/My Note.md"), "Projects/My Note.md")

    def test_case_space_hyphen_underscore(self):
        self.assertEqual(self.r.resolve("other thing"), "Claude/Deep/Other_Thing.md")
        self.assertEqual(self.r.resolve("OTHER-THING"), "Claude/Deep/Other_Thing.md")

    def test_folder_suffix(self):
        self.assertEqual(self.r.resolve("Deep/Other Thing"), "Claude/Deep/Other_Thing.md")
        self.assertEqual(self.r.resolve("reference/my note"), "Reference/my-note.md")

    def test_ambiguous_prefers_same_dir_then_shallow(self):
        self.assertEqual(self.r.resolve("my note", "Reference/x.md"), "Reference/my-note.md")
        self.assertEqual(self.r.resolve("Top", "Elsewhere/a.md"), "Top.md")

    def test_md_relative(self):
        self.assertEqual(self.r.resolve("../Projects/My Note.md", "Reference/x.md", "md"), "Projects/My Note.md")

    def test_ghost(self):
        self.assertIsNone(self.r.resolve("Nope"))
        self.assertIsNone(self.r.resolve("Wrong/Folder/Top Nope"))


def fixture(root, extra=None):
    files = {
        "Home.md": "# Home\nSee [[Alpha]] and [[Ghost Page]] and [b](Projects/Beta.md)\n```\n[[Hidden]]\n```\n",
        "Projects/Alpha.md": "---\ntitle: Alpha Title\n---\nbody [[home|back]] [[Projects/Beta#h]]\n- relates_to [[Gamma]]\n",
        "Projects/Beta.md": "# Beta\nlinks [[alpha]]",
        "Reference/Gamma.md": "# Gamma\nplain",
        "Reference/Lonely.md": "# Lonely\nno links",
        "Claude/SessionIndex/s1.md": "# S\n[[Home]]",
        "Claude/Pending-Import/p1.md": "# P\n[[Home]]",
        "Attachments/pic.png": "bin",
    }
    files.update(extra or {})
    for p, t in files.items():
        fp = os.path.join(root, p)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(t)


class Build(unittest.TestCase):
    def test_graph_counts(self):
        with tempfile.TemporaryDirectory() as d:
            fixture(d)
            notes = vg.collect(vg.DirSource(d))
            self.assertEqual(sorted(notes), ["Home.md", "Projects/Alpha.md", "Projects/Beta.md",
                                             "Reference/Gamma.md", "Reference/Lonely.md"])
            g = vg.build_graph(notes)
            self.assertEqual(g["stats"], {"nodes": 5, "edges": 7,"ghosts": 1, "orphans": 1})
            titles = {n["p"]: n["t"] for n in g["nodes"]}
            self.assertEqual(titles["Projects/Alpha.md"], "Alpha Title")
            self.assertNotIn("Hidden", [n["t"] for n in g["nodes"]])

    def test_include_flags(self):
        with tempfile.TemporaryDirectory() as d:
            fixture(d)
            self.assertEqual(len(vg.collect(vg.DirSource(d), include_session=True)), 6)
            self.assertEqual(len(vg.collect(vg.DirSource(d), include_pending=True)), 6)


def make_html(extra=None):
    d = tempfile.mkdtemp()
    fixture(d, extra)
    g = vg.build_graph(vg.collect(vg.DirSource(d)))
    return g, vg.render_html(g, FAKE_D3)


class Html(unittest.TestCase):
    def test_structure_and_data(self):
        g, html = make_html()
        m = re.search(r"window\.__DATA=(.*?);\n</script>", html, re.S)
        data = json.loads(m.group(1))
        self.assertEqual(len(data["nodes"]), 6)  # 5 notes + 1 ghost
        self.assertEqual(len(data["edges"]), 7)
        self.assertEqual(html.count("<script>"), html.count("</script>"))
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html>"))

    def test_no_external_loads(self):
        _, html = make_html()
        self.assertIsNone(re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html, re.I))
        self.assertNotRegex(html, r"@import|url\(\s*[\"']?https?:|fetch\(|XMLHttpRequest|<link\b")
        self.assertNotRegex(html, r"https?://")

    def test_script_breakout_escaped(self):
        evil = "</script><img src=x onerror=alert(1)>"
        _, html = make_html({"Evil.md": "# %s\n%s [[x]]" % (evil, evil)})
        self.assertNotIn("</script><img", html)
        self.assertNotIn("<img src=x", html)
        data = json.loads(re.search(r"window\.__DATA=(.*?);\n</script>", html, re.S).group(1))
        self.assertTrue(any(evil in n["t"] for n in data["nodes"]))  # data round-trips intact

    def test_placeholder_in_note_not_resubstituted(self):
        _, html = make_html({"P.md": "# P\n/*__D3__*/ /*__DATA__*/"})
        self.assertEqual(html.count("forceSimulation forceManyBody"), 1)

    def test_panel_never_uses_innerhtml(self):
        # note text reaches the DOM only via textContent / createTextNode
        _, html = make_html()
        js = html.split("window.__DATA=")[1]
        self.assertNotRegex(js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write")


class Private(unittest.TestCase):
    def test_modes_and_main(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "v")
            fixture(src)
            gd = os.path.join(t, "gd")
            os.makedirs(gd)
            with open(os.path.join(gd, "d3.min.js"), "w") as fh:
                fh.write(FAKE_D3)
            self.assertEqual(vg.main(["--from-dir", src, "--graph-dir", gd]), 0)
            out = os.path.join(gd, "graph.html")
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(gd).st_mode), 0o700)


class FakeVault(BaseHTTPRequestHandler):
    files = {"A.md": "# A\n[[B]]", "d/B.md": "# B", "Claude/SessionIndex/x.md": "[[A]]", "img.png": "x"}
    gets = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeVault.gets.append(self.path)
        if self.headers.get("Authorization") != "tok":
            self.send_response(401); self.end_headers(); return
        p = self.path[len("/vault/"):]
        if p == "" or p.endswith("/"):
            names = set()
            for f in self.files:
                if f.startswith(p):
                    rest = f[len(p):]
                    names.add(rest.split("/")[0] + ("/" if "/" in rest else ""))
            body = json.dumps({"files": sorted(names)}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body); return
        body = self.files[p].encode()
        etag = '"%d"' % hash(body)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304); self.end_headers(); return
        self.send_response(200); self.send_header("ETag", etag); self.end_headers(); self.wfile.write(body)


class Http(unittest.TestCase):
    def test_http_source_cache(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeVault)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d" % srv.server_port
            with tempfile.TemporaryDirectory() as c:
                s = vg.HttpSource(url, "tok", c, skip_dirs=[vg.SKIP_SESSION, vg.SKIP_PENDING])
                notes = vg.collect(s)
                self.assertEqual(sorted(notes), ["A.md", "d/B.md"])
                self.assertEqual((s.fetched, s.cache_hits), (2, 0))
                s2 = vg.HttpSource(url, "tok", c, skip_dirs=[vg.SKIP_SESSION])
                vg.collect(s2)
                self.assertEqual((s2.fetched, s2.cache_hits), (0, 2))
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(c, os.listdir(c)[0])).st_mode), 0o600)
                self.assertFalse(any(g for g in FakeVault.gets if "SessionIndex/x" in g))
                with self.assertRaises(RuntimeError):
                    vg.HttpSource(url, "bad", c)._get("A.md")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
