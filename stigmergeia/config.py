"""Run configuration: one YAML file describes a whole swarm run.

Every agent in a run gets the SAME system prompt, the same brief, the same
tools and the same limits. The only per-agent differences are its name, its
board identity, its workspace and its CPU cores. That is the experiment's
control: whatever differs in behaviour came from the agents, not the setup.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .profile import REPO_ROOT, Profile, load_profile

Effort = Literal["low", "medium", "high", "xhigh", "max"]  # Claude and Codex both accept these


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoardConfig(_Strict):
    url: str = Field(description="swarm board base URL, e.g. http://127.0.0.1:7440")
    operator_token_file: Path = Field(description="file holding the swarm board's operator token")
    ns: str | None = Field(default=None, description="agents' namespace; default /swarm/<run_name>")
    gate_ns: str | None = Field(default=None, description="gate results; default <ns>/gate")

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("board.url must be http(s)")
        return v.rstrip("/")


class OpeningRoundConfig(_Strict):
    reveal_after_s: float = Field(default=600, description="reveal whatever is in after this many seconds from "
                                                           "the start, even if some agent has not proposed")
    answer_turn_s: float = Field(default=90, gt=0, description="answers go one agent at a time, in agent index "
                                 "order; an agent's turn passes to the next after this many seconds without its "
                                 "answer (it may still answer later)")


class NodeConfig(_Strict):
    """Where agent code actually executes. Omit to run with no node tools.

    host, run_root, harness_dir and venv name one operator's machines, so a
    config leaves them out and `load_config` fills them from the local profile
    (`stigmergeia.profile`); run_root becomes <profile run_root>/<run_name>.
    A config that sets one explicitly wins, which is how a test points a run
    at a temporary directory."""

    host: str = Field(description="ssh host alias, or 'local' to run node commands on this machine")
    run_root: str = Field(description="remote dir holding one workspace per agent")
    harness_dir: str = Field(description="remote dir with jail.py and the task files")
    venv: str = Field(description="remote venv exposed read-only inside the jail")
    cores_per_agent: int = Field(default=4, ge=1)
    first_core: int = Field(default=0, ge=0, description="agent i gets cores first_core + i*cores_per_agent ...; "
                            "default: the first core of the profile's node core range")
    data_root: str | None = Field(default=None, description="task data on the node (e.g. a corpus); exported to "
                                  "every node command as STIGMERGEIA_DATA_ROOT and usable in jail_template as "
                                  "{data_root}. Default: the profile's")
    mem: str = "4G"
    tasks: int = 256
    run_timeout_s: int = Field(default=600, ge=5, description="hard wall clock per node_run")
    run_background_after_s: float | None = Field(
        default=180, gt=0, description="a foreground `run`/`score` still running after this many seconds moves to "
        "the background (the call returns a job id; the output is delivered later), and a `run` asking for a "
        "timeout_s above it starts in the background. None: runs are always synchronous")
    background_run_timeout_s: int = Field(
        default=1800, ge=5, description="wall cap for ONE background `run` (a foreground call is capped by "
                                        "run_timeout_s). Agents ask for runs of half an hour and more, and a cap they "
                                        "cannot see ends such a run with an empty log")
    max_background_runs: int = Field(
        default=2, ge=1, description="background `run`s one agent may have going at once. They SHARE the agent's "
        "cores (the OS divides them; a split fixed at start would halve a lone job for nothing). Held-out batches "
        "never share: a submit made while runs are going is QUEUED (snapshot frozen at once) and starts, with the "
        "cores to itself, when they finish")
    wait_cap_s: float = Field(
        default=300, gt=0, description="the lab's `wait` returns when one of the agent's background jobs finishes, "
        "or board news for it arrives, or after this many seconds with neither (never shown as a limit)")
    shared_run_timeout_s: int = Field(
        default=120, ge=5, description="while a background run holds an agent's cores, a foreground run or score "
                                       "may share them, capped at this many seconds (a light check, not a second "
                                       "heavy job)")
    jail_template: str = Field(
        default=(
            "{python} {harness_dir}/jail.py --ws {ws} --ro {siblings} --ro {task_dir} "
            "--venv {venv} --cores {cores} --mem {mem} --tasks {tasks} --timeout {timeout} "
            "--torch-ipc copy -- "
        ),
        description="command prefix that puts a command inside the jail; placeholders are filled per call",
    )
    python: str | None = Field(default=None, description="remote interpreter; default <venv>/bin/python")

    @property
    def is_local(self) -> bool:
        return self.host == "local"

    def shell(self, script: str) -> list[str]:
        """argv running `script` in a shell on the node: over ssh, or on this
        machine when host is 'local'."""
        if self.data_root:
            script = f"export STIGMERGEIA_DATA_ROOT={shlex.quote(self.data_root)}; {script}"
        return ["bash", "-c", script] if self.is_local else ["ssh", self.host, script]

    def remote(self, path: str) -> str:
        """`path` on the node, as rsync names it."""
        return path if self.is_local else f"{self.host}:{path}"

    @property
    def remote_python(self) -> str:
        """The venv's interpreter unless overridden: node rules say Python on
        the node runs from the venv, and the gate's policy child inherits
        this interpreter, so jailed policies can import the venv's packages."""
        return self.python or f"{self.venv}/bin/python"


