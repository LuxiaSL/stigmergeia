"""One agent's life: connect, orient, keep working until a limit, log all of it."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ConversationResetMessage,
    HookMatcher,
    ResultMessage,
    ToolUseBlock,
)

from . import board
from .board import Credential
from .config import RunConfig
from .guard import make_guard
from .lab import AgentSlot, Lab, lab_server
from .pulse import Pulse

log = logging.getLogger("swarm")

# Korax tools an agent must never have: they read or rebind credentials.
KORAX_DENY = ["korax_enlist", "korax_animate", "korax_credentials", "korax_rotate"]
LAB_TOOLS = ["run", "score", "submit", "jobs", "wait"]
SHELL_TOOLS = ["Bash", "Monitor", "TaskStop"]
# A turn that used none of these did no work: it only read. Idle turns are
# followed by a wait (for others' posts, or a growing timeout), not an
# immediate re-prompt: back-to-back read-only turns otherwise consume nearly
# the whole budget while producing nothing.
ACTIVE_TOOLS = {"Write", "Edit", "Bash", "mcp__lab__run", "mcp__lab__score", "mcp__lab__submit", "mcp__lab__propose",
                "mcp__lab__answer", "mcp__korax__korax_post"}
IDLE_POLL_S = 15
DIGEST_MAX = 6  # posts shown in the continue line: a fixed size at any swarm size
DIGEST_CHARS = 220


def _is_work(block: Any) -> bool:
    """Did this tool call do work? Shell `korax ...` calls are reading the
    board, which is what an idle turn does; everything else in ACTIVE_TOOLS
    is work. (Without this, polling through the shell would dodge the idle
    backoff.)"""
    if not isinstance(block, ToolUseBlock) or block.name not in ACTIVE_TOOLS:
        return False
    if block.name == "Bash":
        cmd = str(block.input.get("command", "")).lstrip()
        return not (cmd.startswith("korax ") or cmd == "korax")
    return True


class SandboxUnavailable(Exception):
    """The CLI said it would run shell commands WITHOUT the sandbox. It fails
    open (a stderr warning, then unsandboxed commands); the harness fails
    closed: the agent stops and is not restarted."""


class _BudgetExhausted(Exception):
    """The CLI stopped this agent at its hard cap: leave, don't restart."""


@dataclass
class Ledger:
    """Spend across the whole run, in USD. `total_cost_usd` on a ResultMessage
    is a RUNNING total for the current connection that resets to zero on a
    conversation reset or a new connection; each agent banks its last running
    total whenever either happens."""

    total_cap: float
    spent: dict[str, float] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def set(self, agent: str, value: float) -> float:
        async with self.lock:
            self.spent[agent] = value
            return sum(self.spent.values())

    def total(self) -> float:
        return sum(self.spent.values())


