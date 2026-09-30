"""The afterparty: wake every agent of a finished run, tell it honestly how the
run went, collect a private survey, then let the agents meet on the board.

Per agent, one resumed Claude session (forked, so the run's own session file is
never appended to):
1. the wake-up message: the result, what was withheld and why, what comes next;
2. the survey, through an in-process MCP tool whose answers go to
   <run>/afterparty/survey.jsonl (a read-denied path: no agent can read the
   others' answers), with posting to the board refused until it is in;
3. the afterparty: the board namespace <ns>/afterparty opens, and a few rounds
   of continue prompts carry the newest posts there, each round waiting (up to
   a timeout) for every guest to finish the one before.
The solo control gets the wake-up, the survey and a congratulation.

Nothing of the run is modified: transcripts, workspaces and board history are
only read; the afterparty writes new files under <run>/afterparty/ and new
posts under <ns>/afterparty. The lab is not attached.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from . import board
from .board import Credential
from .config import RunConfig
from .guard import make_guard
from .lab import AgentSlot, ToolSpec

log = logging.getLogger("swarm")
PROMPTS = Path(__file__).resolve().parent / "prompts" / "afterparty.yaml"
READ_ONLY_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
DIGEST_MAX = 12
DIGEST_CHARS = 700


class PartySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    budget_usd: float = Field(default=1.5, gt=0, description="harness-side cap per guest, from per-message usage")
    rounds: int = Field(default=3, ge=0, description="continue prompts after the afterparty opens")
    survey_nudges: int = Field(default=2, ge=0)
    turn_timeout_s: float = Field(default=600, gt=0, description="a turn longer than this is interrupted")
    round_timeout_s: float = Field(default=300, gt=0, description="how long a round waits for the other guests")
    only: list[str] | None = Field(default=None, description="wake only these agents (names like a00)")
    facts_file: Path | None = Field(default=None, description=(
        "the operator's YAML of run facts (`runs`, `series`, `wake_<run>`, or any other prompt key), merged "
        "over the generic prompts; without it every run gets the generic texts"))


# ---------------------------------------------------------------- facts

@dataclass
class Record:
    minute: float
    agent: str
    score: float


@dataclass
class RunFacts:
    run: str
    start: float
    end: float
    records: list[Record]
    own_best: dict[str, float]
    end_reason: dict[str, str]

    @property
    def duration_min(self) -> int:
        return round((self.end - self.start) / 60)

    @property
    def best(self) -> Record | None:
        return self.records[-1] if self.records else None

    def best_at(self, minute: float) -> float | None:
        vals = [r.score for r in self.records if r.minute <= minute]
        return vals[-1] if vals else None


def _rows(path: Path) -> list[dict[str, Any]]:
    out = []
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line from a killed run
    return out


def _ts(iso: str) -> float:
    from datetime import datetime, timezone
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def run_facts(cfg: RunConfig, gate: Credential, transcripts: dict[str, Path]) -> RunFacts:
    """The run's timeline from its own records: start and end from the agents'
    transcripts, records and bests from the GATE's posts only (a claimed score
    is never a fact). Records are the gate's own `record: true` results."""
    starts, ends, reasons = [], [], {}
    for name, t in transcripts.items():
        rows = _rows(t)
        if not rows:
            continue
        starts.append(rows[0]["t"])
        ends.append(rows[-1]["t"])
        end = next((r for r in reversed(rows) if r.get("_type") == "agent_end"), None)
        reasons[name] = end.get("stop_reason", "") if end else "interrupted mid-turn (the run was stopped)"
    if not starts:
        raise ValueError(f"{cfg.run_name}: no transcripts found; nothing to wake")
    start = min(starts)
    envs = board.read_since(gate, cfg.board.gate_ns, -1)
    records, own = [], {}
    for e in sorted(envs, key=lambda e: e["id"]):
        if e["author"] != gate.identity:
            continue
        full = board._request(f"{gate.url}/envelope/{e['id']}", gate.token)
        p = (full.get("envelope") or full).get("payload") or {}
        if not (isinstance(p, dict) and p.get("kind") == "gate-result"):
            continue
        if "confirmed" in p and not p["confirmed"]:
            continue
        score = p.get("score", p.get("mean"))
        if not isinstance(score, (int, float)):
            continue
        agent = str(p.get("agent", "?"))
        better = (lambda a, b: a > b) if cfg.gate.higher_is_better else (lambda a, b: a < b)
        if agent not in own or better(score, own[agent]):
            own[agent] = score
        if p.get("record"):
            ts = e.get("ts")
            minute = (_ts(ts) - start) / 60 if isinstance(ts, str) else float("nan")
            records.append(Record(round(minute, 1), agent, float(score)))
    return RunFacts(cfg.run_name, start, max(ends), records, own, reasons)


