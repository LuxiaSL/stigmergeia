# Tasks

What it covers, and what it refuses. A task is a problem with a number: a directory under
`tasks/` holding the brief agents read, the environment, a gate that scores a submission, and
baselines. This page states the contract a task keeps with the harness. It refuses tasks whose
score a gate cannot verify: either on inputs the agents have never seen (a held-out gate), or
exactly, as a property of the submitted object that a checker computes (an exact gate).

## The shape

```
tasks/<name>/
  README.md        the brief, verbatim in every agent's first message: the goal, the rules,
                   how to score locally, the baselines, what counts
  gate.py          scores a submission; the contract below
  env.py, ...      whatever the gate and the agents' code import
  baselines/       working starting points, with their scores in the brief
  canon.toml       optional: the words the canon uses for this task (canon/vocabulary.toml)
  tests/           the task's own suite (run in its own pytest process)
```

A copy of the task, without `tests/`, is placed in every agent's workspace as `task/`; the gate
that scores them uses its own canonical copy on the node.

## The gate's contract

- `python gate.py <submission> --split train --episodes N` scores on public training inputs.
- `--split heldout --secret FILE --seed-offset K --episodes N` scores on held-out inputs derived
  from the secret and indices K..K+N-1. The harness never reuses an index range, so every batch
  is fresh; the gate never echoes seeds or held-out data.
- `--policy-cmd PREFIX` prefixes the command line of the process that runs the submission; that is
  how the submission, and only the submission, is put inside the jail.
- `--workers W` plays episodes in parallel on the agent's cores; `--json OUT` writes the full
  result; the last line of stdout is one JSON summary with at least `mean`, `ci95` and `n`.
- Every episode is counted with its end reason; an error or timeout is a scored episode, never a
  dropped one.

Config says how to judge the numbers: `gate.higher_is_better`, `gate.run_sd` (for gates where one
batch is one noisy training run), and `gate.digest` (what makes two submissions "the same").

## Exact gates

Some scores are not estimates. When the submission is an object (a number, a construction, a
certificate) and the score is a property a checker computes exactly, there is nothing held out
to overfit and no noise to confirm. Such a gate sets `gate.exact: true` and keeps the same
command line (it accepts the held-out flags and ignores them) and the same summary, with `std` 0
and `ci95` [mean, mean]. The lab then counts a result on its first batch, never runs a
confirmation batch, and calls any score strictly better than the record a record. An exact gate
runs none of the agent's code: it reads the submission and computes.

The canon's words for the task (what a submission is called, how scoring works, the side
quests) default to snake's and come from `canon/vocabulary.toml`; a task with different words
ships `canon.toml` beside its gate, overriding any key there.

## The shipped tasks

- **snake** (`tasks/snake/`): a 10x10 snake game with a 1000-step budget; score is apples eaten,
  at most 97. Episodes are milliseconds, so batches of hundreds are cheap and the confirmation
  rule has plenty of evidence.
- **lmspeed** (`tasks/lmspeed/`): train a small byte-level language model on a fixed corpus in a
  fixed 600 s CPU budget; score is held-out bits per byte (lower is better). One held-out
  evaluation is a whole training run, so a record needs a second independent run and noise is
  dominated by training throughput. `prepare_data.sh` builds the corpus under the data root.
- **hailstone** (`tasks/hailstone/`): for each bit size B in {128, 256, 512, 1024}, an n below
  2^B whose Collatz orbit takes as long as possible to reach 1; score is the mean steps per bit.
  An exact gate: the checker follows each orbit with big integers (capped at 100 B steps, since
  the conjecture is verified only below 2^71) in milliseconds.