@dataclass
class AgentState:
    banked: float = 0.0  # spend from finished connections / pre-reset conversations
    running: float = 0.0  # current connection's running total
    estimate: float = 0.0  # from per-message usage: sees spend before a turn's ResultMessage
    seen_messages: set[str] = field(default_factory=set)
    session_id: str | None = None
    turns: int = 0
    idle_turns: int = 0
    idle_streak: int = 0
    board_cursor: int = -1
    restarts: int = 0
    stop_reason: str = ""

    @property
    def spent(self) -> float:
        # The CLI's figure is authoritative but only arrives at turn end; the
        # usage estimate is live. Take the larger, so a crash or abort
        # mid-turn never reads as free.
        return max(self.banked + self.running, self.estimate)


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {"_type": type(obj).__name__, **{k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


class Agent:
    def __init__(self, cfg: RunConfig, slot: AgentSlot, cred: Credential, gate: Credential,
                 lab: Lab | None, api_key: str, korax_mcp: list[str], ledger: Ledger,
                 deadline: float, orientation: str, opening_round: Any = None):
        self.cfg, self.slot, self.cred, self.gate = cfg, slot, cred, gate
        self.opening_round = opening_round  # the opening round (opening_round.OpeningRound), or None
        self.lab, self.api_key, self.korax_mcp = lab, api_key, korax_mcp
        self.ledger, self.deadline, self.orientation = ledger, deadline, orientation
        self.state = AgentState()
        self.sandbox_failed: str | None = None
        self.names: dict[str, str] = {gate.identity: "GATE"}  # identity -> display, filled by the runner
        # always this agent's own private dir (its API key file lives there)
        self.deny_paths: list[str] = sorted({*cfg.deny_paths(), str(slot.private)}) if cfg.shell else []
        self.transcript = slot.private / "transcript.jsonl"
        # board news inside a turn (see pulse.py); the solo control has no board to pulse
        self.pulse: Pulse | None = None if (cfg.solo or cfg.pulse_s <= 0) else Pulse(
            cred, gate, cfg.board.ns, cfg.board.gate_ns, lambda: self.names,
            higher_is_better=cfg.gate.higher_is_better, min_interval_s=cfg.pulse_s,
            on_error=lambda m: self._record({"_type": "harness_error", "error": f"pulse: {m}"}))

    async def _pulse_hook(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """PostToolUse / PostToolUseFailure: append the board pulse to what the
        model sees after this tool call (the CLI renders `additionalContext` as
        a system reminder next to the tool result). Never blocks or fails a call."""
        if self.pulse is None:
            return {}
        try:
            text = await self.pulse.poll()
        except Exception as e:  # belt and braces: poll() already swallows board errors
            self._record({"_type": "harness_error", "error": f"pulse hook: {type(e).__name__}: {e}"})
            return {}
        if not text:
            return {}
        self._record({"_type": "harness_pulse", "tool_use_id": tool_use_id, "text": text})
        event = str(input_data.get("hook_event_name") or "PostToolUse")
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def options(self, resume: str | None) -> ClaudeAgentOptions:
        p = self.slot.private
        korax_env = {
            "KORAX_URL": self.cred.url, "KORAX_TOKEN": self.cred.token,
            "KORAX_IDENTITY": self.cred.identity,
            "KORAX_CONFIG_DIR": str(p / "korax-config"),  # never the operator's profiles
        }
        servers: dict[str, Any] = {} if self.cfg.solo else {
            "korax": {"type": "stdio", "command": self.korax_mcp[0], "args": self.korax_mcp[1:], "env": korax_env}}
        tools = list(self.cfg.builtin_tools) + (SHELL_TOOLS if self.cfg.shell else [])
        allowed = list(tools)
        if self.lab is not None:
            # canon must be acked first (the solo control has no board and no canon)
            servers["lab"] = lab_server(self.lab, self.slot, None if self.cfg.solo else self.cred, self.opening_round,
                                        self.pulse)
            allowed += [f"mcp__lab__{t}" for t in LAB_TOOLS + (["propose", "answer"] if self.opening_round else [])]
        deny = self.cfg.deny_patterns()
        guard = make_guard(self.slot.workspace, ("mcp__korax__", "mcp__lab__"), deny,
                           audit=p / "audit.jsonl", shell=self.cfg.shell,
                           posting_blocked=(lambda: self.opening_round.posting_blocked(self.slot.name)) if self.opening_round else None)
        shell_env: dict[str, str] = {}
        extra: dict[str, Any] = {}
        if self.cfg.shell:
            # The sandbox: writes only in the workspace, network only to the
            # board, no unsandboxed escape hatch. Credential globs are Read
            # deny rules, which the sandbox applies to shell reads too; the
            # guard enforces the same list for the file tools.
            extra["sandbox"] = {
                "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                "network": {"allowedDomains": [] if self.cfg.solo else [urlsplit(self.cred.url).hostname or "127.0.0.1"]},
                # The sandbox's OWN read policy: permission Read rules do not
                # reach sandboxed shell reads, so without this a denied file
                # and the API key file are both cat-able from the shell.
                "filesystem": {"denyRead": self.deny_paths},
            }
            # The key reaches the CLI through apiKeyHelper, NOT the environment:
            # a sandboxed shell inherits the CLI's env, and it cannot read the
            # key file (the private dir is a deny rule).
            key_file = p / "api.key"
            board.write_private(key_file, self.api_key)  # always: a stale file must never outlive a key change
            extra["settings"] = json.dumps({
                # Claude Code rule paths: "/x" is relative to the settings
                # file; an absolute path needs "//x". Globs like "**/.env" pass as-is.
                "permissions": {"deny": [f"Read(/{d})" if d.startswith("/") else f"Read({d})" for d in deny]},
                "apiKeyHelper": f"cat {shlex.quote(str(key_file))}",
            })
            # the korax CLI, configured as this agent (not uv: its cache writes are sandboxed away)
            shellbin = Path(__file__).resolve().parent / "shellbin"
            shell_env = ({"PATH": "/usr/local/bin:/usr/bin:/bin"} if self.cfg.solo else
                         {**korax_env, "KORAX_REAL_BIN": str(self.cfg.korax_cli_bin), "SWARM_NS": self.cfg.board.ns,
                          "PATH": f"{shellbin}:/usr/local/bin:/usr/bin:/bin"})
        if self.cfg.local_cpu_quota:
            import claude_agent_sdk
            real_cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            extra["cli_path"] = str(Path(__file__).resolve().parent / "shellbin" / "claude-scoped")
            shell_env.update({"SWARM_REAL_CLI": str(real_cli), "SWARM_CPU_QUOTA": self.cfg.local_cpu_quota,
                              "SWARM_MEM_MAX": self.cfg.local_mem_max})
            if self.cfg.local_pin_cpus:
                # one core per agent, round-robin over this machine's cores
                shell_env["SWARM_CPUS"] = str(self.cfg.local_cpu_for(self.slot.index))
        return ClaudeAgentOptions(
            system_prompt=self.cfg.system_prompt,
            tools=tools,
            **extra,
            allowed_tools=allowed + ([] if self.cfg.solo else ["mcp__korax"]),
            disallowed_tools=[f"mcp__korax__{t}" for t in KORAX_DENY],
            permission_mode="dontAsk",
            mcp_servers=servers,
            strict_mcp_config=True,
            setting_sources=[],
            cwd=str(self.slot.workspace),
            model=self.cfg.model,
            effort=self.cfg.effort,
            resume=resume,
            # NO max_budget_usd here: the CLI writes it into the system prompt
            # ("USD budget: $0/$2; $2 remaining", checked), and agents that can
            # see their budget ration it and stop early. The cap is enforced
            # by the harness instead, mid-turn, from per-message usage.
            env={
                **shell_env,
                **({} if self.cfg.shell else {"ANTHROPIC_API_KEY": self.api_key}),
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                # Bash cwd goes back to the workspace after every command (the bundled CLI,
                # 2.1.285, runs the reset after each command, silently). Without it a `cd` carries
                # into later calls, and agents lose time working from a directory they forgot
                # they left; every cwd change also re-sends the ~9 KB sandbox_instructions block
                # (its deny list names <cwd>/.claude/...).
                "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
                # the "user hasn't heard from you in a while" reminder, which an agent reads as a
                # nudge to act; a tri-bool env, "0" = off
                "CLAUDE_CODE_SILENT_TURN_REMINDER": "0",
                # "<total_tokens>15000000 tokens left</total_tokens>" after every tool result: a
                # countdown budget in the agent's context. Agents must never see a budget.
                "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
                # a 120 s foreground default pushes most agents into sleep-and-poll loops
                "BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "1800000",
                "CLAUDE_CONFIG_DIR": str(p / "claude-config"),
            },
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])],
                   **({"PostToolUse": [HookMatcher(matcher=None, hooks=[self._pulse_hook])],
                       "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[self._pulse_hook])]}
                      if self.pulse is not None else {})},
            stderr=self._on_stderr,
        )

    def _on_stderr(self, line: str) -> None:
        self._record({"_type": "cli_stderr", "line": line})
        if self.cfg.shell and "Sandbox disabled" in line:
            self.sandbox_failed = line.strip()

    def _record(self, obj: Any) -> None:
        body = _jsonable(obj)
        row = {"t": round(time.time(), 3), **body} if isinstance(body, dict) else {"t": round(time.time(), 3), "v": body}
        with self.transcript.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def _stop_reason(self) -> str | None:
        if self.state.spent >= self.cfg.per_agent_budget_usd:
            return "agent budget"
        if self.ledger.total() >= self.cfg.total_budget_usd:
            return "run budget"
        if time.time() >= self.deadline:
            return "wall clock"
        return None

    async def _new_posts(self) -> list[dict[str, Any]]:
        """Posts by OTHERS in the agent's namespace since its cursor; advances
        the cursor past everything seen, its own posts included."""
        try:
            envs = await asyncio.to_thread(board.read_since, self.cred, self.cfg.board.ns,
                                           self.state.board_cursor)
        except Exception as e:  # a board hiccup must not stall or kill the agent
            self._record({"_type": "harness_error", "error": f"board read: {type(e).__name__}: {e}"})
            return []
        if envs:
            self.state.board_cursor = max(e["id"] for e in envs)
        return [e for e in envs if e["author"] != self.cred.identity]

    async def _next_message(self, idle: bool) -> str:
        new = await self._new_posts()
        if idle and not new:
            self.state.idle_streak += 1
            wait = min(self.cfg.idle_max_s, self.cfg.idle_base_s * 2 ** (self.state.idle_streak - 1))
            until = min(time.time() + wait, self.deadline)
            self._record({"_type": "harness_idle_wait", "seconds": wait, "streak": self.state.idle_streak})
            while not new and time.time() < until and not self._stop_reason():
                await asyncio.sleep(min(IDLE_POLL_S, max(0.0, until - time.time())))
                new = await self._new_posts()
        elif not idle:
            self.state.idle_streak = 0
        best = await self._best_verified()
        ns = self.cfg.board.ns
        # a background held-out job that finished and was not shown on a lab call yet
        jobs = self.lab.take_notes(self.slot, "continue") if self.lab is not None else ""
        jobs = f"\n\n{jobs}" if jobs else ""
        if self.cfg.solo:
            fact = (f"[best score verified by the gate so far: {best[0]}; its code: {best[3]}]" if best
                    else "[no score verified by the gate yet]")
            return f"{self.cfg.continue_message}\n\n{fact}{jobs}"
        if new:
            fact = await self._digest(new)
        else:
            fact = f"[no new posts by others in {ns} since your last turn]"
        if self.pulse is not None:
            # DMs and posts elsewhere that name this agent: the digest covers only the namespace
            self.pulse.seen_ns_through(self.state.board_cursor)
            extra = await self.pulse.poll(force=True, skip_ids=frozenset(e["id"] for e in new))
            if extra:
                fact += "\n" + extra
        if best:
            fact += f"\n[best score verified by the gate so far: {best[0]} (by {best[1]}, #{best[2]})"
            fact += f"; its code: {best[3]}]" if best[3] else "]"
        return f"{self.cfg.continue_message}\n\n{fact}{jobs}"

    async def _digest(self, new: list[dict[str, Any]]) -> str:
        """What the others posted since this agent's last turn, in front of it:
        the DIGEST_MAX most recent, newest first, one line each — a fixed size
        however many agents there are — plus how to read the rest."""
        ns = self.cfg.board.ns
        shown = sorted(new, key=lambda e: e["id"], reverse=True)[:DIGEST_MAX]
        lines = []
        for e in shown:
            who = self.names.get(e["author"], e["author"][-6:])
            try:
                full = await asyncio.to_thread(board._request, f"{self.cred.url}/envelope/{e['id']}", self.cred.token)
                p = (full.get("envelope") or full).get("payload")
            except Exception:
                p = None
            if isinstance(p, dict) and p.get("kind") == "gate-result":
                tag = ("NEW RECORD" if p.get("record") else "ties the record" if p.get("tie")
                       else "confirmed" if p.get("confirmed") else "one batch")
                text = f"{p.get('agent')} {p.get('policy')}: {p.get('score', p.get('mean'))} ({tag}); code {p.get('code', '')}"
            else:
                text = p if isinstance(p, str) else (p.get("text") if isinstance(p, dict) and "text" in p else json.dumps(p))
            first = " ".join(str(text or "").split())[:DIGEST_CHARS]
            lines.append(f"  #{e['id']} {who} {e['type']}: {first}")
        more = len(new) - len(shown)
        head = f"[{len(new)} new post{'s' if len(new) != 1 else ''} by others in {ns} since your last turn, newest first]"
        tail = (f"  (+{more} older not shown: korax read --ns {ns} --since {min(e['id'] for e in new) - 1})"
                if more > 0 else "")
        return "\n".join([head, *lines] + ([tail] if tail else []))

    async def _best_verified(self) -> tuple[float, str, int, str] | None:
        """The best held-out score the GATE has posted (highest, or lowest when
        gate.higher_is_better is false) — only the gate's
        own envelopes count, so a claimed score never appears as a fact."""
        try:
            envs = await asyncio.to_thread(board.read_since, self.gate, self.cfg.board.gate_ns, -1)
        except Exception as e:
            self._record({"_type": "harness_error", "error": f"gate read: {type(e).__name__}: {e}"})
            return None
        best = None
        for e in envs:
            if e["author"] != self.gate.identity:
                continue
            full = await asyncio.to_thread(board._request, f"{self.gate.url}/envelope/{e['id']}", self.gate.token)
            p = (full.get("envelope") or full).get("payload") or {}
            if not (isinstance(p, dict) and p.get("kind") == "gate-result"):
                continue
            if "confirmed" in p and not p["confirmed"]:
                continue  # an unconfirmed batch is never the best-so-far fact
            val = p.get("score", p.get("mean"))
            better = (lambda a, b: a > b) if self.cfg.gate.higher_is_better else (lambda a, b: a < b)
            if isinstance(val, (int, float)) and (best is None or better(val, best[0])):
                best = (val, p.get("agent", "?"), e["id"], p.get("code", ""))
        return best

    async def _converse(self, client: ClaudeSDKClient, message: str) -> bool:
        """One turn. Returns whether the agent did any work in it."""
        active = False
        await client.query(message)
        stream = client.receive_response().__aiter__()
        while True:
            # The wall clock is enforced INSIDE a turn: a turn blocked on a
            # long tool call (a held-out submit can run for tens of minutes)
            # would sail past a deadline checked only between turns.
            left = self.deadline - time.time()
            try:
                msg = await asyncio.wait_for(stream.__anext__(), timeout=max(1.0, left))
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                self.state.stop_reason = "wall clock (interrupted mid-turn)"
                self._record({"_type": "harness_deadline", "note": "interrupted a turn at the wall-clock limit"})
                try:
                    await asyncio.wait_for(client.interrupt(), timeout=30)
                except Exception:
                    pass
                raise _BudgetExhausted()  # the same exit: stop, no restart
            self._record(msg)
            if self.sandbox_failed:
                await client.interrupt()
                raise SandboxUnavailable(self.sandbox_failed)
            if isinstance(msg, AssistantMessage) and any(_is_work(b) for b in msg.content):
                active = True
            if isinstance(msg, AssistantMessage):
                # every streamed row carries its own blocks: price what was generated
                self.state.estimate += self.cfg.prices.generated_cost(msg.content)
            if isinstance(msg, AssistantMessage) and msg.usage and msg.message_id not in self.state.seen_messages:
                if msg.message_id:
                    self.state.seen_messages.add(msg.message_id)
                self.state.estimate += self.cfg.prices.cost(msg.usage)
                await self.ledger.set(self.slot.name, self.state.spent)
                # The hard cap, enforced here (never shown to the agent): stop
                # mid-turn the moment a response takes it over. Overshoot is at
                # most that one response.
                if self.state.spent >= self.cfg.per_agent_budget_usd:
                    self.state.stop_reason = "agent budget (harness cap)"
                    await client.interrupt()
                    raise _BudgetExhausted()
            if isinstance(msg, ConversationResetMessage):
                self.state.banked += self.state.running
                self.state.running = 0.0
            elif isinstance(msg, ResultMessage):
                self.state.session_id = msg.session_id
                self.state.turns += 1
                if msg.total_cost_usd is not None:
                    self.state.running = msg.total_cost_usd
                await self.ledger.set(self.slot.name, self.state.spent)
                if msg.subtype == "error_max_budget_usd":
                    self.state.stop_reason = "agent budget (enforced in CLI)"
                    raise _BudgetExhausted()
        if not active:
            self.state.idle_turns += 1
        return active

    async def _prime_pulse(self) -> None:
        if self.pulse is None:
            return
        try:
            await asyncio.to_thread(self.pulse.prime_sync, self.state.board_cursor)
        except Exception as e:  # it primes itself on its first poll instead
            self._record({"_type": "harness_error", "error": f"pulse prime: {type(e).__name__}: {e}"})

    async def live(self) -> AgentState:
        await self._new_posts()  # start the cursor at the current head
        await self._prime_pulse()
        message = self.orientation
        while True:
            reason = self._stop_reason()
            if reason:
                self.state.stop_reason = reason
                break
            try:
                async with ClaudeSDKClient(options=self.options(self.state.session_id)) as client:
                    while not (reason := self._stop_reason()):
                        active = await self._converse(client, message)
                        message = await self._next_message(idle=not active)
                    self.state.stop_reason = reason
                    break
            except _BudgetExhausted:
                break
            except SandboxUnavailable as e:
                self.state.stop_reason = f"sandbox unavailable, stopped (fail closed): {e}"[:300]
                log.error("%s: %s", self.slot.name, self.state.stop_reason)
                break
            except (ClaudeSDKError, OSError) as e:
                self._record({"_type": "harness_error", "error": f"{type(e).__name__}: {e}"})
                self.state.banked += self.state.running
                self.state.running = 0.0
                self.state.restarts += 1
                if self.state.restarts > self.cfg.max_turn_restarts:
                    self.state.stop_reason = f"gave up after {self.state.restarts} restarts: {e}"
                    break
                log.warning("%s: %s; restarting (%d)", self.slot.name, e, self.state.restarts)
                await asyncio.sleep(min(60, 5 * self.state.restarts))
                message = await self._next_message(idle=False)
        end = {k: v for k, v in dataclasses.asdict(self.state).items() if k != "seen_messages"}
        self._record({"_type": "agent_end", **end, "spent": self.state.spent})
        return self.state


