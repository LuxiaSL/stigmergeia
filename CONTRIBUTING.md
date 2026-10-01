# Contributing

A contribution is a pull request against this repository. CI runs the gates described below; a
change merges when they pass and a reviewer agrees it keeps the rules on this page.

This repository holds the tool. What a run of it found (scores, transcripts, the board) belongs to
whoever ran it and stays with them: run data never enters the tree, and nothing here records or
ranks anyone's results.

## What the harness will not do

Some properties of this harness are what make a swarm run worth watching rather than implementation
choices. A change that breaks one makes a different harness, whatever else it improves.

- **Time and budget stay hidden from agents.** Agents who can see a clock or a budget ration it
  and stop early. Nothing on the agent-facing path (the prompts, the canon, tool descriptions,
  tool results, the board pulse, the CLI the agents run) may show the run's duration, the time
  left, the spend or a cap. The harness enforces every limit from outside, and an agent learns
  that the run has ended only by being stopped.
- **No assigned roles.** Every agent in a run gets the same prompt, canon, tools and limits;
  nothing assigns one a job. Specialisation is something to measure when it emerges, never
  something to configure.
- **Backends are interchangeable underneath one surface.** A Claude agent and a Codex agent see
  the same tools under the same names, the same sandbox and the same board. A change to one
  backend's surface is made to both, or the mixed-backend comparison stops being fair.
- **Agent-facing text is part of the instrument.** The canon (`canon/`), the prompt templates
  (`stigmergeia/prompts/`), task briefs (`tasks/*/README.md`), tool descriptions and the messages
  the lab, the guard and the pulse return to agents change what agents do. Change them
  deliberately, say so in the pull request, and expect results from before and after the change
  not to be pooled.

## The documentation rule

Documentation and comments here obey two rules at once. They apply to three surfaces: comments,
docstrings, and the message a `raise` or a logging call says out loud, which a stranger meets at
the moment something breaks. They never apply to wire format (a dict key, a status value, a file
name a run writes), which is content the code keys on. They do apply to prose a run writes for a
human, and to the Markdown and YAML in this tree, which the private-references check reads too.

### 1. State what is true now

A comment describes the code as it stands. It does not narrate how the code got there.

No `previously`, `used to <verb>`, `changed in`, `we now`, `no longer`. No bare `TODO`, `FIXME`
or `HACK`: a known gap is a refusal the code makes explicitly, a test that pins the current
behaviour, or an issue.

**No dates.** Not on an edit, and not on evidence either. Git records when a line changed, and a
dated measurement belongs in the record that holds it. What the code needs is the standing fact
it depends on, in the present tense.

```python
# Good: the constraint, and why it is load-bearing.
# An agent can sleep inside a turn on its own background job, and the between-turn
# idle count never sees that time, so a foreground sleep of 30 s or more counts here.

# Bad: narrates an incident instead of stating the constraint it taught.
# In the fifth run one agent slept 20 minutes on its own job and nobody noticed.
```

### 2. Every referent must be reachable from this repository

If a comment or docstring names something, a reader holding only this repository must be able to
go look at it: a module path that exists here, a file in this tree, a symbol this package
defines, or a public URL.

It does **not** mean a path into a private tree, a planning note, a conversation, a machine, a
host, an account or a sibling checkout. Nor does it mean a **provenance citation**: the code of a
run that established something, the id of a post on a board the reader cannot open, a section of
a document that is not here, a commit hash. The code is the receipt for what the code does, and
git is the receipt for how it came to do it. Runs, their codes and their results are recorded
outside this tree; what a line of code depends on is the standing fact a run taught, stated here.

When the substance is short, state it inline. When it is long and public, link it. When it is
long and private, restate the part this code depends on.

A consequence worth stating plainly: **a claim in a docstring must match the code under it.**

### Keep the prose style already here

Present tense. Say why, not only what: the constraint a reader would otherwise violate, the
failure a refusal exists to prevent. Document a refusal where the refusal happens. A function's
docstring carries what a caller needs: arguments, units, the shape of what comes back, and how it
fails.

