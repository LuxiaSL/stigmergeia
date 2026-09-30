"""stigmergeia demo: a whole swarm run on this machine, with no model and no key.

    stigmergeia demo [--agents 4] [--minutes 5] [--port 7450] [--no-round]

It starts a fresh Korax board as a child process, seeds the canon, provisions
scripted agents (`backend: fake`, see `stigmergeia.fake_agent`) whose lab runs
on this machine, and serves the panel while they work: onboarding, the opening
round, scoring, held-out submits posted by the gate, posts with edges. When
the run ends, the board and the panel stay up until Ctrl-C, so the finished
run can be read.

Two things differ from a real run, and both are stated where they happen:
the lab has no jail (the only code it runs is the scripted agents' own
policies, and config refuses an empty jail for any other backend), and the
held-out gate plays fewer episodes so a batch takes seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from .profile import REPO_ROOT

BOOTSTRAP = REPO_ROOT / "bootstrap" / "board.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str, proc: subprocess.Popen[bytes], timeout_s: float = 20.0) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"the board server exited with {proc.returncode} before answering")
        try:
            with urllib.request.urlopen(f"{url}/conformance", timeout=2):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"the board at {url} did not answer within {timeout_s:.0f}s")


def demo_config(run: str, url: str, token: Path, agents: int, minutes: float, rounds: bool) -> dict[str, Any]:
    local = REPO_ROOT / "runs" / run
    return {
        "run_name": run,
        "backend": "fake",
        "model": "scripted",
        "task_dir": str(REPO_ROOT / "tasks" / "snake"),
        "n_agents": agents,
        "per_agent_budget_usd": 1,
        "total_budget_usd": agents,
        "max_wall_hours": minutes / 60,
        "shell": False,
        "local_cpu_quota": None,
        "pulse_s": 20,
        "idle_base_s": 5,
        "idle_max_s": 20,
        "board": {"url": url, "operator_token_file": str(token)},
        "node": {
            "host": "local",
            "harness_dir": str(local / "node-harness"),
            "run_root": str(local / "node"),
            "venv": sys.prefix,
            "python": sys.executable,
            "cores_per_agent": 1,
            # No jail: the only code this lab runs is the scripted agents' policies.
            "jail_template": " ",
            "run_timeout_s": 120,
            "wait_cap_s": 30,
        },
        "gate": {"train_episodes": 10, "heldout_episodes": 30, "episode_cpu_s": 5.0, "probe_episodes": 0,
                 "background_after_s": None},
        **({"opening_round": {"reveal_after_s": 120, "answer_turn_s": 20}} if rounds else {}),
    }


def run_demo(agents: int, minutes: float, port: int | None, rounds: bool = True,
             stay: bool = False) -> tuple[int, Path, Path]:
    """One scripted run, end to end. Returns (exit code, run dir, board db).
    `port`: serve the panel there while it runs (None: no panel). `stay`: keep
    the board and the panel up after the run until Ctrl-C."""
    from . import cli
    from .config import load_config
    from .panel.server import serve

    if shutil.which("rsync") is None:
        raise RuntimeError("the demo needs rsync on PATH")
    run = time.strftime("demo-%Y%m%d-%H%M%S")
    bdir = REPO_ROOT / "boards" / run
    db, token = bdir / "board.db", bdir / "operator.token"
    py = sys.executable
    init = subprocess.run([py, str(BOOTSTRAP), "init", "--db", str(db)], capture_output=True, text=True)
    if init.returncode:
        raise RuntimeError(f"board init failed: {(init.stderr or init.stdout)[-800:]}")
    board_port = _free_port()
    url = f"http://127.0.0.1:{board_port}"
    server = subprocess.Popen([str(Path(py).parent / "korax-server"), "serve", "--db", str(db),
                               "--port", str(board_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait(url, server)
        r = subprocess.run([py, str(BOOTSTRAP), "setup", "--url", url, "--token-file", str(token),
                            "--canon", str(REPO_ROOT / "canon")], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"board setup failed: {(r.stderr or r.stdout)[-800:]}")
        cfg_path = REPO_ROOT / "runs" / run / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(demo_config(run, url, token, agents, minutes, rounds), sort_keys=False))
        cfg = load_config(cfg_path)
        cli.provision(cfg)
        if port is not None:
            threading.Thread(target=serve, args=(cfg, port), daemon=True).start()
            print(f"\npanel:  http://127.0.0.1:{port}/   board: {url}   config: {cfg_path}", flush=True)
        rc = asyncio.run(cli.run(cfg, cfg_path))
        if stay:
            print(f"\nthe run has ended; the panel stays up at http://127.0.0.1:{port}/ (Ctrl-C to stop)",
                  flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return rc, cfg.run_dir, db
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


def demo(a: argparse.Namespace) -> int:
    rc, _, _ = run_demo(a.agents, a.minutes, a.port, rounds=not a.no_round, stay=True)
    return rc


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agents", type=int, default=4)
    p.add_argument("--minutes", type=float, default=5.0)
    p.add_argument("--port", type=int, default=7450, help="the panel's local port")
    p.add_argument("--no-round", action="store_true", help="skip the opening round")