# The task brief is written for a swarm; the solo control gets the same brief minus the two swarm phrases.
SOLO_BRIEF_EDITS = [
    (" That is the number this run exists to\npush up, and it belongs to everyone here: a score raised by building on\nsomeone else's policy counts exactly as much as one raised alone.",
     " That is the number this run exists to\npush up."),
    ("nothing else, and posts every held-out result to the board.", "nothing else, and records every held-out result."),
    ("- A plateau is a finding. Post it, with the numbers that show it, and\n  then try something different",
     "- A plateau is a finding. Measure it, and\n  then try something different"),
    ("- Every improvement to the best verified score counts, however small, and\n  so does a dead end reported clearly enough that nobody repeats it.",
     "- Every improvement to the best verified score counts, however small."),
]


def solo_brief(text: str) -> str:
    for old, new in SOLO_BRIEF_EDITS:
        if old not in text:
            raise ValueError(f"solo brief edit does not match the task README: {old[:60]!r}")
        text = text.replace(old, new)
    return text


ROSTER_MAX = 40  # beyond this the first message would bloat; `korax identities` lists everyone


def render_orientation(cfg: RunConfig, template: str, name: str, cred: Credential,
                       gate: Credential, roster: list[tuple[str, str]] | None = None) -> str:
    brief = (cfg.task_dir / "README.md").read_text()
    if cfg.solo:
        brief = solo_brief(brief)
    others = [(n, i) for n, i in (roster or []) if n != name]
    lines = [f"  - {n}: {i}" for n, i in others[:ROSTER_MAX]]
    if len(others) > ROSTER_MAX:
        lines.append(f"  - … and {len(others) - ROSTER_MAX} more: `korax identities`")
    opening = ("\n\n**Then: the opening round.** Before the lab opens, every agent submits a sealed proposal with the "
               "lab's `propose` tool; all of them are revealed together once everyone has proposed. Then each agent "
               "answers the round in turn, with the lab's `answer` tool. See *The opening round* in the "
               "swarm-environment canon." if cfg.opening_round else "")
    if cfg.backend == "codex" and cfg.shell:
        # canon/02-swarm-environment.md describes the shell backend-neutrally; this is the one thing
        # that differs on Codex
        opening += ("\n\n**Your shell here:** every Bash call starts a fresh shell in your workspace, so `cd`, "
                    "exported variables and `&` jobs don't carry over between calls; chain steps in one call, and "
                    "use `run_in_background` for anything that should keep running (TaskOutput reads it).")
    out = template.format(opening=opening, name=name, n_agents=cfg.n_agents, identity=cred.identity, ns=cfg.board.ns,
                          gate_ns=cfg.board.gate_ns, gate_identity=gate.identity,
                          budget_usd=cfg.per_agent_budget_usd, brief=brief,
                          roster="\n".join(lines) or "  - (none)")
    if cfg.n_agents == 1:  # the solo control: say so plainly rather than "one of 1 agents"
        out = out.replace(f"You are {name}, one of 1 agents working on the same task at the same time, on a Korax board.",
                          f"You are {name}, the only agent working on this task, on a Korax board.")
        out = out.replace("- The others (mention them with `--mention <band>`, message them with `korax dm <band> \"…\"`):\n  - (none)\n",
                          "- There are no other agents in this run.\n")
    return out