## The gates

Each is a command you can run from the repository root after `uv sync --extra dev`.

| gate | what it checks | command |
|---|---|---|
| tests | the harness suite, including the corpora that pin each documentation rule and the Korax pin | `uv run pytest tests/` |
| task tests | each task's environment, gate and baselines, one process per task | `uv run pytest tasks/snake/tests` and `uv run pytest tasks/lmspeed/tests` |
| end to end | a whole run of scripted agents: a real board, the opening round, the lab, the gate | `uv run pytest -m slow tests/test_demo_e2e.py` |
| state what is true now | no marker comments, no changelog phrasing, no dates in prose | `uv run python -m tools.check_timelessness --root stigmergeia bootstrap node tasks tools tests` |
| reachable referents | every path and module named in prose resolves here; no provenance citations, run codes or board ids; no prose bound to one operator's machines | `uv run python -m tools.check_referents --root stigmergeia bootstrap node tasks tools tests` |
| private references | every published file, of any kind: no absolute home or cluster paths, no run codes, no board ids, and none of the values in the operator's local profile | `uv run python -m tools.check_private_refs` |
| import closure | every module is reached from a test or a listed entry point; no import names a missing module | `uv run python -m tools.check_import_closure --package stigmergeia --roots tests --allowlist tools/closure_allowlist.txt` |

All of them pass on the current tree, and a pull request keeps them passing. Each checker takes
`--report-only`, which prints the full receipt and exits 0, and `--json <file>` for a receipt;
CI writes those to `receipts/`.

Each documentation rule is pinned by a corpus rather than by reading:
`tests/test_gate_fixtures.py` and `tests/test_private_refs.py` hold the strings each checker must
catch and the legitimate prose it must stay quiet on. A rule that stops seeing something fails a
test there instead of quietly reporting a pass; closing a blind spot starts by adding the string
that slipped through.

The pattern files (`tools/timelessness_allowlist.txt`, `tools/referents_allowlist.txt`,
`tools/closure_allowlist.txt`) carry a reason beside every entry, and an entry is a claim that
its reason is true now. When a reason expires, the entry goes. The referent file's `[infra]`
section holds the *shapes* of prose bound to one operator's machines, never the names of hosts
or accounts: a published list would defeat keeping them out. The names are checked instead
against the operator's own local profile, which never leaves their machine, so the check is
mechanical wherever a profile exists and a reviewer's catch where none does (in CI, for one).

A gate may be waived only in writing, with the reason recorded beside the receipt: the failure is
recorded, never the gate rewritten.

## Before you open a pull request

- Run every gate above.
- Nothing that identifies infrastructure enters the tree: no hostnames, usernames, absolute
  paths, credentials or keys. Values of that class come from the local profile
  (`local/profile.example.toml` shows its shape) or the environment.
- Run data never enters git: `runs/`, `boards/`, transcripts, board databases and survey answers
  hold agents' words and your machine's paths. `.gitignore` keeps them out; keep it that way.
- If the change touches agent-facing text or the protocol, say so in the pull request's first
  line.

## Adding to the instrument

- **A task** is a directory under `tasks/` with the shape `tasks/snake/` has: a `README.md` that is
  the brief agents read, a `gate.py` that scores a submission on training or held-out seeds and
  prints one JSON summary line, an environment, baselines, and tests. The held-out split is never
  readable from inside the jail. `docs/TASKS.md` states the gate's contract.
- **A backend** implements an agent whose turns run on another model loop, presenting the tool
  surface `stigmergeia.codex_tools` defines, every call judged by `stigmergeia.guard` first, and
  transcript rows in the shapes `stigmergeia.panel.server` reads. `stigmergeia.fake_agent` is the
  smallest complete example.
- **A protocol piece** (a new opening, a different pulse, a new rule on posting) is described in
  `docs/PROTOCOL.md` as a swappable part: what it does, what it is for, how a config turns it on
  or off, and what would show it working.
