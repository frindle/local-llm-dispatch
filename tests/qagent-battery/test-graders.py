#!/usr/bin/env python3
"""Synthetic good/bad answers for each battery grader, against a FAKE $HOME (QAGENT_BATTERY_HOME) so the
fixtures are fully controlled. Also exercises grade-battery.py end-to-end with a stub qagent. No model, no queue."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
G = HERE / "graders"


class T(unittest.TestCase):
    def setUp(self):
        self.h = Path(tempfile.mkdtemp()); b = self.h / "bin"; (b / "ollama-queue-logs").mkdir(parents=True)
        jobs = [{"status": "failed", "failure_class": c} for c in ["model"] * 5 + ["context"] * 2 + ["operator"]] + \
               [{"status": "done", "failure_class": "model"}] * 9
        (b / "ollama-queue-state.json").write_text(json.dumps({"jobs": jobs}))
        (b / "ollama-worker.py").write_text("x=1\ndef clip_tool_stream(t):\n    pass\n")
        (b / "ollama-worker.py.bak-1").write_text("def clip_tool_stream(t):\n    pass\n")
        (b / "other.py").write_text("def clip_tool_stream_x(t): pass\n")
        (b / "queue-abort").write_text('"""\n2 = refused\nENV   QUEUE_ABORT_QUEUE_PY  x\n      QUEUE_ABORT_POLL  y\n"""\n'
                                      'ap.add_argument("--wait", type=float, default=90.0)\n')
        (b / "ollama-queue.py").write_text("".join(f'x = sub.add_parser("cmd{i}")\n' for i in range(20)))
        for i, sz in enumerate([50, 40, 30, 20, 10, 5, 4, 3, 2, 1]):
            (b / "ollama-queue-logs" / f"l{i}.log").write_text("x" * sz)
        (b / "test-qagent.py").write_text(
            "class T:\n    def test_bounds_always_set_and_no_sampling(self):\n"
            '        self.assertEqual(a[a.index("--max-tokens") + 1], "12000")\n'
            '        self.assertEqual(a[a.index("--max-iters") + 1], "12")\n'
            '        self.assertEqual(a[a.index("--chat-timeout") + 1], "900")\n'
            '        for f in ("--temperature", "--top-p"):\n            self.assertNotIn(f, a)\n'
            '        self.q("run", "--max-tokens", "500", "x"); a = self.argv()\n'
            '        self.assertEqual(a[a.index("--max-tokens") + 1], "500")\n\n    def test_next(self):\n        pass\n')

    def grade(self, g, answer):
        r = subprocess.run([sys.executable, str(G / g)], input=answer, capture_output=True, text=True,
                           env=dict(os.environ, QAGENT_BATTERY_HOME=str(self.h)))
        return r.returncode, r.stdout.strip()

    def check(self, g, good, bads):
        rc, out = self.grade(g, "blah blah\n" + good + "\n"); self.assertEqual(rc, 0, out)
        rc, out = self.grade(g, "ANSWER: " + good.split("ANSWER:")[1].strip().replace("`", "")); self.assertEqual(rc, 0, out)
        for b in bads:
            rc, out = self.grade(g, b); self.assertEqual(rc, 1, f"{g} accepted bad answer {b!r}: {out}")
        rc, out = self.grade(g, "prose only, no final line"); self.assertEqual(rc, 1); self.assertIn("ANSWER", out)

    def test_g1(self):
        self.check("g1-failed-by-class.py", "ANSWER: model=5; context=2; operator=1; nonconvergence_progressing=0",
                   ["ANSWER: model=14; context=2; operator=1; nonconvergence_progressing=0",   # counted done jobs too
                    "ANSWER: model=5; context=2; operator=1",                                    # class missing
                    "ANSWER: model=0; context=0; operator=0; nonconvergence_progressing=0"])
        rc, _ = self.grade("g1-failed-by-class.py", "ANSWER: model=6; context=2; operator=1; nonconvergence_progressing=0")
        self.assertEqual(rc, 0)   # +/-1 drift tolerated

    def test_g2(self):
        self.check("g2-function-home.py", "ANSWER: ollama-worker.py",
                   ["ANSWER: ollama-worker.py,ollama-worker.py.bak-1", "ANSWER: other.py", "ANSWER: ollama-queue.py"])

    def test_g3(self):
        self.check("g3-small-file-facts.py", "ANSWER: wait=90; env=QUEUE_ABORT_QUEUE_PY,QUEUE_ABORT_POLL; refused_exit=2",
                   ["ANSWER: wait=30; env=QUEUE_ABORT_QUEUE_PY,QUEUE_ABORT_POLL; refused_exit=2",
                    "ANSWER: wait=90; env=QUEUE_ABORT_QUEUE_PY; refused_exit=2",
                    "ANSWER: wait=90; env=QUEUE_ABORT_QUEUE_PY,QUEUE_ABORT_POLL; refused_exit=1"])

    def test_g4(self):
        full = ",".join(f"cmd{i}" for i in range(20))
        self.check("g4-cli-subcommands.py", "ANSWER: " + full,
                   ["ANSWER: " + ",".join(f"cmd{i}" for i in range(10)),                       # truncated-list answer
                    "ANSWER: " + full + ",fake1,fake2,fake3"])
        rc, _ = self.grade("g4-cli-subcommands.py", "ANSWER: " + ",".join(f"cmd{i}" for i in range(19)) + ",bogus")
        self.assertEqual(rc, 0)   # 95% recall, 1 invented -> within tolerance

    def test_g5(self):
        self.check("g5-largest-logs.py", "ANSWER: l0.log,l1.log,l2.log,l3.log,l4.log",
                   ["ANSWER: l5.log,l6.log,l7.log,l8.log,l9.log", "ANSWER: l0.log,l1.log,l2.log,l3.log,l9.log",
                    "ANSWER: l0.log,l1.log,l2.log"])
        rc, _ = self.grade("g5-largest-logs.py", "ANSWER: l0.log,l1.log,l2.log,l3.log,l5.log")
        self.assertEqual(rc, 0)

    def test_g6(self):
        self.check("g6-test-asserts.py", "ANSWER: max_tokens=12000; max_iters=12; chat_timeout=900; absent=--temperature,--top-p",
                   ["ANSWER: max_tokens=500; max_iters=12; chat_timeout=900; absent=--temperature,--top-p",   # override trap
                    "ANSWER: max_tokens=12000; max_iters=12; chat_timeout=900; absent=--temperature",
                    "ANSWER: max_tokens=12000; max_iters=30; chat_timeout=900; absent=--temperature,--top-p"])

    def test_grade_battery_end_to_end(self):
        stub = self.h / "qagent"
        stub.write_text('#!/bin/sh\ncase "$2" in\n'
                        ' good*) printf -- "banner\\njob x\\n\\n--- final answer ---\\nthinking\\nANSWER: ollama-worker.py\\n\\n--- diff: none ---\\nworker log: l\\n";;\n'
                        ' bad*) printf -- "--- final answer ---\\nANSWER: other.py\\n\\nworker log: l\\n";;\n'
                        ' pend*) echo "(job not finished yet -- use qagent wait)";;\nesac\n')
        stub.chmod(0o755)
        env = dict(os.environ, QAGENT=str(stub), QAGENT_BATTERY_HOME=str(self.h), QAGENT_STATE=str(self.h / "bin/ollama-queue-state.json"))
        run = lambda *a: subprocess.run([sys.executable, str(HERE / "grade-battery.py"), *a], capture_output=True, text=True, env=env)
        r = run("g2=good1", "g1=pend1")
        self.assertEqual(r.returncode, 1); self.assertRegex(r.stdout, r"g2\s+function-home\s+good1\s+\S+\s+PASS")
        self.assertRegex(r.stdout, r"g1\s+failed-by-class\s+pend1\s+\S+\s+PENDING")
        r = run("g2=bad1"); self.assertRegex(r.stdout, r"g2.*FAIL"); self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
