"""Integrity tests for the hailstone checker, gate and baselines. Run: python3 -m pytest tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from collatz import SIZES, delay, orbit  # noqa: E402
from gate import check  # noqa: E402


def gate_cli(records, *args: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "records.json"
        p.write_text(records if isinstance(records, str) else json.dumps(records))
        out = subprocess.run([sys.executable, str(HERE / "gate.py"), str(p), *args],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])


def baseline(name: str, *args: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "records.json"
        subprocess.run([sys.executable, str(HERE / "baselines" / f"{name}.py"), "--out", str(out), *args],
                       check=True, capture_output=True, timeout=300)
        return json.loads(out.read_text())


ALL_ODD = {str(B): str((1 << B) - 1) for B in SIZES}


class CollatzTests(unittest.TestCase):
    def test_calibration_facts(self):
        # calibration facts, as README.md states them
        self.assertEqual(delay(1), 0)
        self.assertEqual(delay(27), 111)
        self.assertEqual(orbit(27, 1000).peak_bits, (9232).bit_length())
        self.assertEqual(delay(837799), 524)
        self.assertEqual(max(range(1, 10**4), key=delay), 6171)  # the longest delay below 10^4: 261 steps
        self.assertEqual(delay(6171), 261)

    def test_cap_stops_and_says_so(self):
        o = orbit(27, 50)
        self.assertIsNone(o.delay)
        self.assertEqual(o.steps_run, 50)
        with self.assertRaises(ValueError):
            orbit(0, 10)


class GateTests(unittest.TestCase):
    def test_summary_shape_and_exactness(self):
        r = gate_cli(ALL_ODD, "--split", "heldout", "--episodes", "500", "--secret", "x", "--seed-offset", "7",
                     "--policy-cmd", "jail --", "--fifo-dir", "/nowhere", "--workers", "4")
        self.assertEqual((r["n"], r["episodes"], r["std"], r["ends"], r["exact"]), (4, 4, 0.0, {"ok": 4}, True))
        self.assertEqual(r["ci95"], [r["mean"], r["mean"]])
        per_bit = [delay((1 << B) - 1) / B for B in SIZES]
        self.assertAlmostEqual(r["mean"], sum(per_bit) / 4, places=5)
        drop = lambda d: {k: v for k, v in d.items() if k != "records"}  # noqa: E731 (the temp path differs)
        self.assertEqual(drop(gate_cli(ALL_ODD)), drop(r))  # the same answer every time

    def test_ints_and_decimal_strings_agree(self):
        as_ints = {k: int(v) for k, v in ALL_ODD.items()}
        self.assertEqual(check(as_ints)["mean"], check(ALL_ODD)["mean"])

    def test_invalid_entries_are_counted_not_dropped(self):
        bad = dict(ALL_ODD, **{"128": str(1 << 128), "256": "12x", "512": "-5"})
        del bad["1024"]
        r = check(bad)
        self.assertEqual(r["ends"], {"invalid": 4})
        self.assertEqual(r["mean"], 0.0)
        self.assertIn("out of range", r["rows"][0]["why"])
        self.assertEqual(r["rows"][3]["why"], "missing")
        partial = check(dict(ALL_ODD, **{"256": "0"}))
        self.assertEqual(partial["ends"], {"ok": 3, "invalid": 1})
        self.assertAlmostEqual(partial["mean"], (check(ALL_ODD)["mean"] * 4 - delay((1 << 256) - 1) / 256) / 4,
                               places=5)

    def test_unknown_keys_void_the_file(self):
        r = check(dict(ALL_ODD, **{"64": "27"}))
        self.assertEqual((r["mean"], r["ends"]), (0.0, {"invalid": 4}))
        self.assertTrue(any("unknown key" in p for p in r["problems"]))
        self.assertEqual(check([1, 2, 3])["ends"], {"invalid": 4})
        self.assertEqual(check({"128": True, "256": 2.5, "512": None, "1024": [1]})["ends"], {"invalid": 4})

    def test_over_cap_is_held_not_scored(self):
        r = check(ALL_ODD, cap_per_bit=1)  # a test-only cap no real orbit fits under
        self.assertEqual(r["ends"], {"over_cap": 4})
        self.assertEqual(r["mean"], 0.0)
        self.assertIn("held for a human", r["rows"][0]["why"])

    def test_unreadable_files(self):
        self.assertEqual(gate_cli("{not json")["ends"], {"invalid": 4})
        self.assertEqual(gate_cli("x" * (64 * 1024 + 1))["ends"], {"invalid": 4})


class BaselineTests(unittest.TestCase):
    """The scores README.md states for the shipped baselines."""

    def test_random_start(self):
        r = check(baseline("random_start"))
        self.assertEqual(r["ends"], {"ok": 4})
        self.assertAlmostEqual(r["mean"], 8.006592, places=5)

    def test_all_odd(self):
        self.assertAlmostEqual(check(baseline("all_odd"))["mean"], 13.080322, places=5)


if __name__ == "__main__":
    unittest.main()
