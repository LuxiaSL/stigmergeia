"""The local profile fills machine values a config leaves out; agent groups
give each agent its backend and model; an empty jail is refused unless every
agent is scripted; stage derives a standalone run config from a base."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stigmergeia.config import RunConfig, apply_profile, load_config
from stigmergeia.demo import demo_config
from stigmergeia.profile import LocalProfile, NodeProfile, Profile, load_profile
from stigmergeia.stage import derive_config

PROFILE = Profile(
    node=NodeProfile(host="box", harness_dir="/srv/harness", run_root="/srv/runs", venv="/srv/venv", cores="32-63"),
    local=LocalProfile(cpus="0-7"),
)


def base(tmp: Path, **kw: object) -> dict:
    return {"run_name": "t1", "task_dir": str(tmp), "n_agents": 2, "per_agent_budget_usd": 1,
            "total_budget_usd": 10, "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "t")},
            "gate": {}, **kw}


def test_the_profile_fills_what_the_config_leaves_out(tmp_path: Path) -> None:
    raw = apply_profile(base(tmp_path, node={"cores_per_agent": 4}), PROFILE)
    cfg = RunConfig.model_validate(raw)
    assert cfg.node is not None
    assert (cfg.node.host, cfg.node.harness_dir, cfg.node.venv) == ("box", "/srv/harness", "/srv/venv")
    assert cfg.node.run_root == "/srv/runs/t1"
    assert cfg.node.first_core == 32
    assert cfg.local_cpus == "0-7"


def test_an_explicit_config_value_wins(tmp_path: Path) -> None:
    raw = apply_profile(base(tmp_path, node={"host": "other", "first_core": 40}, local_cpus="8-9"), PROFILE)
    assert raw["node"]["host"] == "other" and raw["node"]["first_core"] == 40 and raw["local_cpus"] == "8-9"


def test_a_node_without_a_profile_names_the_missing_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="host, harness_dir, venv, run_root not set"):
        apply_profile(base(tmp_path, node={}), Profile())


def test_a_run_without_a_node_needs_no_profile(tmp_path: Path) -> None:
    cfg = RunConfig.model_validate(apply_profile(base(tmp_path), Profile()))
    assert cfg.node is None


def test_a_broken_profile_file_is_an_error_not_an_empty_profile(tmp_path: Path) -> None:
    bad = tmp_path / "profile.toml"
    bad.write_text("[node]\nhost = 3\n")
    with pytest.raises(ValueError, match="not a valid local profile"):
        load_profile(bad)
    assert load_profile(tmp_path / "absent.toml") == Profile()


def test_groups_give_each_agent_its_backend_model_and_prices(tmp_path: Path) -> None:
    cfg = RunConfig.model_validate(base(tmp_path, n_agents=3, agents=[
        {"count": 2, "backend": "claude", "model": "claude-sonnet-5-5"},
        {"count": 1, "backend": "codex", "model": "gpt-6-luna", "prices": {"input": 0.1, "output": 0.5}},
    ]))
    a0, a1, a2 = (cfg.agent_config(i) for i in range(3))
    assert (a0.backend, a0.model) == ("claude", "claude-sonnet-5-5") and a1.model == a0.model
    assert (a2.backend, a2.model, a2.prices.input) == ("codex", "gpt-6-luna", 0.1)
    assert a0.prices == cfg.prices
    assert cfg.backends() == {"claude", "codex"}
    with pytest.raises(IndexError):
        cfg.agent_config(3)


def test_groups_must_account_for_every_agent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hold 1 agents but n_agents is 2"):
        RunConfig.model_validate(base(tmp_path, agents=[{"count": 1, "model": "m"}]))


def test_an_empty_jail_is_refused_unless_every_agent_is_scripted(tmp_path: Path) -> None:
    node = {"host": "local", "harness_dir": "/h", "run_root": "/r", "venv": "/v", "jail_template": " "}
    RunConfig.model_validate(base(tmp_path, backend="fake", node=node))
    with pytest.raises(ValueError, match="allowed only when every agent is fake"):
        RunConfig.model_validate(base(tmp_path, backend="claude", node=node))
    with pytest.raises(ValueError, match="allowed only when every agent is fake"):
        RunConfig.model_validate(base(tmp_path, node=node, agents=[
            {"count": 1, "backend": "fake", "model": "s"}, {"count": 1, "backend": "claude", "model": "m"}]))


def test_the_local_node_runs_commands_here(tmp_path: Path) -> None:
    node = {"host": "local", "harness_dir": "/h", "run_root": "/r", "venv": "/v"}
    cfg = RunConfig.model_validate(base(tmp_path, node=node))
    assert cfg.node.shell("true") == ["bash", "-c", "true"] and cfg.node.remote("/r/x") == "/r/x"
    remote = cfg.node.model_copy(update={"host": "box"})
    assert remote.shell("true") == ["ssh", "box", "true"] and remote.remote("/r/x") == "box:/r/x"


def test_stage_derives_a_standalone_config(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    b = tmp_path / "base" / "b.yaml"
    b.write_text(yaml.safe_dump(base(tmp_path, task_dir="../task", run_name="template")))
    raw = derive_config(b, "run-7", 7441, tmp_path / "op.token", agents=4, hours=0.5)
    assert raw["run_name"] == "run-7" and raw["n_agents"] == 4 and raw["max_wall_hours"] == 0.5
    assert raw["board"]["url"] == "http://127.0.0.1:7441"
    assert raw["task_dir"] == str(tmp_path / "base" / "../task")
    out = tmp_path / "elsewhere" / "config.yaml"
    out.parent.mkdir()
    out.write_text(yaml.safe_dump(raw))
    assert load_config(out, Profile()).task_dir.resolve() == (tmp_path / "task").resolve()


def test_the_demo_config_is_a_valid_scripted_run(tmp_path: Path) -> None:
    raw = demo_config("demo-x", "http://127.0.0.1:1", tmp_path / "t", agents=3, minutes=2, rounds=True)
    cfg = RunConfig.model_validate(raw)
    assert cfg.backends() == {"fake"} and cfg.node is not None and cfg.node.is_local
    assert cfg.opening_round is not None and cfg.n_agents == 3
