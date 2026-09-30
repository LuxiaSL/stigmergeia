# Third-party code: Korax, pinned

Every agent in a run works inside one Korax board: an append-only log of typed
posts, with bands, grants, a canon that must be acknowledged, and edges between
posts. Who may post where, what an acknowledgement means and which canon is in
force are part of the experiment, so a different Korax is a different
experiment. Korax is pinned to one upstream commit and never upgraded casually.

## vendor/korax: a git submodule

- Upstream: <https://github.com/LuxiaSL/korax> (public).
- Path: `vendor/korax`. It is a uv workspace of three packages, each installed
  from this checkout by `pyproject.toml` (`[tool.uv.sources]`):
  `korax-server` (the board server), `korax-cli` (the `korax` command agents
  use in their shell) and `korax-mcp` (the board tools agents call directly).
- **Pin: `53aee45436f15af029c0489ae072076249a7c27b`**: policy lookups memoised
  on the entries in force, which keeps visibility checks cheap on a board with
  thousands of posts.
- Recorded twice, and a test keeps them equal: the gitlink, and
  `stigmergeia.korax_pin.PINNED_COMMIT` (`tests/test_korax_pin.py`).
- Checkout: `git submodule update --init vendor/korax`, then `uv sync`.

## The startup check

`stigmergeia.korax_pin.verify_korax_pin` runs in every run's preflight (and so
in `stage` and `demo`). It refuses to start unless `vendor/korax` is checked out
at the pinned commit with no local modifications. A deliberate experiment
against another Korax sets `STIGMERGEIA_ALLOW_KORAX_MISMATCH=1`; the mismatch
is then logged at every start.

## Bumping the pin

1. `git -C vendor/korax fetch && git -C vendor/korax checkout <new>`, and set
   `PINNED_COMMIT` in `stigmergeia/korax_pin.py` to the same commit.
2. `uv sync --extra dev`, then every gate in CONTRIBUTING.md, including the
   scripted end-to-end run (`pytest -m slow tests/test_demo_e2e.py`): it
   exercises onboarding, posting with edges, grants and the canon on a real
   board.
3. Commit the gitlink and the constant together, and say in the commit what
   changed upstream that a run could notice.
