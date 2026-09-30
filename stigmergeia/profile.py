"""The local profile: every value that names one operator's machines.

A run config says what an experiment is: the task, the agents, the protocol,
the budgets. Where it runs (the compute node's ssh alias, the remote
directories, the venv exposed in the jail, which cores may be used) belongs
to the machine rather than the experiment, so it lives in one gitignored TOML
file and never in a config or in this repository. `local/profile.example.toml`
shows its shape.

The file is `profile.toml` in the repository's `local/` directory, or
whatever `STIGMERGEIA_PROFILE` names. A missing file is not an error: a run with no
node and no pinned cores needs nothing from it, and a config that does need a
value names the missing key when it asks.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "local" / "profile.toml"
ENV_VAR = "STIGMERGEIA_PROFILE"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeProfile(_Strict):
    """The compute node agents' code runs on. `host: local` runs it on this
    machine instead of over ssh (the jail still applies)."""

    host: str = Field(description="ssh host alias, or 'local'")
    user: str | None = Field(default=None, description="the remote account; informational, ssh config decides")
    harness_dir: str = Field(description="remote dir holding jail.py, the task files and per-run secrets")
    run_root: str = Field(description="remote dir under which each run gets <run_root>/<run_name>")
    venv: str = Field(description="remote venv, exposed read-only inside the jail")
    cores: str | None = Field(default=None, description="node cores runs may pin agents to, e.g. '0-63'")
    data_root: str | None = Field(default=None, description="remote task data (e.g. a corpus)")


class LocalProfile(_Strict):
    cpus: str | None = Field(default=None, description="cores on this machine agents may be pinned to, e.g. '0-7'")


class Profile(_Strict):
    node: NodeProfile | None = None
    local: LocalProfile = LocalProfile()


def profile_path() -> Path:
    env = os.environ.get(ENV_VAR)
    return Path(env).expanduser() if env else DEFAULT_PATH


def load_profile(path: Path | None = None) -> Profile:
    """The profile at `path` (default: `profile_path()`), or an empty one if
    there is no file. A file that exists but does not parse is an error with
    the file named, never a silent empty profile."""
    p = path or profile_path()
    if not p.is_file():
        return Profile()
    try:
        return Profile.model_validate(tomllib.loads(p.read_text()))
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
        raise ValueError(f"{p}: not a valid local profile ({type(e).__name__}: {e})") from e