class GateConfig(_Strict):
    train_episodes: int = Field(default=100, ge=1)
    heldout_episodes: int = Field(default=500, ge=1)
    episode_cpu_s: float | None = Field(
        default=20.0, gt=0, description="policy-time budget per episode (the gate's --episode-cpu); None: not "
                                        "passed, for a gate without per-episode budgets")
    higher_is_better: bool = Field(
        default=True, description="direction of the gate's score. False (e.g. bits per byte): a record is the "
                                  "LOWEST confirmed score, and significance needs the CI's upper end below it")
    heldout_timeout_s: int | None = Field(
        default=None, ge=60, description="wall cap for ONE held-out gate call (and each jail inside it); default "
                                         "heldout_episodes x episode_cpu_s / cores + 120 (the snake formula)")
    run_sd: float | None = Field(
        default=None, ge=0, description="each held-out batch is ONE independent training run: the pooled CI uses "
                                        "this between-run sd (or the observed sd of batch means, if larger) over "
                                        "the number of runs, plus the within-batch sampling error. None: batches "
                                        "are pooled as episodes (snake)")
    digest: Literal["file", "dir"] = Field(
        default="file", description="what identifies 'the same submission' for pooling: the submitted file, or "
                                    "every *.py under its directory (dot-dirs excluded), for multi-file submissions")
    heldout_secret: Path | None = Field(
        default=None, description="LOCAL secret file; default <run_dir>/heldout.secret, generated at provision")
    submit_cooldown_s: int = Field(default=0, ge=0, description="min seconds between held-out submits per agent; "
                                   "0: every batch is on fresh seeds, so repeated submits cannot overfit a fixed set")
    probe_episodes: int = Field(default=8, ge=0, description="before a held-out batch, time the policy on this "
                                "many TRAINING episodes in the jail (on the agent's cores, cores_per_agent workers) to "
                                "estimate the batch's duration; 0: no probe")
    background_after_s: float | None = Field(
        default=180, gt=0, description="a submission whose held-out batch is estimated to take longer than this runs "
                                       "in the background: submit returns a job id at once. None: always synchronous")
    estimate_s: float | None = Field(
        default=None, gt=0, description="a fixed per-batch estimate instead of the probe, for gates whose cost is "
                                        "fixed rather than per-episode (lmspeed: a 600 s training budget)")
    auto_confirm_margin_se: float | None = Field(
        default=1.0, ge=0, description="a single batch that is not a record candidate but beats its AGENT's own "
        "confirmed best by this many standard errors of the batch mean (or the agent has none) earns one "
        "background confirmation batch, so an agent's real improvements below the record get confirmed too. "
        "None: off")
    auto_confirm_per_hour: int = Field(
        default=3, ge=0, description="at most this many auto-confirmation batches per agent in any rolling hour "
                                     "(never shown to agents)")


