"""Baseline: n = 2^B - 1 for each size.

Its first B steps of the shortcut map T(n) = (3n+1)/2 are all odd, so the
value grows to about 3^B before the orbit turns into an ordinary random-looking
descent: about 2B steps up front, then a long tail from a big number.

  python all_odd.py [--out records.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collatz import SIZES  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--out", type=Path, default=Path("records.json"))
a = ap.parse_args()
a.out.write_text(json.dumps({str(B): str((1 << B) - 1) for B in SIZES}, indent=1) + "\n")
print(f"wrote {a.out}")