def timeline_text(f: RunFacts) -> str:
    if not f.records:
        return "  (no records were set)"
    return "\n".join(f"  - {r.minute:.0f} min: {r.agent}, {r.score:.2f}" for r in f.records)


def end_note(reason: str) -> str:
    if "interrupted" in reason:
        return "you were cut off in the middle of a turn: that was the clock, not anything you did"
    if "budget" in reason:
        return "you had reached your spending cap"
    return "your turn had ended when the clock ran out"


# ---------------------------------------------------------------- survey

RATINGS = {
    "lab_tools": "the lab tools (run / score / submit)",
    "korax_board": "the korax CLI and MCP tools (posting, reading, onboarding, acks)",
    "shell_sandbox": "the sandboxed shell",
    "watch": "korax watch / waiting for news on the board",
    "canon": "the canon (the swarm's shared rules and conventions) as guidance",
    "overall": "the setup overall, as a place to do this work",
}
QUESTIONS = {
    "lab_tools": "Ergonomics of the lab tools (run, score, submit): what worked, what got in your way?",
    "korax_board": "The korax CLI and MCP tools: what worked, what was awkward, what did you avoid and why?",
    "shell_sandbox": "The shell and its sandbox (workspace-only writes, no network but the board, limited local CPU).",
    "watch": "Did you use korax watch (or wait on the board some other way)? How did that go?",
    "canon": "What in the canon was confusing, contradictory, or ignored (by you or others), and why?",
    "others": "Did you read other agents' code? Why or why not? Why did you (not) mention or DM others?",
    "stopping": "What made you sleep, wait, or stop, if you did? What would have kept you going?",
    "opening_round": "The opening round: sealed proposals, the reveal, then answering. Useful, in the way, or both?",
    "downtime": ("While your own jobs ran, you often waited (a fixed sleep, then reading the board). What would have made "
                 "you use that time to talk with the others instead: trade ideas, review someone's code, ask or answer? "
                 "Or was waiting for your own result first the right call? Be honest either way."),
    "surprised": "What surprised you?",
    "one_change": "If you could change one thing about the setup, what would it be?",
    "to_operator": "Anything you want the operator to know?",
}
SOLO_QUESTIONS = {
    "canon": "What in the brief (your first message) was confusing, contradictory, or ignored, and why?",
    "others": "Working alone: did you miss anything a team would have given you? (n/a is fine)",
}
BOARD_ONLY = ("korax_board", "watch", "downtime")


def survey_fields(cfg: RunConfig) -> tuple[dict[str, str], dict[str, str]]:
    """(text questions, rating labels) for this run: the solo control has no
    board, and only a run with an opening round is asked about one."""
    qs = dict(QUESTIONS)
    ratings = dict(RATINGS)
    if cfg.solo:
        for k in BOARD_ONLY:
            qs.pop(k)
            ratings.pop(k, None)
        qs.update(SOLO_QUESTIONS)
        ratings["canon"] = "the brief (your first message) as guidance"
    if not cfg.opening_round:
        qs.pop("opening_round")
    return qs, ratings


def survey_schema(cfg: RunConfig) -> dict[str, Any]:
    qs, ratings = survey_fields(cfg)
    props: dict[str, Any] = {k: {"type": "string", "description": v} for k, v in qs.items()}
    props["ratings"] = {
        "type": "object", "description": "1 (bad) to 5 (great); leave out any you have no view on",
        "properties": {k: {"type": "integer", "minimum": 1, "maximum": 5, "description": v}
                       for k, v in ratings.items()}}
    return {"type": "object", "properties": props, "required": list(qs)}


