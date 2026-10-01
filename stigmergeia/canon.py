"""The canon and the prompt templates, with this run's facts filled in.

A few sentences agents read state a fact of the run's configuration: how many
lab cores each agent has, how long a lab run goes before it moves to the
background, how many background runs may share those cores. Those sentences
carry `{{name}}` placeholders in canon/ and stigmergeia/prompts/, and
`render` fills them from the config, so the text is true for whatever the
config says. Only structural facts are offered: nothing here names the run's
duration, a budget or a time left (CONTRIBUTING.md: limits stay hidden), and
`render` refuses a placeholder it does not know rather than posting it raw.

The rest name the task: what a submission is called, how `score` and
`submit` judge it, the side quests. Their defaults (the snake task's words)
are canon/vocabulary.toml; a task overrides any of them in its own
tasks/<name>/canon.toml. A key the defaults don't have is refused, so a typo
cannot silently leave the snake wording in another task's canon.
"""

from __future__ import annotations

import re
import tomllib
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


VOCABULARY = CANON_DIR / "vocabulary.toml"


def vocabulary(cfg: RunConfig) -> dict[str, str]:
    """The task's words: the defaults, with the task's own canon.toml over them."""
    words = tomllib.loads(VOCABULARY.read_text())
    own = cfg.task_dir / "canon.toml"
    if own.is_file():
        theirs = tomllib.loads(own.read_text())
        unknown = sorted(set(theirs) - set(words))
        if unknown:
            raise ValueError(f"{own}: unknown vocabulary key(s) {unknown}; known: {sorted(words)}")
        bad = sorted(k for k, v in theirs.items() if not isinstance(v, str))
        if bad:
            raise ValueError(f"{own}: vocabulary values must be strings: {bad}")
        words.update(theirs)
    return words


def facts(cfg: RunConfig) -> dict[str, str]:
    """The configuration facts the canon may state, as the words it states them in."""
    node = cfg.node
    cores = node.cores_per_agent if node else 4
    after = node.run_background_after_s if node else None
    runs = node.max_background_runs if node else 2
    return {
        **vocabulary(cfg),
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
