"""Integrity tests for the lmspeed gate. Needs numpy (torch for the baseline
tests only). Run where the gate runs, e.g. on the node from the venv:

    python -m unittest discover tests
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import gate as g  # noqa: E402

try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except ImportError:
    HAVE_TORCH = False

# A numpy-only submission: train counts byte frequencies, model predicts them.
COUNT_TRAIN = """
import argparse, os, time, numpy as np
ap = argparse.ArgumentParser()
for k in ("--data", "--out", "--seed", "--deadline"): ap.add_argument(k)
a = ap.parse_args()
d = np.fromfile(os.path.join(a.data, "enwik8.train"), dtype=np.uint8)
c = np.bincount(d, minlength=256).astype(np.float64) + 1
np.save(os.path.join(a.out, "counts.tmp.npy"), c)
os.replace(os.path.join(a.out, "counts.tmp.npy"), os.path.join(a.out, "counts.npy"))
"""
COUNT_MODEL = """
import os, numpy as np
class P:
    def __init__(self, lp): self.lp = lp
    def reset(self, b): self.b = b
    def step(self, x): return np.tile(self.lp, (len(x), 1))
def load(ckpt):
    c = np.load(os.path.join(ckpt, "counts.npy"))
    return P(np.log(c / c.sum()))
"""
UNIFORM_MODEL = """
import numpy as np
class P:
    def reset(self, b): pass
    def step(self, x): return np.zeros((len(x), 256))
def load(ckpt): return P()
"""
# Predicts that the next byte repeats the one just fed: exact on constant text.
REPEAT_MODEL = """
import numpy as np
class P:
    def reset(self, b): pass
    def step(self, x):
        x = np.asarray(x)
        out = np.full((len(x), 256), -1e9)
        out[np.arange(len(x)), x] = 0.0
        return out
def load(ckpt): return P()
"""
NOOP_TRAIN = """
import argparse, os
ap = argparse.ArgumentParser()
for k in ("--data", "--out", "--seed", "--deadline"): ap.add_argument(k)
a = ap.parse_args()
open(os.path.join(a.out, "done"), "w").write("1")
"""
# Ignores the deadline: keeps stamping the time into its checkpoint, and
# after the deadline swaps model.py for one that would score far better.
OVERRUN_TRAIN = """
import argparse, os, time
ap = argparse.ArgumentParser()
for k in ("--data", "--out", "--seed", "--deadline"): ap.add_argument(k)
a = ap.parse_args()
here = os.path.dirname(os.path.abspath(__file__))
swapped = False
while True:
    tmp = os.path.join(a.out, "stamp.tmp")
    open(tmp, "w").write(repr(time.time()))
    os.replace(tmp, os.path.join(a.out, "stamp"))
    if time.time() > float(a.deadline) + 0.3 and not swapped:
        open(os.path.join(here, "model.py"), "w").write(open(os.path.join(here, "better.py")).read())
        swapped = True
    time.sleep(0.05)