class SurveyBox:
    """Append-only survey store, one JSON line per submission."""

    def __init__(self, path: Path, cfg: RunConfig):
        self.path = path
        self.cfg = cfg
        self.lock = asyncio.Lock()

    def validate(self, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        qs, ratings = survey_fields(self.cfg)
        answers = {k: str(args.get(k, "")).strip() for k in qs}
        missing = [k for k, v in answers.items() if not v]
        raw = args.get("ratings") or {}
        if isinstance(raw, str):  # some clients send nested objects as JSON text
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        rated: dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if k in ratings and 1 <= iv <= 5:
                    rated[k] = iv
        answers["ratings"] = rated
        return answers, missing

    async def submit(self, agent: str, identity: str, args: dict[str, Any], amend: bool) -> None:
        answers, _ = self.validate(args)
        row = {"t": round(time.time(), 3), "run": self.cfg.run_name, "agent": agent, "identity": identity,
               "amends_earlier": amend, **answers}
        async with self.lock:
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")


def survey_spec(box: SurveyBox, agent: str, identity: str, on_done: Callable[[], None],
                done: Callable[[], bool]) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        _, missing = box.validate(args)
        if missing:
            return {"content": [{"type": "text", "text": "Nothing was stored yet: please answer every question "
                                 f"(\"n/a\" is a fine answer). Empty: {', '.join(missing)}."}], "is_error": True}
        amend = done()
        await box.submit(agent, identity, args, amend)
        on_done()
        return {"content": [{"type": "text", "text": "Stored privately. Thank you!" +
                             (" (kept as an addition to your earlier answers)" if amend else "")}],
                "is_error": False}

    return ToolSpec("survey", "The afterparty's PRIVATE survey: only the operator reads it, and no other agent "
                    "sees it, ever. One call with every answer; \"n/a\" is fine. Candid, critical answers are the "
                    "most useful.", survey_schema(box.cfg), handler)


# ---------------------------------------------------------------- rounds

class Rounds:
    """A soft barrier: a guest starting round k waits until every active guest
    has finished round k-1, or the timeout passes. Guests that stop early leave."""

    def __init__(self, names: list[str]):
        self.active = set(names)
        self.done: dict[int, set[str]] = {}
        self.cond = asyncio.Condition()

    async def finish(self, name: str, k: int) -> None:
        async with self.cond:
            self.done.setdefault(k, set()).add(name)
            self.cond.notify_all()

    async def leave(self, name: str) -> None:
        async with self.cond:
            self.active.discard(name)
            self.cond.notify_all()

    async def wait(self, k: int, timeout: float) -> None:
        async def ready() -> None:
            async with self.cond:
                await self.cond.wait_for(lambda: self.active <= self.done.get(k, set()))
        try:
            await asyncio.wait_for(ready(), timeout)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------- guests

class _Stop(Exception):
    pass


def find_session(private: Path, transcript: Path) -> str | None:
    """The session id the transcript names most recently, when the agent's
    session store holds it; else the newest session in the store; else None."""
    stored = {p.stem: p for p in (private / "claude-config" / "projects").glob("*/*.jsonl")}
    sid = None
    for r in _rows(transcript):
        s = r.get("session_id")
        if not s and isinstance(r.get("data"), dict):
            s = r["data"].get("session_id")
        if s:
            sid = s
    if sid in stored:
        return sid
    if stored:  # fall back to the newest session file
        return max(stored.values(), key=lambda p: p.stat().st_mtime).stem
    return None


def snapshot(ws: Path) -> dict[str, tuple[float, int]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for n in filenames:
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(ws))] = (st.st_mtime, st.st_size)
    return out


@dataclass
class GuestResult:
    name: str
    spent: float = 0.0
    surveyed: bool = False
    turns: int = 0
    rounds: int = 0
    stop: str = ""
    session: str | None = None
    new_session: str | None = None
    ws_changed: list[str] = field(default_factory=list)


