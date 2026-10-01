"""The run manifest: what a run was, by hash.

`start_manifest` is written when a run starts, `finish_manifest` when it
ends, both to `runs/<run>/run-manifest.json`. Together they say exactly what
ran, so two runs can be compared knowing what differed: the sha256 of the
config as run, of every canon document and every task file, the harness
commit and whether its tree was clean, the Korax pin, each agent's backend
and model, and at the end the sha256 of the board database.

Hashes are of bytes, never of parsed content: a reader re-hashes the files
they hold and compares.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import RunConfig
from .profile import REPO_ROOT

CANON_DIR = REPO_ROOT / "canon"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under `root` (relative paths), caches and tests excluded."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and "__pycache__" not in rel.parts and ".pytest_cache" not in rel.parts:
            out[rel.as_posix()] = sha256_file(p)
    return out


def harness_commit() -> dict[str, Any]:
    """The harness checkout's commit and whether its tracked files are modified."""
    def git(*args: str) -> str | None:
        r = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": head, "dirty": bool(status) if status is not None else None}


def start_manifest(cfg: RunConfig, config_path: Path | None, korax_commit: str) -> dict[str, Any]:
    agents = []
    for i in range(cfg.n_agents):
        a = cfg.agent_config(i)
        agents.append({"name": cfg.agent_name(i), "backend": a.backend, "model": a.model, "effort": a.effort})
    m = {
        "run_name": cfg.run_name,
        "started": time.time(),
        "config": {"path": str(config_path) if config_path else None,
                   "sha256": sha256_file(config_path) if config_path and config_path.is_file() else None,
                   "resolved": json.loads(cfg.model_dump_json())},
        "harness": harness_commit(),
        "korax_commit": korax_commit,
        "canon": tree_hashes(CANON_DIR),
        "task": {"dir": cfg.task_dir.name, "files": tree_hashes(cfg.task_dir)},
        "agents": agents,
    }
    write(cfg, m)
    return m


def finish_manifest(cfg: RunConfig, results: dict[str, Any]) -> dict[str, Any]:
    path = cfg.run_dir / "run-manifest.json"
    m = json.loads(path.read_text()) if path.is_file() else {"run_name": cfg.run_name}
    db = cfg.boards_dir / cfg.run_name / "board.db"
    m.update({"ended": time.time(), "results": results,
              "board_db": {"path": str(db), "sha256": sha256_file(db) if db.is_file() else None}})
    write(cfg, m)
    return m


def write(cfg: RunConfig, m: dict[str, Any]) -> None:
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.run_dir / "run-manifest.json.tmp"
    tmp.write_text(json.dumps(m, indent=2, default=str))
    tmp.replace(cfg.run_dir / "run-manifest.json")
