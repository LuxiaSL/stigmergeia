"""Baseline: one random n of exactly B bits per size (seeded, so it is reproducible).

  python random_start.py [--seed S] [--out records.json]
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collatz import SIZES  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--out", type=Path, default=Path("records.json"))
a = ap.parse_args()
rng = random.Random(a.seed)
records = {str(B): str(rng.getrandbits(B) | (1 << (B - 1))) for B in SIZES}
a.out.write_text(json.dumps(records, indent=1) + "\n")
print(f"wrote {a.out}")