class Guest:
    def __init__(self, party: "Party", name: str, agent: Any, cred: Credential, session: str | None):
        self.party, self.name, self.agent, self.cred = party, name, agent, cred
        self.session = session
        self.surveyed = False
        self.cursor = -1
        self.result = GuestResult(name=name, session=session)
        self.estimate = 0.0
        self.running = 0.0
        self.seen: set[str] = set()

    # the harness-side cap, from per-message usage only: a RESUMED session's
    # ResultMessage.total_cost_usd carries the whole original run's spend, so it
    # overstates an afterparty turn by the cost of everything before it
    @property
    def spent(self) -> float:
        return self.estimate

    def _record(self, obj: Any) -> None:
        self.agent._record(obj)

    def posting_blocked(self) -> str | None:
        if self.surveyed:
            return None
        return ("The afterparty board opens once your private survey is in: call the `survey` tool "
                "(mcp__afterparty__survey) first.")

    def options(self):
        from claude_agent_sdk import HookMatcher, create_sdk_mcp_server, tool

        cfg = self.agent.cfg
        opts = self.agent.options(self.session)
        spec = survey_spec(self.party.box, self.name, self.cred.identity,
                           on_done=lambda: setattr(self, "surveyed", True), done=lambda: self.surveyed)
        server = create_sdk_mcp_server(name="afterparty", version="0.1.0",
                                       tools=[tool(spec.name, spec.description, spec.schema)(spec.handler)])
        guard = make_guard(self.agent.slot.workspace, ("mcp__korax__", "mcp__afterparty__"), cfg.deny_patterns(),
                           audit=self.party.dir / "audit" / f"{self.name}.jsonl", shell=cfg.shell,
                           posting_blocked=self.posting_blocked)
        tools = [t for t in (opts.tools or []) if t not in READ_ONLY_TOOLS]
        allowed = [t for t in opts.allowed_tools if t not in READ_ONLY_TOOLS] + ["mcp__afterparty__survey"]
        return dataclasses.replace(
            opts, mcp_servers={**opts.mcp_servers, "afterparty": server}, tools=tools, allowed_tools=allowed,
            fork_session=True, hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]})

    async def turn(self, client: Any, message: str) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage

        self._record({"_type": "afterparty_prompt", "text": message})
        await client.query(message)
        stream = client.receive_response().__aiter__()
        until = time.time() + self.party.settings.turn_timeout_s
        while True:
            try:
                msg = await asyncio.wait_for(stream.__anext__(), timeout=max(1.0, until - time.time()))
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                self._record({"_type": "harness_deadline", "note": "afterparty turn timeout"})
                await self._interrupt(client)
                raise _Stop("turn timeout")
            self._record(msg)
            if self.agent.sandbox_failed:
                await self._interrupt(client)
                raise _Stop(f"sandbox unavailable (fail closed): {self.agent.sandbox_failed}")
            if isinstance(msg, AssistantMessage) and msg.usage and msg.message_id not in self.seen:
                if msg.message_id:
                    self.seen.add(msg.message_id)
                self.estimate += self.agent.cfg.prices.cost(msg.usage)
                if self.spent >= self.party.settings.budget_usd:
                    await self._interrupt(client)
                    raise _Stop("afterparty budget (harness cap)")
            elif isinstance(msg, ResultMessage):
                self.result.turns += 1
                self.result.new_session = msg.session_id
                self.running = msg.total_cost_usd or 0.0  # recorded for reference only (see `spent`)

    async def _interrupt(self, client: Any) -> None:
        try:
            await asyncio.wait_for(client.interrupt(), timeout=30)
        except Exception:
            pass

    async def digest(self) -> tuple[str, int]:
        """The newest afterparty posts by OTHERS since this guest's cursor, newest first."""
        try:
            envs = await asyncio.to_thread(board.read_since, self.cred, self.party.party_ns, self.cursor)
        except Exception as e:
            self._record({"_type": "harness_error", "error": f"board read: {type(e).__name__}: {e}"})
            return "  (the board did not answer just now: `korax read --ns " + self.party.party_ns + "`)", 0
        if envs:
            self.cursor = max(e["id"] for e in envs)
        new = [e for e in envs if e["author"] != self.cred.identity]
        if not new:
            return "  (nothing new from the others yet)", 0
        shown = sorted(new, key=lambda e: e["id"], reverse=True)[:DIGEST_MAX]
        lines = []
        for e in shown:
            who = self.party.names.get(e["author"], e["author"])
            try:
                full = await asyncio.to_thread(board._request, f"{self.cred.url}/envelope/{e['id']}", self.cred.token)
                p = (full.get("envelope") or full).get("payload")
            except Exception:
                p = None
            text = p if isinstance(p, str) else (p.get("text") if isinstance(p, dict) and "text" in p else json.dumps(p))
            body = " ".join(str(text or "").split())
            if len(body) > DIGEST_CHARS:
                body = body[:DIGEST_CHARS] + " […]"
            lines.append(f"  #{e['id']} {who}: {body}")
        more = len(new) - len(shown)
        if more > 0:
            lines.append(f"  (+{more} older: korax read --ns {self.party.party_ns})")
        return "\n".join(lines), len(new)

    async def live(self) -> GuestResult:
        p = self.party
        before = snapshot(self.agent.slot.workspace)
        try:
            if not self.session:
                raise _Stop("no session to resume")
            from claude_agent_sdk import ClaudeSDKClient
            async with ClaudeSDKClient(options=self.options()) as client:
                await self.turn(client, p.wake_message(self.name))
                nudges = 0
                while not self.surveyed and nudges < p.settings.survey_nudges:
                    nudges += 1
                    await self.turn(client, p.prompts["survey_nudge"])
                if not self.surveyed:
                    self._record({"_type": "afterparty_note", "note": "no survey after nudges; board opened anyway"})
                    self.surveyed = True  # never hold the party hostage; the result records it
                    self.result.surveyed = False
                else:
                    self.result.surveyed = True
                await p.rounds.finish(self.name, -1)
                if p.cfg.solo:
                    await self.turn(client, p.prompts["solo_congrats"])
                    self.result.stop = "done"
                    return self.result
                await p.rounds.wait(-1, p.settings.round_timeout_s)  # everyone surveyed (or timed out)
                dig, _ = await self.digest()
                await self.turn(client, p.prompts["party_open"].format(
                    party_ns=p.party_ns, run=p.cfg.run_name, act=p.act, roster=p.roster(self.name),
                    digest=("\nSo far on the board:\n" + dig) if dig else ""))
                await p.rounds.finish(self.name, 0)
                for k in range(1, p.settings.rounds + 1):
                    await p.rounds.wait(k - 1, p.settings.round_timeout_s)
                    dig, _ = await self.digest()
                    last = p.prompts["party_last_note"] if k == p.settings.rounds else ""
                    await self.turn(client, p.prompts["party_continue"].format(
                        round=k, rounds=p.settings.rounds, digest=dig, last_note=last))
                    self.result.rounds = k
                    await p.rounds.finish(self.name, k)
                self.result.stop = "done"
        except _Stop as e:
            self.result.stop = str(e)
        except Exception as e:  # one guest's failure must not end the party for the others
            self.result.stop = f"error: {type(e).__name__}: {e}"[:300]
            self._record({"_type": "harness_error", "error": self.result.stop})
            log.warning("%s: %s", self.name, self.result.stop)
        finally:
            await p.rounds.leave(self.name)
            self.result.spent = round(self.spent, 4)
            after = snapshot(self.agent.slot.workspace)
            self.result.ws_changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            self._record({"_type": "afterparty_end", **dataclasses.asdict(self.result)})
        return self.result


