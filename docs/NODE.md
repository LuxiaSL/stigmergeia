# The compute node

What it covers, and what it refuses. Agents' code runs on a compute node, inside
`node/jail.py`, on cores reserved for each agent; the held-out gate runs there too. This page says
what the node needs, how the harness uses it, and how to check it before a run. It does not cover
making an unshared machine safe for untrusted code in general: the jail is built for one operator
running their own agents' code beside their own other work, as one Unix user, and its docstring
states the one residual race it leaves.

## What the node needs

- Linux with systemd (user services, `systemd-run --user`), Landlock ABI 4 or later, and seccomp.
- `bubblewrap` is not needed on the node; the jail uses no user namespaces.
- `gcc`, to build the shared-memory shim (`node/shmredir.c`) at provision time.
- A Python venv holding what a task's policies may import (for snake, the standard library is
  enough; for lmspeed, numpy and torch). It is mounted read-only inside the jail.
- ssh access from the harness machine, by a host alias the local profile names.
- Disk under the profile's `run_root` for workspaces and frozen submissions.

## How the harness uses it

- `stigmergeia provision` creates `<run_root>/<run>/agents/<name>/` for every agent, uploads the
  task and the jail's runtime files to `harness_dir`, builds the shim, and writes the run's
  held-out secret to `harness_dir/secrets/` (mode 0600).
- Every lab call syncs the agent's workspace there with rsync and runs a command through the jail
  command prefix (`node.jail_template` in a config), on the agent's cores:
  `first_core + i * cores_per_agent`.
- A held-out submission freezes a snapshot under `<run_root>/<run>/submissions/`, and the gate
  (unjailed, holding the secret) jails the policy it plays.
- At shutdown the harness kills every process of its own on the node whose working directory or
  argv lies under the run's root, by pid, never by matching text.
- Every node command gets `STIGMERGEIA_DATA_ROOT` when the profile names a data root; a task that
  reads a corpus resolves it there, and a config mounts the public part with `--ro {data_root}/...`.

## Checking a node

```bash
python3 node/redteam.py --venv /path/to/venv --module numpy   # the isolation boundaries hold
python3 node/workload_check.py --venv /path/to/venv --cores 0-3 # ordinary workloads still run
```

Both are bounded: they modify only private temporary canaries and cap every resource probe. Run
them on the node itself, outside the jail. `node/test_redteam.py` and
`node/test_python_compat.py` run the same checks under pytest on the node.

## `host: local`

A profile or config with `host: local` runs node commands on this machine with `bash -c` instead
of ssh. The scripted demo uses it with no jail at all, which config permits only when every agent
is scripted.
