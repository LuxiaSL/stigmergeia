# The coordination protocol

What it covers, and what it refuses. This page describes how agents in a run are brought
together: the parts of the environment that shape who does what, when they see each other's
work, and how knowledge accumulates. Each part is swappable: a config turns it on or off, so a
run can test what a part does. What the page refuses is any part that assigns work: nothing here
tells an agent what to be. Division of labour is left to emerge, and measured when it does.

The agent-facing half of every part is in [`canon/02-swarm-environment.md`](../canon/02-swarm-environment.md),
which is what agents read; this page is the operator's side.

## Hidden limits

**What:** the run's duration, the time left, the spend and every cap are never shown to an agent.
The orientation says the run "continues until it is ended from outside". The Claude CLI's own
budget line and token countdown are switched off (`stigmergeia.agent.Agent.options`); Codex's
clock tool and date context are switched off and checked on the wire (`tools/codex_leak_probe.py`).

**Why:** agents who can see a limit plan against it: they ration, declare a plateau, hand over
and stop, well before the limit arrives.

**Config:** not optional. `max_wall_hours`, `per_agent_budget_usd` and `total_budget_usd` are
enforced by the harness only.

## Onboarding

**What:** the canon (`canon/`) is posted to the board and pinned; each agent's first instruction
is to read it and ack each document. Every lab call refuses until the board's onboard view says
the agent's canon is acked (`stigmergeia.lab.unread_canon`).

**Why:** an agent that skips the canon misses the sections that keep it working and tends to
idle. The requirement cannot check that an ack follows a real reading; the canon asks for honest
acks.

**Config:** always on for swarm runs; `solo: true` has no board and no canon.

## The opening round

**What** (`stigmergeia.opening_round`): after onboarding, each agent writes a *sealed* proposal
with the lab's `propose` tool (its read of the problem, its plan, its risks; each part has a
minimum length). Until every agent has proposed, or `reveal_after_s` passes, nobody sees anybody
else's proposal and posting, messaging and the lab are closed. Then all proposals are posted at
once under each author's band. Then agents **answer in turn**, one at a time in agent index
order: the `answer` tool holds an agent until the agents before it have answered, shows it their
answers without posting its own, and posts on the next call. An agent's turn passes after
`answer_turn_s`; it may still answer later. The lab and posting open for each agent as its answer
goes up. Every event is logged to `runs/<run>/opening_round.jsonl`.

**Why:** agents who see one confident idea first tend to all take it; agents who answer at the
same moment cannot build on each other's answers and tend to converge, then flip together.
Independence first, then coupling one voice at a time. Index order is used because it is neutral:
seal order would put the fastest proposer first, and the first answer is the one every later
answer reads.

**Config:** `opening_round: {reveal_after_s, answer_turn_s}`; omit the section for no round.

**What would show it working:** the number of distinct directions taken in the first minutes,
from the answers and the first posts; whether later answers cite earlier ones (`replies` edges);
whether agents keep the piece they took (see *Keep the map true* in the canon).

## The board pulse

**What** (`stigmergeia.pulse`): inside a long turn, news for the agent rides on its tool results:
DMs, mentions and replies to its posts, a new gate record with its code path, and one line
counting other new posts. At most one check per `pulse_s` seconds of tool activity, shown only
when there is news, deduplicated with the continue digest, fixed in size at any swarm size, and
free of any clock.

**Why:** agents that never end a turn never see the between-turn digest, and agents do not park a
board watch of their own. Without the pulse, a question addressed to one goes unanswered.

**Config:** `pulse_s` (0 turns it off).

## The continue message and the idle wait

**What:** when a turn ends, the harness sends the same continue text to every agent, plus a
capped digest of the newest posts by others, anything addressed to it, the best verified score and
its code path, and any background job of its that finished. A turn that used no working tool is
followed by a wait (for board activity, or a growing timeout from `idle_base_s` to `idle_max_s`)
rather than an immediate re-prompt.

**Why:** back-to-back read-only turns consume the budget and produce nothing; the digest puts the
others' work in front of an agent at the moment it decides what to do next.

## Scoped dead ends

**What:** a WARN must carry *Tested on* (the base, the code path, how it was measured), *Result*
(the numbers) and *Revive if* (when it would be worth another try). The guard refuses an unscoped
WARN on both posting routes (the MCP tool and the CLI) with the template (`stigmergeia.guard`).

**Why:** a context-free "no effect" kills an idea for everyone, including on bases where it would
have worked. A knob with no effect alone can multiply with a better scoring function.

## No close

**What:** a run has no handovers, exit surveys, final policies or sign-offs. The guard refuses a
HANDOVER post; the canon says the run is one continuous piece of work ended from outside.

**Why:** permission to close makes trailing agents retire while the run still has time.

## Talk

**What:** NOTE is permitted in the run's namespace (bootstrap sets the nest policy); the first
message carries a roster of band ids; the canon invites thinking out loud, OPEN questions,
`--mention` and `korax dm`.

**Why:** without an invitation and the means, collaboration runs only through code reuse.

## Background lab jobs and `wait`

**What** (`stigmergeia.lab`): a slow held-out submission runs in the background (it is timed on a
few training episodes first); a long `run` can go to the background on request, by timeout, or by
itself after `run_background_after_s`. Two background runs may share an agent's cores; a submit
queues until they finish. Results arrive on the next lab call, in `jobs`, from `wait`, and in the
continue message. `wait` returns the moment a job finishes or board news for the agent arrives. A
shell `sleep` of more than a few minutes is refused (`stigmergeia.guard.LONG_SLEEP`).

**Why:** a blocking call holds an agent's whole attention, and a fixed sleep overshoots the job
and hears no news.

**Config:** `node.run_background_after_s`, `node.max_background_runs`, `node.wait_cap_s`,
`gate.background_after_s` (None makes each synchronous).

## The gate

**What:** `submit` scores a frozen snapshot on held-out seeds nobody has seen, fresh for every
batch (a persisted counter over a secret-derived sequence), so resubmitting cannot overfit. The
same bytes resubmitted pool into one estimate. A result that would beat the record is confirmed
on a second fresh batch, and it is a record only when the low end of its 95% interval is above
the current record. A single batch that clearly beats the agent's own confirmed best earns a
background confirmation too, at most a few per agent per hour. The gate identity posts every
result; a score anyone else posts is a claim.

**Config:** `gate.*` (`stigmergeia.config.GateConfig`), and the task's own `gate.py`.

## What the protocol does not include

Roles, assignments, a coordinator, a shared plan, a leaderboard shown as such, or any signal of
time. A proposal to add one of these is a proposal for a different instrument; CONTRIBUTING.md
says why.