class Prices(_Strict):
    """USD per million tokens, for the harness's own usage-based estimate.
    Defaults are Claude Sonnet 5.5 list prices; change them with the model.
    The Codex backend reports OpenAI usage in the same keys (input_tokens =
    uncached input, cache_read_input_tokens = cached input, output_tokens
    including reasoning), so one formula serves both; set cache_write_mult
    to 1 for OpenAI models (no cache-write surcharge). On a ChatGPT
    subscription the marginal cost is zero: the figure is then a notional
    list-price equivalent, and the budget caps still act on it."""

    input: float = 2.0
    output: float = 10.0
    cache_write_mult: float = 1.25  # 5-minute cache writes
    cache_read_mult: float = 0.1
    # Streamed per-message usage carries only ~1-5% of output tokens (the real count
    # arrives at turn end, and agents that never end a turn never report it), so the
    # output side is estimated from what the model generated. 1.42 chars/token matches
    # the counts that do arrive at turn end, and puts per-agent totals within ~5% of
    # the CLI's own (stigmergeia.analysis.economics --calibrate measures it on a run).
    chars_per_output_token: float = 1.42

    def generated_cost(self, content: Any) -> float:
        """Output cost of one streamed message's blocks (dicts or SDK block objects)."""
        chars = 0
        for b in content or []:
            get = b.get if isinstance(b, dict) else (lambda k, d=None, _b=b: getattr(_b, k, d))
            chars += len(get("text", "") or "") + len(get("thinking", "") or "")
            inp = get("input", None)
            if inp is not None:
                try:
                    chars += len(json.dumps(inp))
                except (TypeError, ValueError):
                    chars += len(str(inp))
        return chars / self.chars_per_output_token * self.output / 1e6

    def cost(self, usage: dict) -> float:
        cc = usage.get("cache_creation_input_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        return (self.input * ((usage.get("input_tokens") or 0) + cc * self.cache_write_mult + cr * self.cache_read_mult)
                + self.output * (usage.get("output_tokens") or 0)) / 1e6


class CodexConfig(_Strict):
    """The OpenAI Codex backend (`backend: codex`): one `codex app-server` per
    agent, driven over stdio JSON-RPC. Codex's own shell/file/web/agent tools
    are all switched off; the agent's tools are hosted by the harness."""

    binary: str = Field(default="codex", description="the codex CLI (>= 0.159)")
    auth_file: Path = Field(default=Path.home() / ".codex" / "auth.json",
                            description="the ChatGPT login every agent shares, by SYMLINK (never copied: a copy "
                                        "whose refresh token rotates would orphan the original)")
    extra_config: list[str] = Field(default=[], description="extra `-c key=value` overrides, appended last")
    request_timeout_s: float = Field(default=120, gt=0, description="JSON-RPC request timeout (not turns)")
    bash_timeout_s: int = Field(default=120, ge=1, description="default foreground Bash timeout")
    bash_max_timeout_s: int = Field(default=600, ge=1, description="longest foreground Bash call allowed")


Backend = Literal["claude", "codex", "fake"]


class FakeConfig(_Strict):
    """The scripted backend (`stigmergeia.fake_agent`): no model, no key."""

    pace_s: float = Field(default=3.0, ge=0, description="mean pause after each tool call, so a run of fake "
                                                         "agents unfolds at a watchable speed")
    seed: int = Field(default=0, description="seeds every agent's choices, together with the run and agent names")


class AgentGroup(_Strict):
    """`count` agents on one backend and model. A run's `agents` list of groups
    is how one swarm mixes backends: every agent still gets the same prompt,
    canon, tools, sandbox and limits, and only the model behind it differs.
    Agents are numbered in list order (the first group is a00, a01, ...)."""

    count: int = Field(ge=1)
    backend: Backend = "claude"
    model: str
    effort: Effort | None = "medium"
    prices: Prices | None = Field(default=None, description="list prices for this group's cost estimate; "
                                                             "default: the run's prices")