def korax_mcp_command(bin_dir: Path) -> list[str]:
    """The pinned Korax MCP server, from the environment the harness runs in
    (installed from vendor/korax). Refuses a missing binary here rather than
    letting every agent fail to connect."""
    exe = bin_dir / "korax-mcp"
    if not exe.is_file():
        raise FileNotFoundError(f"no korax-mcp at {exe}: run `uv sync` in this repository")
    return [str(exe)]


def slot_paths(cfg: RunConfig, i: int) -> tuple[Path, Path]:
    """(workspace, private dir) for agent i.

    Workspaces sit side by side in <run>/ws/<name>, exactly as on the node
    (<run_root>/agents/<name>), so `../<name>/` means the same thing in an
    agent's local shell and in the lab. With each workspace one level deeper,
    `../<name>/` points nowhere locally and agents conclude they cannot read
    each other's code. Private dirs live apart, in <run>/private/<name>.
    A run provisioned with the legacy layout (<run>/agents/<name>/{ws,private})
    keeps it, and panel/status read both layouts."""
    name = cfg.agent_name(i)
    legacy = cfg.run_dir / "agents" / name
    if legacy.is_dir():
        return legacy / "ws", legacy / "private"
    return cfg.run_dir / "ws" / name, cfg.run_dir / "private" / name