"""


def make_data(root: Path, n: int = 400_000, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    words = [bytes(rng.integers(97, 123, rng.integers(2, 8)).astype(np.uint8)) for _ in range(300)]
    text = b" ".join(words[i] for i in rng.integers(0, len(words), n // 4))[:n]
    pub = root / "public"
    pub.mkdir(parents=True)
    (pub / "enwik8.train").write_bytes(text)
    (pub / "enwik8.valid").write_bytes(text[::-1])
    (root / "heldout").write_bytes(text[n // 2:] + text[:n // 2])
    (root / "secret").write_bytes(b"s" * 32)
    return pub


class Sub:
    def __init__(self, train: str, model: str, extra: dict[str, str] | None = None):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "sub"
        self.path.mkdir()
        (self.path / "train.py").write_text(train)
        (self.path / "model.py").write_text(model)
        for k, v in (extra or {}).items():
            (self.path / k).write_text(v)
        self.data = make_data(Path(self.dir.name) / "data")

    def gate(self, *args: str, expect_rc: int = 0, timeout: float = 180) -> dict:
        cmd = [sys.executable, str(HERE / "gate.py"), str(self.path / "train.py"),
               "--data-dir", str(self.data), "--heldout-file", str(self.data.parent / "heldout"),
               "--episodes", "8", "--window", "64", "--budget-s", "3", "--grace-s", "2", *args]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        assert out.returncode == expect_rc, (out.returncode, out.stdout[-2000:], out.stderr[-3000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def heldout(self, *args: str, **kw) -> dict:
        return self.gate("--split", "heldout", "--secret", str(self.data.parent / "secret"), *args, **kw)

    def __del__(self):
        self.dir.cleanup()


class WindowTests(unittest.TestCase):
    def test_windows_are_disjoint_in_range_and_reproducible(self):
        s = g.window_starts(100_000, 50, b"k", 1024, 64)
        self.assertEqual(s, g.window_starts(100_000, 50, b"k", 1024, 64))
        self.assertEqual(len(set(s)), 50)
        ss = sorted(s)
        self.assertTrue(all(b - a >= 1024 + 1 + 64 for a, b in zip(ss, ss[1:])))  # gap: no stream sees another's text
        self.assertTrue(all(0 <= x and x + 1025 <= 100_000 for x in s))
        self.assertNotEqual(s, g.window_starts(100_000, 50, b"other", 1024, 64))
        with self.assertRaises(ValueError):
            g.window_starts(10_000, 50, b"k", 1024, 64)

    def test_heldout_offsets_give_fresh_windows_and_seeds(self):
        sec = b"x" * 32
        a = g.window_starts(5_000_000, 256, g.heldout_key(sec, 0, 256, b"windows"), 1024, 64)
        b = g.window_starts(5_000_000, 256, g.heldout_key(sec, 256, 256, b"windows"), 1024, 64)
        self.assertLess(len(set(a) & set(b)), 3)  # independently drawn; identical windows are rare
        self.assertNotEqual(g.heldout_seed(sec, 0, 256), g.heldout_seed(sec, 256, 256))
        with self.assertRaises(ValueError):
            g.heldout_key(b"short", 0, 1, b"windows")


class ScoreRowTests(unittest.TestCase):
    def test_uniform_is_eight_bits_and_logits_equal_logprobs(self):
        t = np.array([3, 200])
        b, bad = g.score_rows(np.zeros((2, 256), np.float32), t)
        self.assertTrue(np.allclose(b, 8.0))
        self.assertEqual(bad, 0)
        rng = np.random.default_rng(0)
        logits = rng.normal(size=(2, 256)).astype(np.float32)
        lp = logits - np.log(np.exp(logits.astype(np.float64)).sum(1, keepdims=True))
        self.assertTrue(np.allclose(g.score_rows(logits, t)[0], g.score_rows(lp.astype(np.float32), t)[0], atol=1e-4))

    def test_invalid_rows_cost_uniform_and_zero_probability_is_capped(self):
        rows = np.zeros((3, 256), np.float32)
        rows[0, 5] = np.nan
        rows[1, :] = -np.inf
        rows[2, 7] = -np.inf  # target given zero probability
        b, bad = g.score_rows(rows, np.array([0, 0, 7]))
        self.assertEqual(bad, 2)
        self.assertEqual(list(b[:2]), [8.0, 8.0])
        self.assertEqual(b[2], g.MAX_BITS)


class GateTests(unittest.TestCase):
    def test_counting_model_scores_and_every_byte_is_scored(self):
        r = Sub(COUNT_TRAIN, COUNT_MODEL).gate()
        self.assertEqual(r["ends"], {"ok": 8})
        self.assertEqual(r["mean_steps"], 64)
        self.assertLess(r["mean"], 5.0)  # a-z and space: well under 8 bits
        self.assertTrue(r["lower_is_better"])
        self.assertTrue(r["train"]["ended_before_deadline"])

    def test_prediction_is_scored_against_the_NEXT_byte(self):
        s = Sub(NOOP_TRAIN, REPEAT_MODEL)
        (s.data / "enwik8.valid").write_bytes(b"a" * 50_000)
        self.assertAlmostEqual(s.gate()["mean"], 0.0, places=3)  # repeat-last is exact on constant text
        (s.data / "enwik8.valid").write_bytes(bytes(range(256)) * 200)
        self.assertGreater(s.gate()["mean"], 30)  # and maximally wrong on text that always changes

    def test_training_past_the_deadline_is_killed_and_discarded(self):
        s = Sub(OVERRUN_TRAIN, UNIFORM_MODEL, {"better.py": COUNT_MODEL})
        t0 = time.time()
        r = s.heldout("--budget-s", "2", "--grace-s", "1")
        stamp = float((s.path / ".gate-ckpt" / "stamp").read_text())
        self.assertLess(stamp - t0, 2 + 2.0)  # the checkpoint scored is the one at the deadline
        self.assertTrue(r["train"]["stopped"])
        self.assertFalse(r["train"]["ended_before_deadline"])
        self.assertLess(r["train"]["seconds"], 2 + 1 + 5 + 5)  # killed at deadline + grace (+ timeout -k)
        # model.py was swapped after the deadline; the frozen original was scored
        self.assertIn("np.zeros", (s.path / "model.py").read_text())
        self.assertAlmostEqual(r["mean"], 8.0, places=3)

    def test_no_checkpoint_is_an_error_not_a_score(self):
        r = Sub("import sys\nsys.exit(1)\n", UNIFORM_MODEL).gate()
        self.assertEqual(r["ends"], {"error": 8})
        self.assertEqual(r["mean_steps"], 0)
        self.assertIn("no checkpoint", r["first_error"])

    def test_model_load_failure_is_counted(self):
        r = Sub(NOOP_TRAIN, "raise RuntimeError('boom')\n").gate()
        self.assertEqual(r["ends"], {"error": 8})
        self.assertEqual(r["mean_steps"], 0)
        self.assertIn("boom", r["first_error"])

    def test_slow_model_times_out_and_unscored_bytes_cost_uniform(self):
        slow = UNIFORM_MODEL.replace("def step(self, x): return", "def step(self, x): __import__('time').sleep(0.2); return")
        r = Sub(NOOP_TRAIN, slow).gate("--eval-s", "2")
        self.assertEqual(r["ends"], {"timeout": 8})
        self.assertLess(r["mean_steps"], 64)
        self.assertAlmostEqual(r["mean"], 8.0, places=3)

    def test_wrong_shape_is_an_error(self):
        bad = UNIFORM_MODEL.replace("np.zeros((len(x), 256))", "np.zeros((len(x), 255))")
        r = Sub(NOOP_TRAIN, bad).gate()
        self.assertEqual(r["ends"], {"error": 8})
        self.assertIn("shape", r["first_error"])

    def test_prints_do_not_corrupt_protocol(self):
        chatty = UNIFORM_MODEL.replace("def step(self, x): return", "def step(self, x): print('chatty'); return")
        self.assertEqual(Sub(NOOP_TRAIN, chatty).gate()["ends"], {"ok": 8})

    def test_heldout_hides_positions_and_offsets_change_windows(self):
        s = Sub(COUNT_TRAIN, COUNT_MODEL)
        a = s.heldout("--seed-offset", "0", "--json", str(s.path.parent / "a.json"))
        b = s.heldout("--seed-offset", "8")
        rows = json.loads((s.path.parent / "a.json").read_text())["results"]
        self.assertEqual({r["window"] for r in rows}, {-1})
        self.assertEqual(a["seed_range"], [0, 8])
        self.assertNotEqual(a["seed"], b["seed"])
        self.assertNotEqual(a["mean"], b["mean"])

    def test_heldout_refuses_a_ready_checkpoint(self):
        s = Sub(NOOP_TRAIN, UNIFORM_MODEL)
        out = subprocess.run([sys.executable, str(HERE / "gate.py"), str(s.path / "train.py"), "--split", "heldout",
                              "--secret", str(s.data.parent / "secret"), "--ckpt", str(s.path)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 2)

    def test_fifo_transport_matches_stdio(self):
        s = Sub(COUNT_TRAIN, COUNT_MODEL)
        s.gate()  # leaves .gate-ckpt
        ck = str(s.path / ".gate-ckpt")
        with tempfile.TemporaryDirectory() as d:
            a = s.gate("--ckpt", ck)
            b = s.gate("--ckpt", ck, "--fifo-dir", d)
            self.assertEqual((a["mean"], a["ends"]), (b["mean"], b["ends"]))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), [])  # fifos cleaned up

    def test_model_that_never_connects_is_infra_not_score(self):
        s = Sub(NOOP_TRAIN, UNIFORM_MODEL)
        with tempfile.TemporaryDirectory() as d:
            r = s.gate("--ckpt", str(s.path), "--fifo-dir", d, "--policy-cmd", "true", "--load-s", "5", expect_rc=3)
        self.assertIn("infra_error", r)


@unittest.skipUnless(HAVE_TORCH, "torch not installed")
class BaselineTests(unittest.TestCase):
    def test_kv_cache_step_matches_full_forward(self):
        import torch
        sys.path.insert(0, str(HERE / "baselines" / "transformer"))
        import model as bm
        torch.manual_seed(0)
        c = bm.Config(ctx=32, d=32, layers=2, heads=2)
        m = bm.ByteLM(c).eval()
        p = bm.Predictor(m)
        x = torch.randint(0, 256, (3, 80))
        p.reset(3)
        steps = torch.stack([p.step(x[:, t]) for t in range(80)], dim=1)
        with torch.no_grad():
            full = torch.log_softmax(m(x[:, :32])[0], -1)
        self.assertTrue(torch.allclose(steps[:, :32], full, atol=1e-4))  # inside the first window: identical
        # after the cache fills, it re-encodes the last ctx/2 bytes: check the step that rebuilds
        with torch.no_grad():
            rebuilt = torch.log_softmax(m(x[:, 32 - 16 + 1:33])[0][:, -1], -1)
        self.assertTrue(torch.allclose(steps[:, 32], rebuilt, atol=1e-4))
        self.assertTrue(torch.isfinite(steps).all())

    def test_baseline_trains_in_a_short_budget_and_beats_uniform(self):
        s = Sub("", "")
        for f in ("train.py", "model.py"):
            (s.path / f).write_text((HERE / "baselines" / "transformer" / f).read_text())
        r = s.gate("--budget-s", "20", "--grace-s", "5", "--window", "128", timeout=300)
        self.assertEqual(r["ends"], {"ok": 8}, r.get("first_error"))
        self.assertLess(r["mean"], 8.0)
        self.assertIn("model.pt", r["train"]["ckpt_files"])


if __name__ == "__main__":
    unittest.main()
