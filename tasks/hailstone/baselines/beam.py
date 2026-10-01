"""Baseline: a beam search backward through the inverse Collatz tree.

Every n has the predecessor 2n; n has a second, odd predecessor (n - 1) / 3
exactly when n = 4 (mod 6). Starting from 1 and stepping backward, depth d
holds numbers whose delay is exactly d. The search keeps the `width`
smallest numbers at each depth (small numbers have the most room left under
2^B) and returns, for each size, the smallest member of the deepest depth
that still fits below 2^B.

  python beam.py [--width W] [--out records.json]

Wider beams go deeper, but slowly.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collatz import SIZES  # noqa: E402


def beam(B: int, width: int) -> tuple[int, int]:
    """(depth, n): an n < 2^B with delay(n) == depth, from a width-limited backward search."""
    cap, frontier, depth, best = 1 << B, [1], 0, (0, 1)
    while frontier:
        nxt = set()
        for x in frontier:
            nxt.add(2 * x)
            if x % 6 == 4 and x > 4:
                nxt.add((x - 1) // 3)
        depth += 1
        frontier = sorted(v for v in nxt if v < cap)[:width]
        if frontier:
            best = (depth, frontier[0])
    return best


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--out", type=Path, default=Path("records.json"))
    a = ap.parse_args()
    records = {}
    for B in SIZES:
        t = time.time()
        depth, n = beam(B, a.width)
        records[str(B)] = str(n)
        print(f"B={B}: delay {depth} ({depth / B:.2f} per bit) in {time.time() - t:.1f}s", flush=True)
    a.out.write_text(json.dumps(records, indent=1) + "\n")
    print(f"wrote {a.out}")
