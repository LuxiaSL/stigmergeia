# Results

Every item here carries a status:

- **Established**: replicated, with receipts anyone holding them can check.
- **Exploratory**: observed, with receipts, but not yet replicated or controlled.
- **Rejected**: tested and not supported; kept so nobody retests it blind.
- **In progress**: a question the harness is being used to answer, with the falsifier stated.

Every number carries its scope: the task, the number and kind of agents, the run length, and the
protocol parts in force. A claim enters this file through an issue with its receipts
(CONTRIBUTING.md): the run manifest, the board database's sha256, and the command that produced
the number.

## Established

Nothing yet.

## Exploratory

Nothing yet.

## Rejected

Nothing yet.

## In progress

- **Does answering the opening round in turn split a herd that simultaneous answers produce?**
  Falsifier: with the same task, agents and canon, runs with answers in turn take no more
  distinct directions in their first quarter than runs with simultaneous answers.
- **Does a swarm beat one agent working alone for the same time?** Falsifier: the best held-out
  score of N agents on one board is not above that of the one-agent control
  (`configs/examples/solo.yaml`) across repeated pairs.
- **Does specialisation emerge without assignment, and does it grow with run length and swarm
  size?** Measured, never configured: who takes which piece in the opening round and keeps it,
  who builds tools the others use, whose code the records derive from.
- **Do mixed model families search more widely than either alone?** Falsifier: a mixed swarm's
  distinct directions and best score are within the range of single-family swarms of the same
  size.

## What is and is not verifiable from this repository

Verifiable here: that the harness does what its docs say. The test suite, the scripted
end-to-end run and the gates in CONTRIBUTING.md check it without a node, a model or a key.

Not verifiable here: any result of a run with real models. Those depend on model versions,
providers and the run's own randomness, and their receipts (board databases, transcripts) contain
agents' words and an operator's paths, so they are not in this tree. An item above moves out of
*In progress* only with receipts attached to its issue.
