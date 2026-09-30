"""The corpus tools/check_private_refs.py must catch, and the text it must pass.

A shape that stops matching fails here instead of quietly reporting a pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import check_private_refs as cpr

SHAPES = cpr.shape_patterns()

MUST_CATCH = [
    ("the workspace sits at /home/someone/work/run", "abs-path"),
    ("run roots live on /mnt/scratchfs/runs", "abs-path"),
    ("canon-7 herded, then flipped", "run-code"),
    ("see codex-t3 for the luna numbers", "run-code"),
    ("copied from canon9-base.yaml", "run-code"),
    ("the gate corrected #553 with a WARN", "envelope-id"),
]

MUST_PASS = [
    "copy it to /home/<user>/profile.toml",
    "the jail masks /home/you/.ssh",
    "an opening round with answers in turn",
    "the canon has two documents",
    "color: #fff; border: #1a2b3c",
    "agents a00 through a09",
    "/proc/self/maps and /dev/shm",
]


@pytest.mark.parametrize("text,kind", MUST_CATCH)
def test_a_private_shape_is_caught(text: str, kind: str) -> None:
    kinds = {k for k, rx in SHAPES if rx.search(text)}
    assert kind in kinds, f"{text!r} fired {kinds}"


@pytest.mark.parametrize("text", MUST_PASS)
def test_ordinary_text_passes(text: str) -> None:
    fired = [(k, rx.search(text).group(0)) for k, rx in SHAPES if rx.search(text)]
    assert not fired, f"{text!r} fired {fired}"


def test_profile_values_are_read_and_short_values_dropped(tmp_path: Path) -> None:
    prof = tmp_path / "profile.toml"
    prof.write_text('[node]\nhost = "computebox"\nharness_dir = "/srv/h"\nrun_root = "/srv/runs"\n'
                    'venv = "/srv/venv"\ncores = "0-63"\n[local]\ncpus = "0-7"\n')
    vals = cpr.profile_values(prof)
    assert "computebox" in vals and "/srv/runs" in vals
    assert "0-63" not in vals and "0-7" not in vals
    assert str(Path.home()) in vals


def test_a_missing_profile_checks_shapes_only(tmp_path: Path) -> None:
    assert cpr.profile_values(tmp_path / "absent.toml") == []