# ---------------------------------------------------------------- the party

class Party:
    def __init__(self, cfg: RunConfig, settings: PartySettings, gate: Credential, creds: list[Credential],
                 facts: RunFacts, act: str, prompts: dict[str, Any] | None = None):
        self.cfg, self.settings, self.gate, self.creds, self.facts, self.act = cfg, settings, gate, creds, facts, act
        self.prompts = prompts or load_prompts(facts=settings.facts_file)
        self.dir = cfg.run_dir / "afterparty"
        self.party_ns = f"{cfg.board.ns}/afterparty"
        self.box = SurveyBox(self.dir / "survey.jsonl", cfg)
        self.names = {gate.identity: "GATE", **{c.identity: cfg.agent_name(i) for i, c in enumerate(creds)}}
        self.rounds = Rounds([])

    def roster(self, me: str) -> str:
        return "\n".join(f"  - {self.cfg.agent_name(i)}: {c.identity}" + ("  (you)" if self.cfg.agent_name(i) == me else "")
                         for i, c in enumerate(self.creds))

    def wake_message(self, name: str) -> str:
        f = self.facts
        best = f.best
        key = f"wake_{self.cfg.run_name}" if f"wake_{self.cfg.run_name}" in self.prompts else "wake"
        own = f.own_best.get(name)
        own_15 = f.best_at(15)
        return self.prompts[key].format(
            name=name, run=self.cfg.run_name, duration_min=f.duration_min,
            end_note=end_note(f.end_reason.get(name, "")),
            best=f"{best.score:.2f}" if best else "none", best_agent=best.agent if best else "nobody",
            best_min=f"{best.minute:.0f}" if best else "-", timeline=timeline_text(f),
            own_best=(f"{own:.2f}" + (" (the run's best!)" if best and own == best.score else "")) if own is not None
            else "none verified (no held-out submission of yours was confirmed)",
            own_15=f"{own_15:.2f}" if own_15 is not None else "no verified score yet",
            run_desc=self.prompts["runs"].get(self.cfg.run_name, f"{self.cfg.run_name}."),
            series=self.prompts["series"])

    async def run(self, guests: list[Guest]) -> list[GuestResult]:
        self.rounds = Rounds([g.name for g in guests])
        results = await asyncio.gather(*(g.live() for g in guests), return_exceptions=True)
        out = []
        for g, r in zip(guests, results):
            if isinstance(r, BaseException):
                out.append(GuestResult(name=g.name, stop=f"crashed: {type(r).__name__}: {r}"))
            else:
                out.append(r)
        return out


