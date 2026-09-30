"""Integrity tests for the snake env and gate. Run: python3 -m unittest discover tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from env import DOWN, LEFT, RIGHT, UP, Snake  # noqa: E402


def gate(policy_src: str, *args: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "policy.py"
        p.write_text(policy_src)
        out = subprocess.run([sys.executable, str(HERE / "gate.py"), str(p), *args],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)


class EnvTests(unittest.TestCase):
    def test_same_seed_same_episode(self):
        a, b = Snake(seed=7), Snake(seed=7)
        for m in [RIGHT, DOWN, DOWN, LEFT, UP] * 3:
            self.assertEqual(a.step(m), b.step(m))
            self.assertEqual(a.state(), b.state())

    def test_reversal_is_straight(self):
        s = Snake(seed=0)
        head = s.body[0]
        s.step(LEFT)  # reversing: treated as continuing right
        self.assertEqual(s.body[0], (head[0] + 1, head[1]))

    def test_moving_into_vacating_tail_is_legal(self):
        s = Snake(seed=0)
        s.body = [(5, 5), (5, 6), (4, 6), (4, 5)]  # head adjacent to tail
        s.heading = UP
        s.apple = (0, 0)
        self.assertIsNone(s.step(LEFT))  # into (4,5), which the tail leaves this step

    def test_wall_ends(self):
        s = Snake(seed=0)
        end = None
        while end is None:
            end = s.step(RIGHT)
        self.assertEqual(end, "wall")


class GateTests(unittest.TestCase):
    def test_heldout_seeds_are_not_echoed(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"x" * 32)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.py"
            p.write_text("def act(s): return 1\n")
            j = Path(d) / "r.json"
            subprocess.run([sys.executable, str(HERE / "gate.py"), str(p), "--split", "heldout",
                            "--secret", f.name, "--episodes", "5", "--json", str(j)], check=True,
                           capture_output=True, timeout=60)
            rows = json.loads(j.read_text())["results"]
        self.assertEqual({r["seed"] for r in rows}, {-1})
        Path(f.name).unlink()

    def test_heldout_offsets_give_disjoint_reproducible_batches(self):
        import gate as g
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"s" * 32)
        a = g.heldout_seeds(Path(f.name), 100, 0)
        b = g.heldout_seeds(Path(f.name), 100, 100)
        self.assertEqual(a, g.heldout_seeds(Path(f.name), 100, 0))  # same range, same seeds
        self.assertEqual(a[50:], g.heldout_seeds(Path(f.name), 50, 50))  # ranges compose
        self.assertFalse(set(a) & set(b))  # a fresh range shares no seeds (2^32 space)
        with self.assertRaises(ValueError):
            g.heldout_seeds(Path(f.name), 10, 2**32 - 5)
        Path(f.name).unlink()

    def test_crashing_policy_is_counted_not_dropped(self):
        r = gate("def act(s): raise RuntimeError('boom')\n", "--episodes", "4")
        self.assertEqual(r["episodes"], 4)
        self.assertEqual(r["ends"], {"error": 4})

    def test_hanging_policy_times_out(self):
        r = gate("import time\ndef act(s): time.sleep(5)\n", "--episodes", "2", "--move-timeout", "0.2")
        self.assertEqual(r["ends"].get("timeout"), 2)

    def test_prints_do_not_corrupt_protocol(self):
        r = gate("def act(s):\n    print('chatty')\n    return 1\n", "--episodes", "2")
        self.assertEqual(r["ends"], {"wall": 2})

    def test_fifo_transport_matches_stdio(self):
        with tempfile.TemporaryDirectory() as d:
            src = (HERE / "baselines" / "greedy.py").read_text()
            a = gate(src, "--episodes", "5")
            b = gate(src, "--episodes", "5", "--fifo-dir", d)
            self.assertEqual((a["mean"], a["ends"]), (b["mean"], b["ends"]))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), [])  # fifos cleaned up

    def test_policy_that_never_connects_is_infra_not_score(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.py"
            p.write_text("def act(s): return 1\n")
            out = subprocess.run([sys.executable, str(HERE / "gate.py"), str(p), "--episodes", "3",
                                  "--fifo-dir", d, "--policy-cmd", "true"],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 3)
        self.assertIn("infra_error", json.loads(out.stdout))

    def test_parallel_workers_give_the_same_report(self):
        src = (HERE / "baselines" / "greedy.py").read_text()
        with tempfile.TemporaryDirectory() as d:
            one = gate(src, "--episodes", "12")
            four = gate(src, "--episodes", "12", "--workers", "4")
            fifo4 = gate(src, "--episodes", "12", "--workers", "4", "--fifo-dir", d)
        for r in (four, fifo4):
            self.assertEqual((r["mean"], r["ends"], r["episodes"]), (one["mean"], one["ends"], one["episodes"]))

    def test_bad_load_is_reported(self):
        r = gate("this is not python\n", "--episodes", "3")
        self.assertEqual(r["ends"], {"error": 3})


if __name__ == "__main__":
    unittest.main()
