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


# The snake canon as agents read it, pinned: snake runs are compared across the project's life, so a
# change to these words must be deliberate (update the pins and say why in the commit).
SNAKE_CANON_SHA256 = {
    "01-board-basics.md": "947e2d895f65380a00346e4b9dca70ab024c2daa2571b486a4c7b55330071617",
    "02-swarm-environment.md": "67fb7567b3c858ce4617a78ee6dde44027f6371ca1df5d8742fcd2c8a123fbb9",
}


def test_the_default_vocabulary_renders_the_pinned_snake_canon(tmp_path: Path) -> None:
    import hashlib
    out = render_canon(cfg(tmp_path), tmp_path / "canon")
    got = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.glob("*.md"))}
    assert got == SNAKE_CANON_SHA256


def test_a_task_speaks_in_its_own_words(tmp_path: Path) -> None:
    hail = cfg(REPO_ROOT / "tasks" / "hailstone")
    text = (render_canon(hail, tmp_path / "canon") / "02-swarm-environment.md").read_text()
    for snake_word in ("policy", "policies", "held-out seeds", "seeds", "episode", "95%"):
        assert snake_word not in text, snake_word
    assert "The score is exact" in text and "best verified search" in text
    orientation = render((REPO_ROOT / "stigmergeia" / "prompts" / "orientation.md").read_text(), hail)
    assert "verified scores" in orientation and "held-out" not in orientation


def test_a_task_vocabulary_cannot_invent_or_mistype_keys(tmp_path: Path) -> None:
    (tmp_path / "canon.toml").write_text('nuon = "search"\n')
    with pytest.raises(ValueError, match="unknown vocabulary key"):
        facts(cfg(tmp_path))
    (tmp_path / "canon.toml").write_text("noun = 3\n")
    with pytest.raises(ValueError, match="must be strings"):
        facts(cfg(tmp_path))


def test_no_vocabulary_names_time_or_money(tmp_path: Path) -> None:
    for task in (tmp_path, REPO_ROOT / "tasks" / "hailstone"):
        values = " ".join(facts(cfg(task)).values()).lower()
        for word in ("budget", "deadline", "hours", "minutes left", "time left", "$"):
            assert word not in values, (task, word)
