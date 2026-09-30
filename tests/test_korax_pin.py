"""The Korax pin is recorded twice, and the two records agree."""

from __future__ import annotations

import subprocess

import pytest

from stigmergeia import korax_pin
from stigmergeia.profile import REPO_ROOT


def _gitlink() -> str | None:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-tree", "HEAD", "vendor/korax"],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return None
    mode, kind, sha = r.stdout.split()[:3]
    assert (mode, kind) == ("160000", "commit"), "vendor/korax must be a submodule"
    return sha


def test_the_gitlink_and_the_constant_name_the_same_commit() -> None:
    link = _gitlink()
    if link is None:
        pytest.skip("not a git checkout with vendor/korax committed")
    assert link == korax_pin.PINNED_COMMIT


def test_a_wrong_commit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(korax_pin, "korax_state", lambda: ("0" * 40, False))
    monkeypatch.delenv(korax_pin.OVERRIDE_ENV, raising=False)
    with pytest.raises(korax_pin.KoraxPinError, match="not the pinned"):
        korax_pin.verify_korax_pin()


def test_local_modifications_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(korax_pin, "korax_state", lambda: (korax_pin.PINNED_COMMIT, True))
    monkeypatch.delenv(korax_pin.OVERRIDE_ENV, raising=False)
    with pytest.raises(korax_pin.KoraxPinError, match="local modifications"):
        korax_pin.verify_korax_pin()


def test_the_override_logs_and_proceeds(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(korax_pin, "korax_state", lambda: ("1" * 40, False))
    monkeypatch.setenv(korax_pin.OVERRIDE_ENV, "1")
    assert korax_pin.verify_korax_pin() == "1" * 40
    assert "KORAX PIN OVERRIDDEN" in caplog.text