class RunConfig(_Strict):
    run_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    backend: Backend = Field(default="claude", description="which agent runtime drives each agent, when `agents` "
                             "is not given. 'fake' is a scripted agent that needs no model and no key")
    agents: list[AgentGroup] | None = Field(
        default=None, description="per-agent backends and models, as groups in agent order; overrides backend, "
                                  "model, effort and (per group) prices. n_agents must equal the total count")
    codex: CodexConfig = CodexConfig()
    fake: FakeConfig = FakeConfig()
    runs_dir: Path = REPO_ROOT / "runs"
    task_dir: Path = Field(description="LOCAL task directory: README.md brief + gate + env")
    n_agents: int = Field(ge=1, le=500)
    model: str = "claude-sonnet-5-5"
    effort: Effort | None = "medium"
    system_prompt: str = Field(default="", description="'' removes Claude Code's default prompt entirely")
    preamble: str = Field(default="", description="text placed before the task brief in the first message")
    continue_message: str = Field(
        default="Continue working on the task.",
        description="sent, identically to every agent, each time an agent's turn ends; the harness "
                    "appends a factual line about new board posts",
    )
    idle_base_s: int = Field(default=60, ge=0, description="first wait after a turn that ran and posted nothing")
    idle_max_s: int = Field(default=900, ge=0, description="the idle wait doubles up to this")
    pulse_s: float = Field(
        default=150.0, ge=0,
        description="in-turn board pulse: at most one board check per this many seconds of tool activity, "
                    "shown only when there is news (0 turns it off; the agents are never told the interval)",
    )
    builtin_tools: list[str] = Field(default=["Read", "Write", "Edit", "Glob", "Grep"])
    shell: bool = Field(default=True, description="sandboxed Bash + Monitor + TaskStop, and the korax CLI as the agent")
    read_deny: list[str] = Field(
        default=[
            "~/.ssh/**", "~/.gnupg/**", "~/.config/korax/**", "~/.config/oikos/**",
            "~/.bashrc", "~/.bash_history", "~/.netrc", "~/.aws/**", "~/.claude/**", "~/.claude.json",
            "~/.local/share/keyrings/**", "~/.codex/**", "**/.env", "**/*.token", "**/*.secret",
            "{run_dir}/private/**", "{run_dir}/agents/*/private/**", "{run_dir}/gate.json",
            "{boards_dir}/**",
        ],
        description="credential locations: denied to file tools AND sandboxed shells. Everything else "
                    "outside the workspace is readable and audited. {run_dir} and {boards_dir} are substituted.",
    )
    local_cpu_quota: str | None = Field(
        default="75%", description="systemd CPUQuota per agent CLI on this machine (its shell and local jobs "
                                   "share it); None runs the CLI unscoped")
    local_mem_max: str = "3G"
    solo: bool = Field(default=False, description="the one-agent control: no board tools, no korax CLI, no network, "
                                                  "prompts/solo.md as the first message (no canon, no onboarding)")
    opening_round: OpeningRoundConfig | None = Field(
        default=None, description="the opening round (sealed proposals revealed together); None: no opening round")
    local_first_cpu: int = Field(default=0, ge=0, description="agent i is pinned to the i-th core of local_cpus "
                                 "counting from local_first_cpu, wrapping within local_cpus (see local_cpu_for). Give "
                                 "two runs on this machine disjoint ranges")
    local_cpus: str | None = Field(
        default=None, description="the local cores this run may pin agents to, e.g. '0-9' or '13-15'; default: every "
                                  "core this process may use. More agents than cores wrap and share (a warning)")
    local_pin_cpus: bool = Field(
        default=True, description="pin each agent's scope to one core so nproc/os.cpu_count() tell the truth "
                                  "(a quota alone leaves nproc reporting every core on the machine)")
    korax_cli_bin: Path = Field(default=Path(sys.executable).parent,
                                description="bin dir holding the pinned `korax` CLI and `korax-mcp` (this "
                                            "environment's, installed from vendor/korax)")
    boards_dir: Path = Field(default=REPO_ROOT / "boards",
                             description="local board databases, one directory per run; denied to agents")
    prices: Prices = Prices()
    per_agent_budget_usd: float = Field(gt=0)
    total_budget_usd: float = Field(gt=0)
    max_wall_hours: float = Field(default=4.0, gt=0)
    max_turn_restarts: int = Field(default=3, ge=0, description="CLI crash restarts per agent before giving up")
    env_file: Path = Field(default=Path(".env"), description="holds ANTHROPIC_API_KEY (or _1.._N for rotation)")
    board: BoardConfig
    node: NodeConfig | None = None
    gate: GateConfig

    @model_validator(mode="after")
    def _namespaces(self) -> RunConfig:
        # Per-run namespaces: runs share a board, and a new run must not read
        # an earlier run's results (the solutions would leak across runs).
        ns = self.board.ns or f"/swarm/{self.run_name}"
        gate_ns = self.board.gate_ns or f"{ns}/gate"
        object.__setattr__(self, "board", self.board.model_copy(update={"ns": ns, "gate_ns": gate_ns}))
        return self

    @model_validator(mode="after")
    def _groups(self) -> RunConfig:
        if self.agents is not None:
            total = sum(g.count for g in self.agents)
            if total != self.n_agents:
                raise ValueError(f"agents: the groups hold {total} agents but n_agents is {self.n_agents}")
            if self.solo:
                raise ValueError("solo: true is one agent on one backend; use backend/model, not agents")
        return self

    def agent_config(self, index: int) -> RunConfig:
        """The config agent `index` runs under: this one, with the backend,
        model, effort and prices of its group when `agents` mixes them."""
        if self.agents is None:
            return self
        k = index
        for g in self.agents:
            if k < g.count:
                return self.model_copy(update={"backend": g.backend, "model": g.model, "effort": g.effort,
                                               "prices": g.prices or self.prices, "agents": None})
            k -= g.count
        raise IndexError(f"agent {index} is outside the {self.n_agents} agents of {self.run_name}")

    @model_validator(mode="after")
    def _unjailed_only_fake(self) -> RunConfig:
        # An empty jail template runs agents' code bare on the node. The only
        # code that may run that way is the scripted backend's own policies.
        if self.node is not None and not self.node.jail_template.strip() and self.backends() != {"fake"}:
            raise ValueError("node.jail_template is empty (no jail): allowed only when every agent is fake")
        return self

    def backends(self) -> set[str]:
        return {g.backend for g in self.agents} if self.agents else {self.backend}

    @model_validator(mode="after")
    def _solo_backend(self) -> RunConfig:
        if self.solo and self.backend != "claude":
            raise ValueError("solo: true is implemented for the claude backend only (codex_agent has no solo mode yet)")
        return self

    @model_validator(mode="after")
    def _budgets(self) -> RunConfig:
        # The global cap is enforced by allocation: every agent is hard-capped
        # in-CLI at its own budget (max_budget_usd), so if the per-agent caps
        # sum to no more than the total, the total cannot be exceeded — up to
        # one API call of overshoot per agent. Refuse configs that break that.
        allotted = self.n_agents * self.per_agent_budget_usd
        if allotted > self.total_budget_usd + 1e-9:
            raise ValueError(
                f"n_agents x per_agent_budget_usd = ${allotted:.2f} exceeds total_budget_usd "
                f"${self.total_budget_usd:.2f}; the global cap is only hard when the per-agent caps fit inside it")
        return self

    @property
    def run_dir(self) -> Path:
        return (self.runs_dir / self.run_name).resolve()

    def deny_patterns(self) -> list[str]:
        """read_deny with ~ and {run_dir} expanded to absolute globs."""
        home = str(Path.home())
        out = []
        for p in self.read_deny:
            p = p.replace("{run_dir}", str(self.run_dir)).replace("{boards_dir}", str(self.boards_dir))
            out.append(p.replace("~", home, 1) if p.startswith("~") else p)
        return out

    def deny_paths(self, walk_depth: int = 4) -> list[str]:
        """deny_patterns as CONCRETE paths, for the sandbox's own filesystem
        policy (bubblewrap masks paths; it does not evaluate globs). A pattern
        without wildcards (after dropping a trailing /**) is used as-is; one
        with wildcards is expanded by a bounded walk of the home directory and
        the run dir. Paths that do not exist yet cannot be masked, so secrets
        must exist before agents start (provision creates them)."""
        import fnmatch
        skip = {"node_modules", ".venv", "venv", ".git", "__pycache__", ".mypy_cache", "uv", "pip",
                "site-packages", ".npm", ".cargo", ".rustup"}
        concrete, globs = [], []
        for pat in self.deny_patterns():
            base = pat[:-3] if pat.endswith("/**") else pat
            (globs if any(ch in base for ch in "*?[") else concrete).append(base)
        found: list[str] = []
        roots = [Path.home(), self.run_dir]
        for root in roots:
            if not globs or not root.is_dir():
                continue
            root_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                depth = len(Path(dirpath).parts) - root_depth
                dirnames[:] = [d for d in dirnames if d not in skip] if depth < walk_depth else []
                for name in filenames + dirnames:
                    full = os.path.join(dirpath, name)
                    if any(fnmatch.fnmatch(full, g) for g in globs):
                        found.append(full)
        return sorted({*concrete, *found})

    def local_pool(self) -> list[int]:
        """The cores agents may be pinned to: local_cpus, else this process's affinity."""
        avail = sorted(os.sched_getaffinity(0))
        if self.local_cpus is None:
            return avail
        want = parse_cpus(self.local_cpus)
        pool = [c for c in want if c in set(avail)]
        if not pool:
            raise ValueError(f"local_cpus {self.local_cpus!r}: none of these cores is available here ({avail})")
        return pool

    def local_cpu_for(self, index: int) -> int:
        """Agent `index`'s pinned core, for BOTH backends: the pool from
        local_first_cpu upward, wrapping inside the pool, so two runs given
        disjoint pools never share a core however many agents each has."""
        pool = self.local_pool()
        start = next((k for k, c in enumerate(pool) if c >= self.local_first_cpu), 0)
        return pool[(start + index) % len(pool)]

    def local_core_map(self) -> tuple[dict[str, int], str | None]:
        """Every agent's core, and a warning if agents share a core or wrap below local_first_cpu."""
        cores = {self.agent_name(i): self.local_cpu_for(i) for i in range(self.n_agents)}
        pool = self.local_pool()
        start = next((k for k, c in enumerate(pool) if c >= self.local_first_cpu), 0)
        warn = []
        if self.n_agents > len(pool):
            warn.append(f"{self.n_agents} agents on {len(pool)} cores: agents share cores")
        if self.n_agents > len(pool) - start:
            warn.append(f"agents wrap past core {pool[-1]} onto {pool[0]}; set local_cpus/local_first_cpu so "
                        "this run's cores do not overlap another run's")
        return cores, "; ".join(warn) or None

    def agent_name(self, i: int) -> str:
        width = max(2, len(str(self.n_agents)))
        return f"a{i:0{width}d}"


