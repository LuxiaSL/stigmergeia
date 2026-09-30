"""The corpus the documentation checkers are held to: strings each must catch, and must not.

Both documentation checkers were written by reading the code they police, and
reading is how a checker acquires a blind spot: a rule that covers `#` comments
looks complete until somebody puts the same sentence in a docstring, and a rule
keyed to the shape of one document name looks complete until a document is named
some other way. Nothing in a source tree fails when a gate stops seeing — the
verdict still says PASS.

So the rules are pinned by a corpus instead. Every case below is a string of the
kind this codebase actually writes, on the surfaces prose lands on: a comment, a
docstring, and the message a `raise` or a logging call says out loud. Each one
carries the rule set it must produce, and a large share of the corpus carries the
empty set — legitimate prose that must stay quiet, because a gate that cries wolf
is a gate the next contributor switches off. The two checkers are run over every
case together, since their rule names are distinct and a violation belongs to
whichever one owns it.

Names of people, machines and schedulers in the cases are neutral stand-ins
(`Robin`, `jobrunner`): the infrastructure rule is written against the shape such
prose takes, never against a list of real names, and the corpus keeps it that way.

Adding a case is how a blind spot gets closed: write the string that slipped
through, give it the rule it should have produced, and the gate is pinned there
from then on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tools import check_referents, check_timelessness

CITATION = check_referents.CITATION_RULE
DEFERRAL = check_referents.DEFERRAL_RULE
INFRA = check_referents.INFRA_RULE
MODULE = check_referents.MODULE_RULE
PATH = check_referents.PATH_RULE
PRIVATE = check_referents.PRIVATE_RULE
DATED = check_timelessness.DATED_RULE
MARKER = check_timelessness.MARKER_RULE
USED_TO = check_timelessness.USED_TO_RULE
PRIOR_STATE = "changelog-prior-state"
CHANGED_IN = "changelog-changed-in"
WE_NOW = "changelog-we-now"
NO_LONGER = "changelog-no-longer"

#: The surfaces a case can be written on. `data` is the control: the same sentence
#: as a value in a payload, which no rule may read.
SURFACES = ("comment", "docstring", "raise", "log", "data")


@dataclass(frozen=True)
class Case:
    """One string, where it sits, and the rules it must produce there."""

    text: str
    surface: str
    rules: frozenset[str] = field(default_factory=frozenset)
    #: True when the line carries a date the allowlist exempts rather than no date.
    exempt: bool = False

    def source(self) -> str:
        """The smallest module that puts `text` on `surface`."""
        # `ensure_ascii=False` so a section mark or a box-drawing rule reaches the
        # generated line as itself rather than as an escape the rules cannot see.
        literal = json.dumps(self.text, ensure_ascii=False)
        if self.surface == "comment":
            return f"# {self.text}\nVALUE = 1\n"
        if self.surface == "docstring":
            return f"{literal}\nVALUE = 1\n"
        if self.surface == "raise":
            return (
                "def run(count: int) -> None:\n"
                "    if not count:\n"
                f"        raise ValueError({literal})\n"
            )
        if self.surface == "log":
            return (
                "import logging\n\n"
                "LOGGER = logging.getLogger(__name__)\n\n\n"
                "def run() -> None:\n"
                f"    LOGGER.warning({literal})\n"
            )
        if self.surface == "data":
            return f"PAYLOAD = {{'note': {literal}}}\n"
        raise AssertionError(f"unknown surface {self.surface!r}; expected one of {SURFACES}")

    def label(self) -> str:
        return f"{self.surface}:{self.text[:60]}"


MUST_CATCH: tuple[Case, ...] = (
    # A date in prose, on each of the three surfaces.
    Case("measured 2026-07-12, n=80", "comment", frozenset({DATED})),
    Case("verified on transformers 5.3.0, 2026-07-27", "comment", frozenset({DATED})),
    Case("Slimmed at bootstrap (2026-07-26) from the sibling pipeline.", "docstring", frozenset({DATED})),
    Case("the corpus was rebuilt 2026-07-18", "raise", frozenset({DATED})),
    Case("calibration banked 2026-07-12", "log", frozenset({DATED})),
    # Section marks: a charter clause, a pre-registration clause, a brief clause.
    Case("UNSTAMPED (C§8): nothing here scores a bar.", "docstring", frozenset({CITATION})),
    Case("Mechanics only — the verdict belongs elsewhere (C§8).", "raise", frozenset({CITATION})),
    Case("one analysis template per model (prereg §6-I1)", "docstring", frozenset({CITATION})),
    Case("The receptacle rule is stated in §4 of the brief.", "docstring", frozenset({CITATION})),
    Case("the frozen halving (§2.3) is never recomputed", "log", frozenset({CITATION})),
    # Rakes: the numbered lesson, cited with and without its list's name.
    Case("Duplicate names HALT (rake M18).", "comment", frozenset({CITATION})),
    Case("a negative peak is refused and named as Rake M29", "raise", frozenset({CITATION})),
    Case("TORCH IS OPTIONAL BY DESIGN (M44).", "docstring", frozenset({CITATION})),
    # Rulings, and decisions credited to whoever made them.
    Case("the flat multiplier stands by desk ruling", "comment", frozenset({CITATION})),
    Case("RULED BY ROBIN 2026-07-29 (session-5 close)", "comment", frozenset({CITATION, DATED})),
    Case("the fit grids were ratified by Robin", "comment", frozenset({CITATION})),
    Case(
        "By Robin's ruling of 2026-08-01 the thread count is eight.",
        "docstring",
        frozenset({CITATION, DATED}),
    ),
    Case("the thread default of record (Robin ruling)", "log", frozenset({CITATION})),
    # The roles and records of a private working process.
    Case("the enactor never adjudicates a live halt", "docstring", frozenset({CITATION})),
    Case("the ledger records 59 overlapping bodies", "raise", frozenset({CITATION})),
    Case("the sigma at this site is not banked (baton item)", "comment", frozenset({CITATION})),
    Case("the progress bars the brief asks for", "docstring", frozenset({CITATION})),
    Case("The node-side engine (BRIEF item 2).", "docstring", frozenset({CITATION, INFRA})),
    Case("a pick is never coerced (brief requirement 3)", "comment", frozenset({CITATION})),
    Case("the de dicto probe (session-8)", "docstring", frozenset({CITATION})),
    Case("re-freeze #4 leaves these modules byte-unchanged", "comment", frozenset({CITATION})),
    # Phase, arm, vector and leg codes of the research plan.
    Case("A8 Leg-3 — build the injection banks.", "docstring", frozenset({CITATION})),
    Case("nothing here scores P8-2d", "docstring", frozenset({CITATION})),
    Case("growth for P2 adds entries here", "comment", frozenset({CITATION})),
    Case("transported source axes for V7 and dir0", "docstring", frozenset({CITATION})),
    Case("The floor is the one arm A9 reported.", "docstring", frozenset({CITATION})),
    Case("the how-axis directions, spec P-B", "docstring", frozenset({CITATION})),
    Case("the zero-denominator guard of item 14e", "raise", frozenset({CITATION})),
    Case("measured on `a1b2c3d` in the node venv", "comment", frozenset({CITATION})),
    Case("kept per the addendum", "comment", frozenset({CITATION})),
    Case("the codicil that followed narrows it", "comment", frozenset({CITATION})),
    Case("the row the journal entry records", "comment", frozenset({CITATION})),
    # Working trees `.gitignore` keeps out, and documents filed outside the tree.
    Case(
        "the specs live in the gitignored desk/configs/ tree",
        "docstring",
        frozenset({PRIVATE, CITATION}),
    ),
    Case("`desk/` is gitignored and therefore empty.", "comment", frozenset({PRIVATE, CITATION})),
    Case("See research/notes/v3-delta-memo for the revision.", "comment", frozenset({PRIVATE})),
    Case(
        "authorized by desk/briefs/class-probe.md section 1",
        "docstring",
        frozenset({PRIVATE, CITATION}),
    ),
    Case("Known traps (REPORT-dsv3-fp8-lane-design section 5):", "docstring", frozenset({PRIVATE})),
    Case(
        "PRESTATEMENT-dose-window-2026-08-08.md fixes the window.",
        "docstring",
        frozenset({PRIVATE, DATED}),
    ),
    Case(
        "the draw reads DESK-SCORE-L2-2026-08-06.json first",
        "raise",
        frozenset({PRIVATE, CITATION, DATED}),
    ),
    Case("the probe log is /scratch/run-logs/sibling-arch.log", "comment", frozenset({PRIVATE})),
    Case("falling back to stigmergeia/analysis/absent.py", "log", frozenset({PATH})),
    Case("The loader is tools.check_referents.AbsentLoader.", "docstring", frozenset({MODULE})),
    # Infrastructure: the shape of prose that presupposes one operator's machines.
    Case("The `jobrunner submit` line for a rendered job.", "docstring", frozenset({INFRA})),
    Case("the coordinator HTTP API is read-only verification", "raise", frozenset({INFRA})),
    Case("Three node-side build scripts carried a heuristic.", "comment", frozenset({INFRA})),
    Case("a missing weight fails on our cluster rather than here", "raise", frozenset({INFRA})),
    # Run codes and board envelope ids are citations of records kept outside this tree.
    Case("canon-5 left 28 jail processes running", "comment", frozenset({CITATION})),
    Case("the round (as in solo-1) has one agent", "docstring", frozenset({CITATION})),
    Case("corrected by the gate's WARN #554", "comment", frozenset({CITATION})),
    Case("the canon8-base settings apply", "log", frozenset({CITATION})),
    Case("the inference was retracted after a cross-node probe", "comment", frozenset({INFRA})),
    Case("a fixed weights root on the collection node", "docstring", frozenset({INFRA})),
    # Deferrals to a conversation, or to a session on somebody's calendar.
    Case("Kept for the reason discussed when this was ruled on.", "comment", frozenset({DEFERRAL})),
    Case(
        "rows from two machines must not be joined; see the earlier ruling",
        "raise",
        frozenset({DEFERRAL, CITATION}),
    ),
    Case("landing outcomes are scored next session", "comment", frozenset({DEFERRAL})),
    # Changelog phrasing, one case per phrase, and a marker comment.
    Case("This module used to carry its own copy.", "comment", frozenset({USED_TO})),
    Case("fallback: previously an unconditional AttributeError", "comment", frozenset({PRIOR_STATE})),
    Case("the split is no longer identical across ranks", "comment", frozenset({NO_LONGER})),
    Case("we now cache the matrix", "docstring", frozenset({WE_NOW})),
    Case("the bound changed in the second pass", "comment", frozenset({CHANGED_IN})),
    Case("TODO: tighten this", "comment", frozenset({MARKER})),
)

MUST_NOT_CATCH: tuple[Case, ...] = (
    # The constraint and the reason: what the prose style asks for.
    Case(
        "A proc fit with an isotropic scale is an isometry on span(va), so cosines "
        "between transported vectors equal cosines between their in-frame projections.",
        "comment",
    ),
    Case("Section 9 reads the generated text, so a corpus without it fails first.", "docstring"),
    # Shouted status labels and emphases: the same shape as a document name, no date,
    # no extension, no sentence in the tail.
    Case("NO OFF-BY-ONE IS INTRODUCED here.", "comment"),
    Case("the other half is reported UNSCORED-NO-FIT, a resolution gap", "docstring"),
    Case("THE BELONGS-TO-THIS-BASIS GATE: the manifest sha equals the loaded one.", "docstring"),
    Case("compared BYTE-FOR-BYTE against the banked anchor", "comment"),
    Case("The ANCHOR-versus-RECENCY contrast is the routing echo of the how-axis.", "comment"),
    # Shapes the code rules guard against: model names, row ids, format specs,
    # quantities and percentiles.
    Case("DeepSeek-V3 and FP8 checkpoints load through the same path.", "comment"),
    Case("prompt ids P000..P079 align one to one with the cells", "docstring"),
    Case("Latency is quoted at P95.", "comment"),
    Case("the seed material ends member{index:02d}", "docstring"),
    Case("the separation is ~12x, so a run that misses it fails loudly", "comment"),
    Case("about 30k positions feed the reference covariance", "comment"),
    Case("A floor of 1e-12 keeps the ratio finite.", "docstring"),
    Case("The 8b preset and the 3b preset decode at a different nucleus mass.", "comment"),
    Case("gemma3-27b and dsv2-lite sample at 0.95.", "comment"),
    Case("It builds the section 4.1 cell plan and resolves the vectors.", "docstring"),
    # Identifiers the allowlist exists for: a hub model id, the output layout, a
    # kernel interface, a third-party source file, a checkpoint file, a module here.
    Case("Presets cover meta-llama/Llama-3.1-8B-Instruct and Qwen/Qwen2.5-7B.", "comment"),
    Case("Reads runs/<run>/private/<agent>/transcript.jsonl as it grows.", "docstring"),
    # "The node" is this harness's own vocabulary: the compute machine a config names.
    Case("a missing weight fails on the node rather than here", "raise"),
    # The board's canon is a concept, not a run code; a padded section number is prose.
    Case("the canon an agent must ack before its first lab call", "comment"),
    Case("Linux-only by construction (/proc/self/maps).", "docstring"),
    Case("/dev/shm is used only when explicitly requested", "comment"),
    Case("The memo it relies on is in korax/policy.py.", "comment"),
    Case("The tree index is tools.check_referents.TreeIndex.", "docstring"),
    Case("Vendored verbatim from the anamnesis extraction stack.", "docstring"),
    # Ordinary words that a citation or an infrastructure pattern also uses.
    Case("The spec a cell was generated under, read from its own run metadata.", "docstring"),
    Case("Keep a brief record of every refusal.", "comment"),
    Case("The node in the tree has two children.", "comment"),
    Case("the `submit` subcommand prints the line and runs nothing", "docstring"),
    Case("The staging area is created by the collection step.", "comment"),
    Case("Published as a TABLE over alpha, BH-FDR-corrected at the per-test rate.", "docstring"),
    # The message a run writes for an operator, with the substance stated inline.
    Case(
        "eos ids are model-specific and must be passed explicitly; an empty list "
        "would let generation run to the length cap",
        "raise",
    ),
    # The same sentences as data: a payload value is not a claim.
    Case("the reason is in staging/some-notes.md, ruled by the desk 2026-07-12", "data"),
    Case("UNSTAMPED (C§8), rake M18, session-5, P8-2, the `jobrunner submit` line", "data"),
    Case("we now cache the matrix; the split is no longer identical", "data"),
)

EXEMPTED: tuple[Case, ...] = (
    # The one date exemption: the date is part of the address.
    Case("The frozen record is https://example.com/2026-07-12/stigmergeia.", "comment", exempt=True),
)

ALL_CASES = MUST_CATCH + MUST_NOT_CATCH + EXEMPTED


@pytest.fixture(scope="module")
def index() -> check_referents.TreeIndex:
    """One index of this repository, shared by every case."""
    return check_referents.TreeIndex.build(check_referents.DEFAULT_REPO)


@pytest.fixture(scope="module")
def referent_allowlist() -> check_referents.Allowlist:
    return check_referents.load_allowlist(check_referents.DEFAULT_ALLOWLIST)


@pytest.fixture(scope="module")
def date_allowlist() -> list[re.Pattern[str]]:
    return check_timelessness.load_allowlist(check_timelessness.DEFAULT_ALLOWLIST)


def run_source(
    source: str,
    index: check_referents.TreeIndex,
    referent_allowlist: check_referents.Allowlist,
    date_allowlist: list[re.Pattern[str]],
) -> tuple[set[str], int]:
    """Both checkers over one module's source; returns (rules fired, dates exempted)."""
    path = Path("case.py")
    timeless, exempted = check_timelessness.scan_source(path, source, date_allowlist)
    referents = check_referents.scan_source(path, source, index, referent_allowlist)
    return {v.rule for v in timeless} | {v.rule for v in referents}, len(exempted)


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.label() for c in ALL_CASES])
def test_the_gates_produce_exactly_the_rules_the_corpus_states(
    case: Case,
    index: check_referents.TreeIndex,
    referent_allowlist: check_referents.Allowlist,
    date_allowlist: list[re.Pattern[str]],
) -> None:
    fired, exempted = run_source(case.source(), index, referent_allowlist, date_allowlist)
    assert fired == set(case.rules), f"{case.surface}: {case.text!r}"
    assert exempted == (1 if case.exempt else 0)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The values substituted into a logging template are data: the filename a
        # run writes is not a file a reader is sent to open.
        (
            "import logging\nLOG = logging.getLogger(__name__)\nOUT = None\n\n\n"
            "def run() -> None:\n    LOG.info('wrote %s', OUT / 'star_systems.json')\n",
            set(),
        ),
        (
            "import logging\nLOG = logging.getLogger(__name__)\n\n\n"
            "def run() -> None:\n    LOG.log(20, 'filed %s', 'desk/some-tree/')\n",
            set(),
        ),
        # The template itself is prose, whichever position the level puts it in.
        (
            "import logging\nLOG = logging.getLogger(__name__)\n\n\n"
            "def run() -> None:\n    LOG.log(20, 'see desk/some-tree/ for %s', 'x')\n",
            {PRIVATE, CITATION},
        ),
        # A `%` template is prose on its left and data on its right.
        (
            "def run(count: int) -> None:\n"
            "    if not count:\n"
            "        raise ValueError('refused (rake M18): %s' % 'staging/x/')\n",
            {CITATION},
        ),
    ],
    ids=["log-value-filename", "log-level-value-path", "log-level-template", "raise-mod-template"],
)
def test_a_message_is_its_template_and_not_its_values(
    source: str,
    expected: set[str],
    index: check_referents.TreeIndex,
    referent_allowlist: check_referents.Allowlist,
    date_allowlist: list[re.Pattern[str]],
) -> None:
    fired, _ = run_source(source, index, referent_allowlist, date_allowlist)
    assert fired == expected


def test_the_corpus_covers_every_rule_and_every_surface() -> None:
    """A rule with no case behind it, or a surface with none, is the next blind spot."""
    covered = {rule for case in MUST_CATCH for rule in case.rules}
    expected = set(check_referents.rule_names()) | set(check_timelessness.rule_names())
    assert expected - covered == set(), expected - covered
    assert {case.surface for case in ALL_CASES} == set(SURFACES)


def test_every_case_is_written_on_a_surface_the_corpus_knows() -> None:
    for case in ALL_CASES:
        assert case.surface in SURFACES
        assert case.source()
