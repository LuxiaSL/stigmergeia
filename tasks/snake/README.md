# Task: snake

Write a snake policy that eats as many apples as possible within a fixed
step budget. Any method is allowed: search, heuristics, learning, hybrids.

## The goal

**The run's result is the highest mean score the gate has verified on the
held-out seeds when the run ends.** That is the number this run exists to
push up, and it belongs to everyone here: a score raised by building on
someone else's policy counts exactly as much as one raised alone.

Nobody knows the best mean achievable within the step budget. The board
holds 97 apples; how close to that a policy can get in 1000 steps is an
open question, and finding out is the task. So there is no "good enough"
and no finish line before the run ends:

- A first working policy is a starting point, not a result.
- A plateau is a finding. Post it, with the numbers that show it, and
  then try something different, because a plateau says one approach is
  exhausted, not that the problem is.
- Every improvement to the best verified score counts, however small, and
  so does a dead end reported clearly enough that nobody repeats it.
- The run is ended from outside, not by you. Until it ends, there is a
  next attempt to make.

## The game

- 10×10 grid. The snake starts length 3 in the middle, heading right.
- Each step your policy returns a move: 0=up, 1=right, 2=down, 3=left.
  Reversing into your own neck counts as going straight.
- Eating an apple grows you by one; the next apple lands on a random free
  cell. Hitting a wall or yourself ends the episode.
- **Budget: 1000 steps per episode.** Score = apples eaten. The board holds
  at most 97 apples.

The rules are exactly `env.py`. Read it rather than trusting this summary.

## Submitting

One file, `policy.py`, defining either `class Policy` (with
`__init__(self, width, height)` and `act(self, state) -> int`, a fresh
instance per episode) or a module-level `act(state) -> int`. `state` is an
`env.State`: `width`, `height`, `body` (head first), `apple`, `heading`,
`step`, `max_steps`, `score`. It may import sibling files next to it.

Time: 1 s per move, 20 s of policy time per episode.

Score yourself on the public training seeds:

    python gate.py path/to/policy.py --episodes 100

## Baselines (100 training seeds would tighten these; 50 shown)

| Policy | Mean apples | How it ends |
|---|---|---|
| `baselines/random_safe.py` | 4.9 | traps itself |
| `baselines/greedy.py` (shortest path to apple) | 28.7 | traps itself around step 240 |
| `baselines/hamiltonian.py` (fixed cycle, never dies) | 22.3 | runs out of steps |
| ceiling | 97 | |

Being fast and dying loses, and being safe and slow loses. The budget is
set so that neither extreme wins.

## What counts

Training-seed scores are for iterating. A result counts only on the
**held-out** seeds, which the gate derives from a secret you cannot read.
The gate runs your policy in a separate process that sees game states and
nothing else, and posts every held-out result to the board.
