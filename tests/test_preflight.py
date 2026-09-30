"""Preflight fails closed on every missing piece, after checking the Korax pin."""

from __future__ import annotations

from pathlib import Path

import pytest

from stigmergeia import cli, korax_pin
from stigmergeia.config import RunConfig


def cfg(tmp: Path, **kw: object) -> RunConfig:
    return RunConfig.model_validate({
        "run_name": "pf", "task_dir": str(tmp), "n_agents": 1, "per_agent_budget_usd": 1, "total_budget_usd": 1,
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "t")}, "gate": {}, **kw})


@pytest.fixture(autouse=True)
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(korax_pin, "korax_state", lambda: (korax_pin.PINNED_COMMIT, False))


def test_a_shell_without_the_sandbox_tools_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda t: None if t == "socat" else f"/usr/bin/{t}")
    with pytest.raises(RuntimeError, match="socat"):
        cli.preflight(cfg(tmp_path, shell=True))


def test_a_missing_korax_cli_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda t: f"/usr/bin/{t}")
    with pytest.raises(RuntimeError, match="no korax CLI"):
        cli.preflight(cfg(tmp_path, shell=True, korax_cli_bin=str(tmp_path)))


def test_an_unpinned_korax_is_refused_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(korax_pin, "korax_state", lambda: ("0" * 40, False))
    monkeypatch.delenv(korax_pin.OVERRIDE_ENV, raising=False)
    with pytest.raises(korax_pin.KoraxPinError):
        cli.preflight(cfg(tmp_path, shell=False))


def test_a_clean_setup_returns_the_korax_commit(tmp_path: Path) -> None:
    assert cli.preflight(cfg(tmp_path, shell=False)) == korax_pin.PINNED_COMMIT