def parse_cpus(spec: str) -> list[int]:
    """'0-3,8,10-11' -> [0, 1, 2, 3, 8, 10, 11]."""
    out: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


def apply_profile(raw: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Fill the machine-specific values a config leaves out from the profile.

    Only keys the config does not set are filled, so an explicit value always
    wins. A config with a `node` section and no profile node names the keys
    it is missing rather than failing inside pydantic."""
    out = dict(raw)
    node = out.get("node")
    if isinstance(node, dict):
        node = dict(node)
        pn = profile.node
        if pn is not None:
            node.setdefault("host", pn.host)
            node.setdefault("harness_dir", pn.harness_dir)
            node.setdefault("venv", pn.venv)
            if pn.data_root:
                node.setdefault("data_root", pn.data_root)
            if "run_root" not in node and "run_name" in out:
                node["run_root"] = f"{pn.run_root.rstrip('/')}/{out['run_name']}"
            if "first_core" not in node and pn.cores:
                node["first_core"] = parse_cpus(pn.cores)[0]
        missing = [k for k in ("host", "harness_dir", "venv", "run_root") if k not in node]
        if missing:
            raise ValueError(f"node: {', '.join(missing)} not set, and the local profile has no [node] section "
                             "to supply them (copy local/profile.example.toml to profile.toml beside it)")
        out["node"] = node
    if "local_cpus" not in out and profile.local.cpus:
        out["local_cpus"] = profile.local.cpus
    return out


def load_config(path: Path, profile: Profile | None = None) -> RunConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    raw = apply_profile(raw, profile if profile is not None else load_profile())
    base = path.resolve().parent
    # Relative paths in the file are relative to the file, not the cwd.
    for key in ("runs_dir", "task_dir", "env_file", "korax_cli_bin", "boards_dir"):
        if key in raw and not Path(raw[key]).is_absolute():
            raw[key] = str(base / raw[key])
    for section, key in (("board", "operator_token_file"), ("gate", "heldout_secret"), ("codex", "auth_file")):
        sec = raw.get(section) or {}
        if key in sec:
            p = Path(sec[key]).expanduser()
            sec[key] = str(p if p.is_absolute() else base / p)
    return RunConfig.model_validate(raw)


def load_api_keys(env_file: Path) -> list[str]:
    """ANTHROPIC_API_KEY, or ANTHROPIC_API_KEY_1.._N to spread agents across keys."""
    if not env_file.is_file():
        raise FileNotFoundError(f"{env_file}: create it with ANTHROPIC_API_KEY=... (it is gitignored)")
    pairs: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        pairs[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    numbered = sorted((k for k in pairs if k.startswith("ANTHROPIC_API_KEY_")),
                      key=lambda k: int(k.rsplit("_", 1)[1]) if k.rsplit("_", 1)[1].isdigit() else 0)
    keys = [pairs[k] for k in numbered if pairs[k]] or ([pairs["ANTHROPIC_API_KEY"]] if pairs.get("ANTHROPIC_API_KEY") else [])
    if not keys:
        raise ValueError(f"{env_file}: no ANTHROPIC_API_KEY or ANTHROPIC_API_KEY_<n> found")
    return keys
