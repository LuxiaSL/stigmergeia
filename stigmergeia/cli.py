"""stigmergeia COMMAND CONFIG

demo        a whole run of scripted agents on this machine: no node, no model,
            no key (see stigmergeia.demo). Takes no config.
stage       prepare a run on a fresh board, end to end (see stigmergeia.stage;
            it takes a run name and a base config rather than a config).
provision   mint identities, generate and upload the held-out secret, create
            local and node workspaces, copy the task to the node. Re-runnable:
            existing identities and secrets are reused, never re-minted.
run         start every agent concurrently and wait for all of them.
status      spend and turn counts per agent, from the transcripts.
panel       a live local page: every agent's status, calls and words, the
            board feed, gate results and outside reads (--port, default 7450).
afterparty  wake every agent of a FINISHED run: tell it how the run went,
            collect a private survey, then let the agents meet on the board
            at <ns>/afterparty (see stigmergeia.afterparty).
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import board
from .agent import Agent, Ledger, korax_mcp_command, render_orientation, slot_paths
from .config import NodeConfig, RunConfig, load_api_keys, load_config
from .profile import REPO_ROOT
from .opening_round import OpeningRound
from .lab import AgentSlot, Lab, cores_for

# Stopping the harness mid-call leaves the node's side running: jails and gates
# outlive the lab calls that started them. At shutdown, every process of ours on the node
# whose working directory or an argv entry lies under this run's root is killed
# (never by fuzzy text: a pid's cwd and exact argv prefixes; the script skips
# itself and its ancestors, and gets the root base64-encoded so no argv of its own matches).
NODE_CLEANUP = r"""
import base64, os, signal, sys, time
root = base64.b64decode(os.environ["SWARM_CLEAN_ROOT_B64"]).decode().rstrip("/") + "/"
assert root.startswith("/") and len([p for p in root.split("/") if p]) >= 3, root  # never a shallow root
me, anc = os.getpid(), set()
p = me
while p > 1:
    anc.add(p)
    try:
        p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception:
        break
uid = os.getuid()
killed = []
for rnd in range(3):
    hit = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in anc:
            continue
        pid = int(d)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd") + "/"
            argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except Exception:
            continue
        if cwd.startswith(root) or any(a.decode(errors="replace").startswith(root) or
                                       ("=" + root) in a.decode(errors="replace") for a in argv):
            hit.append(pid)
    for pid in hit:
        try:
            os.kill(pid, signal.SIGTERM if rnd == 0 else signal.SIGKILL)
            killed.append(pid)
        except Exception:
            pass
    if not hit:
        break
    time.sleep(2)
