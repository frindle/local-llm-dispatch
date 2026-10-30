#!/usr/bin/env python3
"""Self-test for dispatch_env.py: `python3 dispatch_env_test.py` (run from dispatch-templates)."""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_env import memory_db, FakeClock, StubHttp, StubResponse, env, tmp_cwd


class T(unittest.TestCase):
    def test_memory_db(self):
        db = memory_db("CREATE TABLE p (id INTEGER PRIMARY KEY); CREATE TABLE c (id INTEGER, p INTEGER REFERENCES p(id));",
                       "INSERT INTO p VALUES (1)")
        self.assertEqual(db.execute("SELECT id FROM p").fetchone()["id"], 1)
        with self.assertRaises(Exception):
            db.execute("INSERT INTO c VALUES (1, 99)")        # foreign keys are ON
        self.assertNotEqual(id(memory_db()), id(db))
        with self.assertRaises(Exception):
            memory_db().execute("SELECT * FROM p")            # isolated per call

    def test_migrations_replay_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            for name, sql in (("002_b", "ALTER TABLE a ADD COLUMN y INTEGER DEFAULT 7;"),
                              ("001_a", "CREATE TABLE a (x INTEGER);"), ("notes", None)):
                (Path(d) / name).mkdir()
                if sql:
                    (Path(d) / name / "migration.sql").write_text(sql)
            db = memory_db(migrations_dir=d)
            db.execute("INSERT INTO a (x) VALUES (1)")
            self.assertEqual(db.execute("SELECT y FROM a").fetchone()["y"], 7)
            with self.assertRaises(FileNotFoundError):
                memory_db(migrations_dir=Path(d) / "missing")

    def test_clock(self):
        c = FakeClock(100.0)
        self.assertEqual(c.time(), 100.0)
        c.advance(5); c.sleep(2.5)
        self.assertEqual((c.time(), c.sleeps), (107.5, [2.5]))
        self.assertEqual(FakeClock(0).now().year, 1970)

    def test_stub_http(self):
        h = StubHttp(lambda m, u, **kw: (201, {"u": u}) if m == "POST" else {"ok": 1})
        r = h.post("https://a/b", json={"x": 1}, headers={"H": "v"})
        self.assertEqual((r.status_code, r.ok, r.json()), (201, True, {"u": "https://a/b"}))
        self.assertEqual(h.get("https://a/c").json(), {"ok": 1})
        self.assertEqual([c["method"] for c in h.calls], ["POST", "GET"])
        self.assertEqual(h.calls[0]["json"], {"x": 1})
        bad = StubHttp(lambda m, u, **kw: StubResponse(500, "boom", {"X-A": "1"})).get("https://z")
        self.assertEqual((bad.ok, bad.text, bad.headers["x-a"]), (False, "boom", "1"))
        with self.assertRaises(RuntimeError):
            bad.raise_for_status()

    def test_env_and_tmp_cwd(self):
        os.environ["DE_A"] = "keep"; os.environ.pop("DE_B", None)
        with env(DE_A=None, DE_B="x"):
            self.assertNotIn("DE_A", os.environ); self.assertEqual(os.environ["DE_B"], "x")
        self.assertEqual(os.environ["DE_A"], "keep"); self.assertNotIn("DE_B", os.environ)
        before = os.getcwd()
        with tmp_cwd() as d:
            self.assertEqual(Path(os.getcwd()).resolve(), d.resolve())
        self.assertEqual(os.getcwd(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
