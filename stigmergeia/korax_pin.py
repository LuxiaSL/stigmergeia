"""The Korax this harness is built against, pinned by commit.

The board's semantics (who may post where, what an ack means, which canon is
in force) are the protocol every agent works inside, so a different Korax is
a different experiment. The pin is recorded twice: the git submodule at
vendor/korax, and PINNED_COMMIT below. tests/test_korax_pin.py keeps the two
equal, and `verify_korax_pin` refuses to start a run whose Korax checkout is
at another commit or carries local modifications. VENDOR.md says how to bump it.

STIGMERGEIA_ALLOW_KORAX_MISMATCH=1 lets a deliberate experiment run against
another Korax; the mismatch is then logged at every start, never silent.
"""

from __future__ import annotations

import logging
import os
import subprocess

from .profile import REPO_ROOT

PINNED_COMMIT = "e81903bf87f51b9db2eff338a3c04f6e58454ae9"
KORAX_DIR = REPO_ROOT / "vendor" / "korax"
OVERRIDE_ENV = "STIGMERGEIA_ALLOW_KORAX_MISMATCH"

log = logging.getLogger("stigmergeia")


class KoraxPinError(RuntimeError):
    """The Korax checkout is not the pinned commit, or has local changes."""


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(KORAX_DIR), *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise KoraxPinError(f"git {' '.join(args)} in {KORAX_DIR} failed: {r.stderr.strip()[:300]} "
                            "(check out the submodule: git submodule update --init vendor/korax)")
    return r.stdout.strip()


def korax_state() -> tuple[str, bool]:
    """(commit, dirty) of the vendored Korax checkout."""
    if not (KORAX_DIR / ".git").exists():
        raise KoraxPinError(f"{KORAX_DIR} is not checked out: git submodule update --init vendor/korax")
    return _git("rev-parse", "HEAD"), bool(_git("status", "--porcelain", "--untracked-files=no"))


def verify_korax_pin() -> str:
    """The pinned commit, after checking the checkout is exactly it. Raises
    KoraxPinError otherwise, unless the override is set (then logs and returns
    the commit actually in use)."""
    head, dirty = korax_state()
    problem = None
    if head != PINNED_COMMIT:
        problem = f"vendor/korax is at {head[:12]}, not the pinned {PINNED_COMMIT[:12]}"
    elif dirty:
        problem = "vendor/korax has local modifications"
    if problem is None:
        return head
    if os.environ.get(OVERRIDE_ENV) == "1":
        log.warning("KORAX PIN OVERRIDDEN (%s=1): %s; this run is not on the pinned board", OVERRIDE_ENV, problem)
        return head
    raise KoraxPinError(f"{problem}. Run `git submodule update vendor/korax`, or set {OVERRIDE_ENV}=1 for a "
                        "deliberate experiment against another Korax")
