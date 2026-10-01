# Task: hailstone

Find starting numbers whose Collatz orbits take as long as possible to
reach 1, at four sizes far past anything that can be searched by brute
force. Any method is allowed: construction, search, number theory,
hybrids.

## The goal

**The run's result is the highest score the gate has posted when the run
ends.** That is the number this run exists to push up, and it belongs to
everyone here: a score raised by building on someone else's numbers or
code counts exactly as much as one raised alone.

Nobody knows how long an orbit can be at these sizes; finding out is the
task. So there is no "good enough" and no finish line before the run
ends:

- A first working search is a starting point, not a result.
- A plateau is a finding. Post it, with the numbers that show it, and
  then try something different, because a plateau says one approach is
  exhausted, not that the problem is.
- Every improvement to the best score counts, however small, and so does
  a dead end reported clearly enough that nobody repeats it.
- The run is ended from outside, not by you. Until it ends, there is a
  next attempt to make.

## The problem

The Collatz map is C(n) = n / 2 for even n and 3n + 1 for odd n. The
**delay** of n is the number of applications of C until the orbit first
reaches 1 (the delay of 1 is 0). For example, 27 takes 111 steps and
peaks at 9232; 837799 takes 524, the longest below 10^6.

For each bit size **B in {128, 256, 512, 1024}**, find an n with
1 <= n < 2^B whose delay is as long as you can make it.

**Score:** the mean over the four sizes of delay(n_B) / B, "steps per
bit". Higher is better. The checker is exactly `collatz.py` and `gate.py`;
read them rather than trusting this summary.

## Submitting

One JSON file (any name; `records.json` by convention) naming one n per
size, as decimal strings (JSON numbers lose precision past 2^53):

    {"128": "3138...", "256": "...", "512": "...", "1024": "..."}

All four sizes are required. A missing, malformed or out-of-range entry
scores 0 for its size; an unknown key voids the file.

Score yourself (it is the same computation the gate runs):

    python gate.py path/to/records.json

The score is **exact**: a property of the numbers, computed with big
integers, with no held-out set and no noise. The same file always gets the
same score, and a record is any score strictly above the current one.

**The cap.** Every n is verified to reach 1 only below 2^71, so the gate
follows an orbit for at most 100 x B steps. An orbit that hits the cap is
reported as `over_cap`, held for a human to look at, and scores 0 for its
size.

## Baselines

| File | Score | What it does |
|---|---|---|
| `baselines/random_start.py` | 8.01 | a random B-bit n per size |
| `baselines/all_odd.py` | 13.08 | n = 2^B - 1 |

Each writes a `records.json` (`--out` to name it). They are there to be
beaten and to show the file format; neither is a direction. Nobody knows
the ceiling.

## What counts

Only the gate's posted scores count. The gate reads your file and
computes; it runs none of your code. Claims about the conjecture itself,
either way, are not results here: an orbit that hits the cap is a
measurement for a human to check, not a disproof, and a long delay is not
a proof of anything.