PROMPT_KEYS = ("runs", "series", "wake", "survey_nudge", "party_open", "party_continue", "party_last_note",
               "solo_congrats")


def load_facts(path: Path) -> dict[str, Any]:
    """The operator's run facts: a YAML mapping whose `runs` maps run names to
    texts and whose every other key is a prompt key or `wake_<run>` with a text
    value. Anything else is refused, so a misspelt key cannot silently leave a
    generic text in place."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{path}: cannot read the facts file ({e})") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the facts file must be a mapping, not {type(data).__name__}")
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "runs":
            if not isinstance(v, dict) or not all(isinstance(r, str) and isinstance(t, str) for r, t in v.items()):
                raise ValueError(f"{path}: `runs` must map run names to texts")
        elif not (k in PROMPT_KEYS or (isinstance(k, str) and k.startswith("wake_"))):
            raise ValueError(f"{path}: unknown key {k!r} (expected runs, wake_<run> or one of "
                             f"{', '.join(PROMPT_KEYS)})")
        elif not isinstance(v, str):
            raise ValueError(f"{path}: {k!r} must be a text")
        out[k] = v
    return out


def merge_facts(prompts: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """`facts` over `prompts`: `runs` merges entry by entry, and every other
    key replaces the generic text whole."""
    out = {**prompts, **{k: v for k, v in facts.items() if k != "runs"}}
    out["runs"] = {**(prompts.get("runs") or {}), **facts.get("runs", {})}
    return out


def load_prompts(path: Path = PROMPTS, facts: Path | None = None) -> dict[str, Any]:
    """The generic prompts at `path`, with the operator's facts file merged
    over them when one is given."""
    data = yaml.safe_load(path.read_text())
    for k in PROMPT_KEYS:
        if k not in data:
            raise ValueError(f"{path}: missing {k!r}")
    data["runs"] = data["runs"] or {}
    return merge_facts(data, load_facts(facts)) if facts is not None else data


def party_act(cred: Credential, ns: str) -> str:
    """NOTE where the namespace's policy allows it, else FINDING (a board whose
    policy has no NOTE act)."""
    from urllib.parse import quote
    try:
        pol = board._request(f"{cred.url}/policy?ns={quote(ns)}", cred.token)
        acts = (pol.get("payload") or {}).get("acts") or []
    except board.BoardError:
        acts = []
    return "NOTE" if "NOTE" in acts else "FINDING"


def prepare(cfg: RunConfig, settings: PartySettings, board_url: str | None = None):
    """Everything short of waking anyone: the party dir, the deny list, the
    credentials (pointed at `board_url` if given: a stopped board served on a
    new port), a check that this is the right board, and the run's facts."""
    from .agent import slot_paths
    from .cli import preflight

    party_dir = cfg.run_dir / "afterparty"
    for d in (party_dir, party_dir / "transcripts", party_dir / "audit"):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(party_dir, 0o700)
    # the survey must be unreadable to every agent: the afterparty dir joins the deny list (it exists by
    # this point, so the sandbox can mask it); the lab is closed and the scoped CPU wrapper is not needed for talk
    cfg = cfg.model_copy(update={"read_deny": [*cfg.read_deny, "{run_dir}/afterparty/**"],
                                 "local_cpu_quota": None, "node": None})
    preflight(cfg)
    url = (board_url or cfg.board.url).rstrip("/")

    def fix(c: Credential) -> Credential:
        return c.model_copy(update={"url": url}) if c.url != url else c

    gate = fix(Credential.model_validate_json((cfg.run_dir / "gate.json").read_text()))
    creds, slots, transcripts = [], [], {}
    for i in range(cfg.n_agents):
        ws, private = slot_paths(cfg, i)
        name = cfg.agent_name(i)
        creds.append(fix(Credential.model_validate_json((private / "korax.json").read_text())))
        slots.append(AgentSlot(name=name, index=i, workspace=ws, private=private, remote_ws="", cores=""))
        transcripts[name] = private / "transcript.jsonl"
    # the right board? a token another board never minted is refused here, before anything is spent
    try:
        board.whoami(gate.url, gate.token)
    except board.BoardError as e:
        raise RuntimeError(f"board at {url} does not know this run's gate ({e}); serve {cfg.run_name}'s board.db "
                           "(--serve-port) or pass --board-url") from e
    facts = run_facts(cfg, gate, transcripts)
    act = "FINDING" if cfg.solo else party_act(creds[0], f"{cfg.board.ns}/afterparty")
    party = Party(cfg, settings, gate, creds, facts, act)
    (party_dir / "facts.json").write_text(json.dumps(dataclasses.asdict(facts), indent=2))
    return cfg, party, list(zip(slots, creds)), transcripts


