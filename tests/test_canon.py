"""The canon states the run's configuration facts, and nothing else of the run."""

from __future__ import annotations

from pathlib import Path

import pytest

from stigmergeia.canon import CANON_DIR, facts, render, render_canon
from stigmergeia.config import RunConfig
from stigmergeia.profile import REPO_ROOT


def cfg(tmp: Path, **node: object) -> RunConfig:
    return RunConfig.model_validate({
        "run_name": "cx", "task_dir": str(tmp), "n_agents": 2, "per_agent_budget_usd": 1, "total_budget_usd": 2,
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "t")}, "gate": {},
        "node": {"host": "local", "harness_dir": "/h", "run_root": "/r", "venv": "/v", **node}})


def test_the_canon_follows_the_config(tmp_path: Path) -> None:
    out = render_canon(cfg(tmp_path, cores_per_agent=8, run_background_after_s=300, max_background_runs=3),
                       tmp_path / "canon")
    text = (out / "02-swarm-environment.md").read_text()
    assert "on eight cores reserved for you" in text
    assert "after five minutes moves there by itself" in text
    assert "Three background runs can go at once" in text
    assert "{{" not in text


def test_one_core_is_singular(tmp_path: Path) -> None:
    assert render("on {{lab_cores}} core{{lab_cores_plural}}", cfg(tmp_path, cores_per_agent=1)) == "on one core"


def test_every_shipped_template_renders(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    for f in [*CANON_DIR.glob("*.md"), *(REPO_ROOT / "stigmergeia" / "prompts").glob("*.md")]:
        assert "{{" not in render(f.read_text(), c, str(f)), f


def test_an_unknown_placeholder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown canon placeholder"):
        render("the run lasts {{hours}} hours", cfg(tmp_path))


def test_no_fact_names_time_or_money(tmp_path: Path) -> None:
    keys = " ".join(facts(cfg(tmp_path)))
    for word in ("hour", "budget", "usd", "deadline", "wall", "spend", "left"):
        assert word not in keys