print(f"node cleanup: {len(set(killed))} process(es) under {root}")
"""


async def node_cleanup(cfg: RunConfig) -> None:
    if not cfg.node:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *cfg.node.shell(f"SWARM_CLEAN_ROOT_B64={base64.b64encode(cfg.node.run_root.encode()).decode()} "
                            f"{shlex.quote(cfg.node.remote_python)} -"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(NODE_CLEANUP.encode()), 90)
        print((out or b"").decode(errors="replace").strip())
    except Exception as e:  # report, never raise: the run's results are already in
        print(f"node cleanup failed: {type(e).__name__}: {e} (check {cfg.node.host} for jobs under {cfg.node.run_root})")

log = logging.getLogger("swarm")
PROMPTS = Path(__file__).resolve().parent / "prompts"


def _ssh(node: NodeConfig, script: str) -> None:
    r = subprocess.run(node.shell(script), capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"{node.host}: {script[:80]!r} failed ({r.returncode}): {r.stderr.strip()[-1000:]}")


def _secret_path(cfg: RunConfig) -> Path:
    return cfg.gate.heldout_secret or cfg.run_dir / "heldout.secret"


def _creds(cfg: RunConfig) -> tuple[board.Credential, list[board.Credential]]:
    gate = board.Credential.model_validate_json((cfg.run_dir / "gate.json").read_text())
    agents = []
    for i in range(cfg.n_agents):
        _, private = slot_paths(cfg, i)
        agents.append(board.Credential.model_validate_json((private / "korax.json").read_text()))
    return gate, agents


def provision(cfg: RunConfig) -> None:
    op = cfg.board.operator_token_file.read_text().strip()
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    gate = board.mint(cfg.board.url, op, f"{cfg.run_name}-gate", cfg.run_dir / "gate.json")
    print(f"gate      {gate.identity}")
    for i in range(cfg.n_agents):
        ws, private = slot_paths(cfg, i)
        ws.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True)
        os.chmod(private, 0o700)
        os.chmod(private.parent, 0o700)  # <run>/private: nobody but the harness lists it
        cred = board.mint(cfg.board.url, op, f"{cfg.run_name}-{cfg.agent_name(i)}", private / "korax.json")
        # A reference copy of the task (rules, gate, baselines) the agent can
        # read and run. The gate that scores it uses the canonical copy.
        r = subprocess.run(["rsync", "-a", "--exclude", "__pycache__/", "--exclude", "tests/",
                            f"{cfg.task_dir}/", f"{ws}/task/"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"copying the task into {ws}: {r.stderr[-500:]}")
        if cfg.solo:
            from .agent import solo_brief
            readme = ws / "task" / "README.md"
            readme.write_text(solo_brief(readme.read_text()))
        print(f"{cfg.agent_name(i):9s} {cred.identity}")

    secret = _secret_path(cfg)
    if not secret.is_file():
        board.write_private(secret, secrets.token_hex(32))
    if cfg.node:
        n = cfg.node
        task = f"{n.harness_dir}/tasks/{cfg.task_dir.name}"
        dirs = [f"{n.run_root}/agents/{cfg.agent_name(i)}" for i in range(cfg.n_agents)]
        _ssh(n, " && ".join([
            "mkdir -p " + " ".join(shlex.quote(d) for d in [*dirs, f"{n.run_root}/submissions", task]),
            f"mkdir -p {shlex.quote(n.harness_dir + '/secrets')}",
            f"chmod 700 {shlex.quote(n.harness_dir + '/secrets')}",
        ]))
        # --delete: the gate's canonical copy mirrors the task exactly; a file removed from the task
        # (an old baseline, say) must not live on where the gate and the lab can still reach it
        r = subprocess.run(["rsync", "-a", "--delete", "--exclude", "__pycache__/", "--exclude", "tests/",
                            f"{cfg.task_dir}/", n.remote(f"{task}/")], capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"task upload failed: {r.stderr[-1000:]}")
        if n.jail_template.strip():  # a jail-less lab (the scripted demo) needs no jail files
            # The jail's runtime files only (not its test scripts), then build the
            # shm shim on the node if the source is newer than the library.
            src = REPO_ROOT / "node"
            r = subprocess.run(["rsync", "-a", "--exclude", "__pycache__/",
                                str(src / "jail.py"), str(src / "shmredir.c"), str(src / "python_compat"),
                                n.remote(f"{n.harness_dir}/")], capture_output=True, text=True, timeout=120)
            if r.returncode:
                raise RuntimeError(f"harness upload failed: {r.stderr[-1000:]}")
            h = shlex.quote(n.harness_dir)
            _ssh(n, f"cd {h} && {{ [ libshmredir.so -nt shmredir.c ] || "
                         "gcc -O2 -Wall -shared -fPIC -o libshmredir.so shmredir.c -pthread; }")
        remote_secret = f"{n.harness_dir}/secrets/{cfg.run_name}.secret"
        r = subprocess.run(n.shell(f"umask 077 && cat > {shlex.quote(remote_secret)}"),
                           input=secret.read_text(), capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError(f"secret upload failed: {r.stderr[-1000:]}")
        print(f"node      {n.host}:{n.run_root} ({cfg.n_agents} workspaces), task at {task}")
    (cfg.run_dir / "manifest.json").write_text(json.dumps(
        {"provisioned": time.time(), "config": json.loads(cfg.model_dump_json())}, indent=2, default=str))
    print(f"provisioned {cfg.run_dir}")


def preflight(cfg: RunConfig) -> str:
    """Fail closed before spending anything, and return the Korax commit in use.

    Refuses a Korax that is not the pinned one (stigmergeia.korax_pin).
    Claude Code's Linux sandbox needs bubblewrap AND socat, and without them
    it runs shell commands unsandboxed after a stderr warning. The Codex
    backend builds its own sandbox from the same tools, plus systemd-run."""
    from .korax_pin import verify_korax_pin
    korax_commit = verify_korax_pin()
    if "codex" in cfg.backends():
        from .codex_sandbox import preflight_tools
        missing = preflight_tools()
        if missing:
            raise RuntimeError(f"backend: codex needs {', '.join(missing)} for its sandbox; refusing to start")
        if shutil.which(cfg.codex.binary) is None:
            raise RuntimeError(f"no codex CLI {cfg.codex.binary!r} on PATH")
        if not cfg.codex.auth_file.is_file():  # existence only: the harness never reads it
            raise RuntimeError(f"{cfg.codex.auth_file} does not exist: log in with `codex login` first")
    if cfg.shell:
        missing = [t for t in ("bwrap", "socat") if shutil.which(t) is None]
        if missing:
            raise RuntimeError(f"shell: true needs {' and '.join(missing)} for the sandbox "
                               f"(Fedora: sudo dnf install {' '.join('bubblewrap' if m == 'bwrap' else m for m in missing)}); "
                               "refusing to start agents with an unsandboxed shell")
        if not (cfg.korax_cli_bin / "korax").is_file():
            raise RuntimeError(f"no korax CLI at {cfg.korax_cli_bin}/korax (run `uv sync` in this repository)")
    return korax_commit


async def run(cfg: RunConfig, config_path: Path | None = None) -> int:
    from . import manifest
    korax_commit = preflight(cfg)
    manifest.start_manifest(cfg, config_path, korax_commit)
    backends = cfg.backends()
    keys = load_api_keys(cfg.env_file) if "claude" in backends else [""]
    gate, creds = _creds(cfg)
    catalogs: dict[str, Path] = {}
    if "codex" in backends:
        from .codex_agent import build_catalog, rate_limits
        # One stripped catalog per Codex model the run uses.
        for i in range(cfg.n_agents):
            acfg = cfg.agent_config(i)
            if acfg.backend == "codex" and acfg.model not in catalogs:
                catalogs[acfg.model] = build_catalog(acfg, cfg.run_dir / f"codex-catalog-{acfg.model}.json",
                                                     cfg.run_dir / "private" / "codex-harness-home")
        rl = await rate_limits(cfg, cfg.run_dir / "private" / "codex-harness-home")
        with open(cfg.run_dir / "codex-ratelimits.jsonl", "a") as f:
            f.write(json.dumps({"when": "start", **(rl or {})}) + "\n")
        print(f"codex rate limits at start: {json.dumps((rl or {}).get('rateLimits', rl))[:400]}")
    template = (PROMPTS / ("solo.md" if cfg.solo else "orientation.md")).read_text()
    lab = Lab(cfg, gate) if cfg.node else None
    ledger = Ledger(cfg.total_budget_usd)
    deadline = time.time() + cfg.max_wall_hours * 3600
    roster = [(cfg.agent_name(j), c.identity) for j, c in enumerate(creds)]
    agents: list[Any] = []
    for i, cred in enumerate(creds):
        acfg = cfg.agent_config(i)  # this agent's backend, model, effort and prices
        ws, private = slot_paths(cfg, i)
        name = cfg.agent_name(i)
        slot = AgentSlot(name=name, index=i, workspace=ws, private=private,
                         remote_ws=f"{cfg.node.run_root}/agents/{name}" if cfg.node else "",
                         cores=cores_for(cfg.node, i) if cfg.node else "")
        args = (acfg, slot, cred, gate, lab, keys[i % len(keys)], korax_mcp_command(cfg.korax_cli_bin), ledger,
                deadline, render_orientation(acfg, template, name, cred, gate, roster))
        if acfg.backend == "codex":
            from .codex_agent import CodexAgent
            agents.append(CodexAgent(*args, catalog=catalogs[acfg.model]))
        elif acfg.backend == "fake":
            from .fake_agent import FakeAgent
            agents.append(FakeAgent(*args))
        else:
            agents.append(Agent(*args))
    names = {gate.identity: "GATE", **{c.identity: cfg.agent_name(i) for i, c in enumerate(creds)}}
    opening_round = None
    if cfg.opening_round:
        opening_round = OpeningRound(ns=cfg.board.ns, names=[cfg.agent_name(i) for i in range(len(creds))],
                          creds={cfg.agent_name(i): c for i, c in enumerate(creds)},
                          reveal_after_s=cfg.opening_round.reveal_after_s, answer_turn_s=cfg.opening_round.answer_turn_s,
                          log_path=cfg.run_dir / "opening_round.jsonl")
    for a in agents:
        a.names = names
        a.opening_round = opening_round
    if cfg.local_cpu_quota and cfg.local_pin_cpus:
        cores, warn = cfg.local_core_map()
        print(f"local cores: {' '.join(f'{a}={c}' for a, c in cores.items())}" + (f"  WARNING: {warn}" if warn else ""))
    mix = ", ".join(f"{a.slot.name}={a.cfg.backend}:{a.cfg.model}" for a in agents) if cfg.agents else \
        f"{cfg.backend} on {cfg.model}"
    print(f"starting {len(agents)} agents ({mix}) "
          f"(budget ${cfg.per_agent_budget_usd}/agent, ${cfg.total_budget_usd} total, {cfg.max_wall_hours}h)")
    deadline_task = asyncio.create_task(opening_round.deadline()) if opening_round else None
    try:
        results = await asyncio.gather(*(a.live() for a in agents), return_exceptions=True)
    finally:
        if deadline_task:
            deadline_task.cancel()
        if opening_round:
            opening_round.close()
        await node_cleanup(cfg)
        if "codex" in backends:
            from .codex_agent import rate_limits
            rl = await rate_limits(cfg, cfg.run_dir / "private" / "codex-harness-home")
            with open(cfg.run_dir / "codex-ratelimits.jsonl", "a") as f:
                f.write(json.dumps({"when": "end", **(rl or {})}) + "\n")
            print(f"codex rate limits at end: {json.dumps((rl or {}).get('rateLimits', rl))[:400]}")
    failed = 0
    for a, r in zip(agents, results):
        if isinstance(r, BaseException):
            failed += 1
            print(f"{a.slot.name}: CRASHED {type(r).__name__}: {r}")
        else:
            print(f"{a.slot.name}: {r.stop_reason:24s} ${r.spent:7.2f}  turns {r.turns}  restarts {r.restarts}")
    print(f"total ${ledger.total():.2f}")
    manifest.finish_manifest(cfg, {
        "failed": failed, "spent_usd": round(ledger.total(), 4),
        "agents": {a.slot.name: ({"crashed": f"{type(r).__name__}: {r}"} if isinstance(r, BaseException) else
                                 {"stop_reason": r.stop_reason, "turns": r.turns, "restarts": r.restarts,
                                  "spent_usd": round(r.spent, 4)}) for a, r in zip(agents, results)}})
    return 1 if failed else 0


def status(cfg: RunConfig) -> None:
    total = 0.0
    for i in range(cfg.n_agents):
        prices = cfg.agent_config(i).prices
        _, private = slot_paths(cfg, i)
        t = private / "transcript.jsonl"
        turns, banked, running, last = 0, 0.0, 0.0, "not started"
        estimate, seen = 0.0, set()
        if t.is_file():
            for line in t.read_text().splitlines():
                row = json.loads(line)
                kind = row.get("_type")
                if kind == "AssistantMessage":
                    estimate += prices.generated_cost(row.get("content"))
                if kind == "AssistantMessage" and row.get("usage") and row.get("message_id") not in seen:
                    seen.add(row.get("message_id"))
                    estimate += prices.cost(row["usage"])
                if kind == "ResultMessage":
                    turns += 1
                    running = row.get("total_cost_usd") or running
                elif kind in ("ConversationResetMessage", "harness_error"):
                    banked, running = banked + running, 0.0
                if kind:
                    last = kind
        spent = max(banked + running, estimate)
        total += spent
        print(f"{cfg.agent_name(i)}  ${spent:7.2f}  turns {turns:4d}  last {last}")
    print(f"total ${total:.2f}")


def afterparty_cmd(cfg: RunConfig, a: argparse.Namespace) -> int:
    from . import afterparty as ap
    settings = ap.PartySettings(budget_usd=a.budget, rounds=a.rounds, only=a.only, facts_file=a.facts)
    unit = None
    url = a.board_url
    try:
        if a.serve_port:
            unit = ap.serve_board(cfg, a.serve_port)
            url = f"http://127.0.0.1:{a.serve_port}"
            print(f"serving {cfg.run_name}'s board on {url} as {unit}")
        if a.dry_run:
            return ap.dry_run(cfg, settings, url)
        return asyncio.run(ap.afterparty(cfg, settings, url))
    finally:
        if unit:
            ap.stop_board(unit)
            print(f"stopped {unit}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        from . import demo
        dp = argparse.ArgumentParser(prog="stigmergeia demo", description=demo.__doc__.split("\n\n")[0])
        demo.add_arguments(dp)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        try:
            return demo.demo(dp.parse_args(sys.argv[2:]))
        except (FileNotFoundError, ValueError, RuntimeError, board.BoardError) as e:
            print(f"stigmergeia demo: {e}", file=sys.stderr)
            return 2
    if len(sys.argv) > 1 and sys.argv[1] == "stage":
        from . import stage
        sp = argparse.ArgumentParser(prog="stigmergeia stage", description=stage.__doc__.split("\n\n")[0])
        stage.add_arguments(sp)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        try:
            return stage.stage(sp.parse_args(sys.argv[2:]))
        except (FileNotFoundError, ValueError, RuntimeError, board.BoardError) as e:
            print(f"stigmergeia stage: {e}", file=sys.stderr)
            return 2
    ap = argparse.ArgumentParser(prog="stigmergeia", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["provision", "run", "status", "panel", "afterparty"])
    ap.add_argument("config", type=Path)
    ap.add_argument("--port", type=int, default=7450, help="panel: local port to serve on")
    ap.add_argument("--board-url", help="afterparty: the run's board, if not at the URL in its credentials")
    ap.add_argument("--serve-port", type=int, help="afterparty: serve the run's stopped board.db on this port "
                    "(unit stigmergeia-board-afterparty-<run>) for the party, and stop it afterwards")
    ap.add_argument("--only", action="append", help="afterparty: wake only this agent (repeatable)")
    ap.add_argument("--budget", type=float, default=1.5, help="afterparty: USD cap per agent (harness-side)")
    ap.add_argument("--rounds", type=int, default=3, help="afterparty: continue prompts once the board opens")
    ap.add_argument("--dry-run", action="store_true", help="afterparty: print the facts and wake-up messages only")
    ap.add_argument("--facts", type=Path, help="afterparty: a YAML file of run descriptions and wake texts "
                    "merged over the generic prompts (see stigmergeia/prompts/afterparty.yaml)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = load_config(a.config)
        if a.command == "provision":
            provision(cfg)
            return 0
        if a.command == "status":
            status(cfg)
            return 0
        if a.command == "panel":
            from .panel.server import serve
            serve(cfg, a.port)
            return 0
        if a.command == "afterparty":
            return afterparty_cmd(cfg, a)
        return asyncio.run(run(cfg, a.config.resolve()))
    except (FileNotFoundError, ValueError, RuntimeError, board.BoardError) as e:
        print(f"stigmergeia: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
