"""Every example config loads, with a stand-in profile, into a valid run."""

from __future__ import annotations

from pathlib import Path

import pytest

from stigmergeia.config import load_config
from stigmergeia.profile import REPO_ROOT, NodeProfile, Profile

EXAMPLES = sorted((REPO_ROOT / "configs" / "examples").glob("*.yaml"))
PROFILE = Profile(node=NodeProfile(host="box", harness_dir="/srv/h", run_root="/srv/runs", venv="/srv/v",
                                   cores="0-63", data_root="/srv/data"))


def test_there_are_examples() -> None:
    assert len(EXAMPLES) >= 4


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_an_example_loads(path: Path) -> None:
    cfg = load_config(path, PROFILE)
    assert cfg.task_dir.resolve().is_dir(), cfg.task_dir
    assert cfg.node is not None and cfg.node.run_root == f"/srv/runs/{cfg.run_name}"
    for i in range(cfg.n_agents):
        cfg.agent_config(i)
