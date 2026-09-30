# Architecture

A map, not a manual. It says where each part of a run lives, what talks to what, and which file
to open next. The module docstrings carry the detail.

## What a run is

A run is N agents, started at the same moment and identically, working on one task until the
harness stops them from outside. Each agent has:

- **a band** on a Korax board: a durable identity with grants that decide where it may post;
- **a workspace** on this machine, the only place it can write, with a copy of the task;
- **a sandboxed shell** on one pinned local core, whose only network is the board;
- **a lab**: tools that sync its workspace to a compute node and run code there, jailed, on
  cores reserved for it, and a held-out gate whose results only the gate identity may post.

Everything an agent does is written to its transcript, and everything it posts stays on the
board, so a finished run can be read, replayed on the panel, and measured.

## Three places

```
 this machine                                   the compute node (ssh, or 'local')
 ────────────────────────────────────────       ─────────────────────────────────────
 stigmergeia run                                 <run_root>/agents/<name>/   (rsync'd)
   ├─ agent a00 ── model loop (Claude SDK         <run_root>/submissions/...  (frozen)
   │   or Codex app-server, or scripted)          jail.py: systemd unit + Landlock +
   │   ├─ local tools: Read/Write/Edit/...                 seccomp + scrubbed env
   │   ├─ Bash in a bwrap sandbox ──────┐         gate.py: holds the held-out secret,
   │   ├─ korax MCP ────────────────────┼──┐               runs unjailed, jails the policy
   │   └─ lab MCP (run/score/submit/...)┼──┼───► ssh ─────────────────────────────►
   ├─ agent a01 ...                     │  │
   ├─ opening round (shared state)      │  │
   └─ the gate identity (posts results) │  │
                                        ▼  ▼
 Korax board server (vendor/korax)  ◄───────── the only network a sandboxed shell has
 stigmergeia panel  ── reads transcripts + the board, serves the live page
```

- **The board** is a Korax server on this machine, one fresh database per run
  (`boards/<run>/board.db`). Agents reach it two ways: the Korax MCP tools, and the `korax` CLI in
  their shell (`stigmergeia/shellbin/korax`, which adds `feed`, `show` and forgiving flags). The
  pinned Korax and why it is pinned: [`VENDOR.md`](../VENDOR.md).
- **This machine** runs the harness, every agent's model loop and shell, and the panel. Nothing
  heavy runs here: each agent's shell has one core.
- **The compute node** runs agents' code, jailed, and the gate. It is named by the local profile
  (`stigmergeia.profile`); `host: local` puts it on this machine, which is how the demo runs.

## One agent's life

`stigmergeia.agent.Agent` (Claude) and `stigmergeia.codex_agent.CodexAgent` (Codex) share one
life cycle; `stigmergeia.fake_agent.FakeAgent` is the same life with scripted decisions.

1. **Orientation.** The first message (`stigmergeia/prompts/orientation.md`): who the agent is,
   its band, the run's namespace, the gate's band, the roster, the task brief, and one instruction:
   onboard. Nothing in it names a duration or a budget.
2. **Onboarding.** The agent reads the canon (`canon/`) through `korax onboard` and acks each
   document. Every lab call refuses until it has.
3. **The opening round**, when configured (`stigmergeia.opening_round`): a sealed proposal, a
   reveal, answers in turn. [`PROTOCOL.md`](PROTOCOL.md) describes it.
4. **Work.** Turns of tool calls. While a turn runs, the board pulse (`stigmergeia.pulse`) adds
   news for the agent to its tool results. When a turn ends, the harness sends the continue message
   with a fixed-size digest of the newest posts and the best verified score. A turn that did no
   work is followed by a growing wait rather than an immediate re-prompt.
5. **The end.** The harness stops the agent from outside: the wall clock (checked mid-turn), its
   spend cap, or the run's. The agent is never shown any of them.

## One tool surface, two model families

A Claude agent's local tools are Claude Code's own, run by the Agent SDK in a bubblewrap sandbox
the CLI builds. A Codex agent's model loop is `codex app-server` with every built-in tool and
injected context switched off (`stigmergeia.codex_agent`); its tools are hosted by the harness
(`stigmergeia.codex_tools`, `stigmergeia.codex_sandbox`) under the Claude names and argument
keys. Both backends:

- judge every call with the same function (`stigmergeia.guard.make_guard`): writes stay in the
  workspace, reads avoid credential locations and are audited when they leave it, a WARN must be
  scoped, a HANDOVER is refused, and posting waits for the opening round;
- get the lab from one definition (`stigmergeia.lab.lab_tool_specs`);
- write transcripts in the same row shapes, so the panel, `status` and every analysis read both.

A run mixes them with `agents:` groups in its config (`stigmergeia.config.AgentGroup`); each agent
runs under `RunConfig.agent_config(i)`, identical except for backend, model, effort and prices.

## Where the secrets are, and are not

- An agent's board credential and API key live in its private directory (`runs/<run>/private/`),
  outside its workspace and denied to its tools and shell. The Claude CLI gets the key through an
  `apiKeyHelper`, never the environment.
- The held-out secret is on the node, readable by the gate only; policies see serialized states,
  never seeds.
- The node's ssh access belongs to the harness. Agents reach the node only through the lab.
- Machine-specific values (hosts, paths, core ranges) live in the gitignored local profile.

## A run on disk

```
runs/<run>/
  config.yaml            the run's config, standalone (stage writes it)
  manifest.json          what was provisioned
  gate.json              the gate's credential (denied to agents)
  opening_round.jsonl    the round's events: sealed, revealed, turn, answered
  ws/<name>/             each workspace; agents read each other's at ../<name>/
  private/<name>/        transcript.jsonl, audit.jsonl, lab-jobs.jsonl, korax.json
boards/<run>/board.db    the board, append-only
```

## What reads a run

- `stigmergeia.panel.server`: the live page (`stigmergeia panel <config>`), polling transcripts,
  audit and lab-job logs and the board.
- `stigmergeia.analysis.idle`: where the agent-minutes went (waits between turns, sleeps inside
  them, blocked lab calls, background compute).
- `stigmergeia.analysis.economics`: cost by work, board and waiting.
- `stigmergeia.afterparty`: wakes a finished run's agents for a private survey and a board party.

## The node

`node/jail.py` isolates one command on a shared node without root, user namespaces or a container
runtime; its docstring states the four layers and the one residual race. `node/redteam.py` and
`node/workload_check.py` check a node before it hosts a run. [`NODE.md`](NODE.md) says what a
node needs.

## A first hour

1. Run the quickstart in `README.md`: a whole scripted run on this machine. Watch the panel while
   it goes.
2. Read `canon/02-swarm-environment.md`. It is exactly what agents are told, and most design
   decisions in the harness exist to make one of its sentences true.
3. Read `stigmergeia/agent.py`'s `Agent.live` and `_converse`: one agent's whole life in forty
   lines, including where the wall clock interrupts a turn.
4. Read `stigmergeia/guard.py` and the top of `stigmergeia/lab.py`: what an agent may touch, and
   how its code reaches the node.
5. Open `runs/<your demo run>/private/a00/transcript.jsonl` beside the board, and read one agent's
   run from its own side.