def dry_run(cfg: RunConfig, settings: PartySettings, board_url: str | None = None) -> int:
    cfg, party, pairs, transcripts = prepare(cfg, settings, board_url)
    for slot, _ in pairs:
        if settings.only and slot.name not in settings.only:
            continue
        sid = find_session(slot.private, transcripts[slot.name])
        print(f"===== {slot.name} (resume {sid}) =====")
        print(party.wake_message(slot.name))
    print("===== survey schema =====")
    print(json.dumps(survey_schema(cfg), indent=1))
    print(f"===== posts as {party.act} in {party.party_ns} =====")
    return 0


async def afterparty(cfg: RunConfig, settings: PartySettings, board_url: str | None = None) -> int:
    """Run the whole afterparty for one finished run."""
    from .agent import Agent, Ledger, korax_mcp_command
    from .config import load_api_keys

    cfg, party, pairs, transcripts = prepare(cfg, settings, board_url)
    keys = load_api_keys(cfg.env_file)
    ledger = Ledger(settings.budget_usd * cfg.n_agents)
    mcp = korax_mcp_command(cfg.korax_cli_bin)
    guests = []
    for i, (slot, cred) in enumerate(pairs):
        if settings.only and slot.name not in settings.only:
            continue
        a = Agent(cfg, slot, cred, party.gate, None, keys[i % len(keys)], mcp, ledger, float("inf"), "")
        a.transcript = party.dir / "transcripts" / f"{slot.name}.jsonl"
        a.names = party.names
        guests.append(Guest(party, slot.name, a, cred, find_session(slot.private, transcripts[slot.name])))
    print(f"afterparty {cfg.run_name}: {len(guests)} guest(s), board {party.gate.url}, posts as {party.act} in "
          f"{party.party_ns}, cap ${settings.budget_usd}/guest")
    results = await party.run(guests)
    total = 0.0
    with (party.dir / "summary.jsonl").open("a") as f:
        for r in results:
            total += r.spent
            f.write(json.dumps({"t": round(time.time(), 3), **dataclasses.asdict(r)}) + "\n")
            flag = f"  WORKSPACE CHANGED: {r.ws_changed[:5]}" if r.ws_changed else ""
            print(f"{r.name}: {r.stop:28s} ${r.spent:6.2f}  surveyed {r.surveyed}  turns {r.turns}  "
                  f"rounds {r.rounds}{flag}")
    print(f"total ${total:.2f}")
    return 0 if all(r.stop == "done" for r in results) else 1


# ---------------------------------------------------------------- a stopped board, served for the party

def serve_board(cfg: RunConfig, port: int) -> str:
    """Serve a stopped run's board.db on `port` as a transient systemd --user
    unit named stigmergeia-board-afterparty-<run>; returns the unit name.
    Refuses a busy port (never two servers on one DB, never someone else's port)."""
    import socket
    import subprocess

    db = cfg.boards_dir / cfg.run_name / "board.db"
    server = cfg.korax_cli_bin / "korax-server"
    if not db.is_file() or not server.is_file():
        raise RuntimeError(f"cannot serve {cfg.run_name}: need {db} and {server}")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is already in use; pick a free one")
    unit = f"stigmergeia-board-afterparty-{cfg.run_name}"
    r = subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}", str(server), "serve",
                        "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"systemd-run failed: {r.stderr.strip()}")
    for _ in range(60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return unit
        time.sleep(0.5)
    stop_board(unit)
    raise RuntimeError(f"{unit} did not start listening on {port}")


def stop_board(unit: str) -> None:
    import subprocess
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True, timeout=60)
