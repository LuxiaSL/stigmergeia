"""The canon and the prompt templates, with this run's facts filled in.

A few sentences agents read state a fact of the run's configuration: how many
lab cores each agent has, how long a lab run goes before it moves to the
background, how many background runs may share those cores. Those sentences
carry `{{name}}` placeholders in canon/ and stigmergeia/prompts/, and
`render` fills them from the config, so the text is true for whatever the
config says. Only structural facts are offered: nothing here names the run's
duration, a budget or a time left (CONTRIBUTING.md: limits stay hidden), and
`render` refuses a placeholder it does not know rather than posting it raw.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import RunConfig
from .profile import REPO_ROOT

CANON_DIR = REPO_ROOT / "canon"
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]


def _word(n: int) -> str:
    return WORDS[n] if 0 <= n < len(WORDS) else str(n)


def _duration(seconds: float) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        m = int(seconds // 60)
        return f"{_word(m)} minute{'s' if m != 1 else ''}"
    return f"{int(seconds)} seconds"


def facts(cfg: RunConfig) -> dict[str, str]:
    """The configuration facts the canon may state, as the words it states them in."""
    node = cfg.node
    cores = node.cores_per_agent if node else 4
    after = node.run_background_after_s if node else None
    runs = node.max_background_runs if node else 2
    return {
        "lab_cores": _word(cores),
        "lab_cores_plural": "s" if cores != 1 else "",
        "background_after": _duration(after) if after else "a few minutes",
        "background_runs": _word(runs).capitalize(),
    }


def render(text: str, cfg: RunConfig, where: str = "text") -> str:
    known = facts(cfg)
    unknown = sorted({m for m in PLACEHOLDER.findall(text) if m not in known})
    if unknown:
        raise ValueError(f"{where}: unknown canon placeholder(s) {unknown}; known: {sorted(known)}")
    return PLACEHOLDER.sub(lambda m: known[m.group(1)], text)


def render_canon(cfg: RunConfig, dest: Path, source: Path = CANON_DIR) -> Path:
    """Every canon document, rendered for this run, written to `dest` (which
    bootstrap then posts). Returns `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    docs = sorted(source.glob("*.md"))
    if not docs:
        raise FileNotFoundError(f"{source}: no canon documents (*.md)")
    for doc in docs:
        (dest / doc.name).write_text(render(doc.read_text(), cfg, str(doc)))
    return dest
