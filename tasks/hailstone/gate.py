"""The hailstone gate: check a records file and score it, exactly.

  python gate.py RECORDS_FILE [--json OUT]

The submission names one n per bit size B (see README.md):

    {"128": "<decimal n>", "256": "...", "512": "...", "1024": "..."}

For each B the gate checks 1 <= n < 2^B and follows n under the Collatz map
for at most 100 x B steps. The score is the mean over the four sizes of
delay(n_B) / B (steps per bit); higher is better.

The score is a property of the file, computed exactly: there is no held-out
set and no noise, so the gate runs nothing of the agent's and gives the
same answer every time. The lab's usual flags (--split, --episodes,
--secret, --seed-offset, --policy-cmd, --fifo-dir, --workers) are accepted
and ignored. Each size is one "episode" of the contract, with an end
reason: ok, invalid (missing, not an integer, out of range) or over_cap
(no 1 within the cap: held for a human to look at, scored 0, never a
record). Nothing is dropped.

Output: the last line of stdout is a JSON summary with mean, std (0),
ci95 ([mean, mean]), n, episodes, ends and one row per size.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from collatz import CAP_PER_BIT, SIZES, orbit  # noqa: E402

MAX_FILE_BYTES = 64 * 1024


def parse_value(v: Any) -> int:
    """A decimal string (or a plain JSON integer) as an int; anything else is refused."""
    if isinstance(v, bool):
        raise ValueError("a boolean is not an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s and (s.isdigit() or (s[0] in "+-" and s[1:].isdigit())):
            return int(s)
    raise ValueError(f"not a decimal integer: {str(v)[:40]!r}")


def check(records: Any, cap_per_bit: int = CAP_PER_BIT) -> dict[str, Any]:
    """Score a parsed records object. Pure: no I/O."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    if not isinstance(records, dict):
        records = {}
        problems.append("the file must hold one JSON object mapping each size to an n")
    extra = sorted(set(records) - {str(b) for b in SIZES})
    if extra:
        problems.append(f"unknown key(s) {extra}: the keys are exactly {[str(b) for b in SIZES]}")
    for B in SIZES:
        row: dict[str, Any] = {"B": B, "end": "ok", "per_bit": 0.0}
        try:
            if str(B) not in records:
                raise ValueError("missing")
            n = parse_value(records[str(B)])
            if not 1 <= n < (1 << B):
                raise ValueError(f"out of range: needs 1 <= n < 2^{B}, got {n.bit_length()} bits"
                                 + (" (n < 1)" if n < 1 else ""))
        except ValueError as e:
            row.update(end="invalid", why=str(e))
            rows.append(row)
            continue
        o = orbit(n, cap_per_bit * B)
        row.update(bits=n.bit_length(), peak_bits=o.peak_bits)
        if o.delay is None:
            row.update(end="over_cap", why=f"no 1 within {cap_per_bit * B} steps: held for a human to look at")
        else:
            row.update(delay=o.delay, per_bit=round(o.delay / B, 6))
        rows.append(row)
    mean = round(sum(r["per_bit"] for r in rows) / len(rows), 6)
    if extra:  # an extra key makes the whole file invalid: nothing it claims is scored
        for r in rows:
            r.update(end="invalid", per_bit=0.0, why=r.get("why", "the file has unknown keys"))
        mean = 0.0
    return {"mean": mean, "std": 0.0, "ci95": [mean, mean], "n": len(rows), "episodes": len(rows),
            "ends": dict(Counter(r["end"] for r in rows)), "rows": rows, "problems": problems,
            "exact": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("--json", type=Path, help="write the full report here")
    for flag in ("--split", "--episodes", "--secret", "--seed-offset", "--policy-cmd", "--fifo-dir",
                 "--workers", "--episode-cpu"):
        ap.add_argument(flag, help="accepted for the lab's sake; ignored (the score is exact)")
    a = ap.parse_args()
    try:
        if not a.records.is_file():
            raise ValueError(f"no such file: {a.records}")
        if a.records.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file is over {MAX_FILE_BYTES} bytes")
        records = json.loads(a.records.read_text())
    except (ValueError, OSError) as e:  # an unreadable file scores 0 on every size: counted, not dropped
        report = check({})
        report["problems"].insert(0, f"could not read the records file: {e}")
    else:
        report = check(records)
    report["records"] = str(a.records)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
