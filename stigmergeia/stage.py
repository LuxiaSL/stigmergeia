"""stigmergeia stage: prepare (not launch) a run on a fresh board, end to end.

    stigmergeia stage RUN_NAME BASE --board-port 7440 --panel-port 7450 [--agents N] [--hours H]
                      [--rakes DUMP.jsonl] [--no-services]

Steps, each refusing rather than guessing:

1. A new board at <boards_dir>/<run>/board.db with its operator token. An
   existing board is never reused: two runs on one board would read each
   other's results.
2. The board server, as a systemd user service `stigmergeia-board-<run>`
   (or, with --no-services, the command to start it yourself; staging then
   stops and says what to run next).
3. Board setup: the canon from canon/, the grants the swarm needs, and rakes
   only when --rakes names a dump (off by default).
4. The run's config at <runs_dir>/<run>/config.yaml: the base config with the
   run name, board URL, token path, and optional agent count and duration
   filled in, every relative path made absolute so the file stands alone.
5. `provision` (identities, workspaces, the held-out secret, node upload).
6. The panel as a systemd user service `stigmergeia-panel-<run>`.
7. Preflight, and the launch command.

Machine-specific values (node host, remote dirs, core ranges) come from the
local profile, never from the base config (`stigmergeia.profile`).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from .config import load_config
from .profile import REPO_ROOT

BOOTSTRAP = REPO_ROOT / "bootstrap" / "board.py"
CANON = REPO_ROOT / "canon"
PATH_KEYS = ("runs_dir", "task_dir", "env_file", "korax_cli_bin", "boards_dir")


def _run(argv: list[str], what: str) -> str:
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{what} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


def _wait_for_board(url: str, timeout_s: float = 15.0) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{url}/conformance", timeout=2):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"the board at {url} did not answer within {timeout_s:.0f}s")


def derive_config(base: Path, run: str, board_port: int, token: Path, agents: int | None,
                  hours: float | None) -> dict[str, Any]:
    """The base config with this run's values filled in and every relative
    path resolved against the base config's directory."""
    raw = yaml.safe_load(base.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{base}: expected a mapping at the top level")
    here = base.resolve().parent
    for key in PATH_KEYS:
        if key in raw and not Path(raw[key]).expanduser().is_absolute():
            raw[key] = str(here / raw[key])
    raw["run_name"] = run
    raw["board"] = {**(raw.get("board") or {}), "url": f"http://127.0.0.1:{board_port}",
                    "operator_token_file": str(token)}
    if agents is not None:
        if raw.get("agents"):
            raise ValueError("--agents cannot resize a config with `agents` groups; edit the groups instead")
        raw["n_agents"] = agents
    if hours is not None:
        raw["max_wall_hours"] = hours
    return raw


def stage(a: argparse.Namespace) -> int:
    from . import cli  # provision and preflight live with the other commands

    base = a.base.resolve()
    probe = load_config(base)  # validates the base (and the profile it needs) before anything is created
    boards_dir, runs_dir = probe.boards_dir, probe.runs_dir
    bdir = boards_dir / a.run
    db, token = bdir / "board.db", bdir / "operator.token"
    if db.exists():
        raise RuntimeError(f"{db} exists; a board is never reused across runs")
    cfg_path = runs_dir / a.run / "config.yaml"
    if cfg_path.exists():
        raise RuntimeError(f"{cfg_path} exists; pick a new run name")
    url = f"http://127.0.0.1:{a.board_port}"
    py = sys.executable
    server = Path(py).parent / "korax-server"

    _run([py, str(BOOTSTRAP), "init", "--db", str(db)], "board init")
    print(f"board     {db}")
    if a.no_services:
        print(f"start the board yourself, then re-run setup:\n  {server} serve --db {db} --port {a.board_port}")
        return 0
    if shutil.which("systemd-run") is None:
        raise RuntimeError("systemd-run is not available; use --no-services and start the board yourself")
    _run(["systemd-run", "--user", f"--unit=stigmergeia-board-{a.run}", "--collect", "-p", "Restart=on-failure",
          str(server), "serve", "--db", str(db), "--port", str(a.board_port)], "starting the board service")
    _wait_for_board(url)
    setup = [py, str(BOOTSTRAP), "setup", "--url", url, "--token-file", str(token), "--canon", str(CANON)]
    if a.rakes:
        setup += ["--rakes", str(a.rakes)]
    _run(setup, "board setup")
    print(f"served    {url} (unit stigmergeia-board-{a.run}); canon seeded" + (", rakes seeded" if a.rakes else ""))

    raw = derive_config(base, a.run, a.board_port, token, a.agents, a.hours)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = load_config(cfg_path)
    print(f"config    {cfg_path}")
    cli.provision(cfg)
    _run(["systemd-run", "--user", f"--unit=stigmergeia-panel-{a.run}", "--collect", f"--working-directory={REPO_ROOT}",
          py, "-m", "stigmergeia.cli", "panel", str(cfg_path), "--port", str(a.panel_port)], "starting the panel")
    cli.preflight(cfg)
    print(f"ready: {cfg.run_name}: {cfg.n_agents} agents, {cfg.max_wall_hours}h, board {url}, "
          f"panel http://127.0.0.1:{a.panel_port}/")
    print(f"launch:  stigmergeia run {cfg_path}")
    return 0


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("run", help="the new run's name")
    p.add_argument("base", type=Path, help="a base config (e.g. configs/examples/snake-claude.yaml)")
    p.add_argument("--board-port", type=int, required=True)
    p.add_argument("--panel-port", type=int, required=True)
    p.add_argument("--agents", type=int, help="override n_agents")
    p.add_argument("--hours", type=float, help="override max_wall_hours")
    p.add_argument("--rakes", type=Path, help="seed rakes from this board dump (off by default)")
    p.add_argument("--no-services", action="store_true", help="init the board only; start services yourself")
