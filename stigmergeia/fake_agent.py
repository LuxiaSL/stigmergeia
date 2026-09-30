"""A scripted agent: the whole harness, with no model and no API key.

`backend: fake` drives an agent through the SAME tool layer a Codex agent uses
(`codex_agent.CodexAgent`): every call is judged by the guard, answered by the
real tool (the Korax MCP bridge, the lab, the opening round), and written to
the transcript in the rows the panel and every transcript reader expect. Only
the decisions are scripted. That makes it the harness's end-to-end test and
the quickstart: a run of fake agents exercises onboarding, the opening round,
posting with edges, scoring and held-out submits, background jobs and the
panel, on a machine with nothing but this repository.

What it does is deliberately plain, and seeded per agent so a run is
reproducible given the same board timing:

1. Onboards: reads the canon it is shown and acks it.
2. In a run with an opening round: proposes, then answers in turn.
3. Then, until the run is ended, repeats: write a snake policy (a
   breadth-first apple chaser with a seeded exploration rate and, sometimes, a
   flood-fill safety check), score it, submit it, and post what it did. Now
   and then it starts from another agent's best policy instead, copies it
   across and says so with a `derives-from` edge, which is the genealogy the
   panel draws. A policy that does worse than its parent is posted as a
   scoped WARN.

The policies it writes are ordinary task code: they run in the lab exactly as
a model's would, and the gate scores them the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
from pathlib import Path
from typing import Any

from .agent import AgentState
from .codex_agent import CodexAgent

POLICY_TEMPLATE = '''"""{doc}"""
import random
from collections import deque

DELTA = {{0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}}
EPS = {eps}
SAFETY = {safety}


def _free(s, start, blocked):
    seen, q = {{start}}, deque([start])
    while q:
        x, y = q.popleft()
        for dx, dy in DELTA.values():
            n = (x + dx, y + dy)
            if 0 <= n[0] < s.width and 0 <= n[1] < s.height and n not in blocked and n not in seen:
                seen.add(n)
                q.append(n)
    return len(seen)


class Policy:
    def __init__(self, width, height):
        self.rng = random.Random({seed})

    def act(self, s):
        blocked = set(s.body[:-1])
        hx, hy = s.body[0]
        safe = [m for m, (dx, dy) in DELTA.items()
                if 0 <= hx + dx < s.width and 0 <= hy + dy < s.height and (hx + dx, hy + dy) not in blocked]
        if not safe:
            return s.heading
        if SAFETY:
            roomy = [m for m in safe
                     if _free(s, (hx + DELTA[m][0], hy + DELTA[m][1]), blocked) >= len(s.body)]
            safe = roomy or safe
        if self.rng.random() < EPS:
            return self.rng.choice(safe)
        first, seen, q = {{}}, {{s.body[0]}}, deque([s.body[0]])
        while q:
            cur = q.popleft()
            if cur == s.apple:
                m = first[cur]
                return m if m in safe else self.rng.choice(safe)
            for m, (dx, dy) in DELTA.items():
                n = (cur[0] + dx, cur[1] + dy)
                if 0 <= n[0] < s.width and 0 <= n[1] < s.height and n not in blocked and n not in seen:
                    seen.add(n)
                    first[n] = first.get(cur, m)
                    q.append(n)
        return safe[0]
'''

PARAMS = re.compile(r"^EPS = ([0-9.]+)\nSAFETY = (True|False)$", re.M)


class _NoServer:
    """Stands in for the Codex app-server: a fake agent has no model process."""


class FakeAgent(CodexAgent):
    """A CodexAgent whose turns are scripted instead of generated."""

    def __init__(self, *a: Any, **kw: Any):
        kw.setdefault("catalog", Path("/dev/null"))
        super().__init__(*a, **kw)
        self.slice = None  # no model process to scope
        self.cpus = None
        self.rng = random.Random(f"{self.cfg.run_name}:{self.slot.name}:{self.cfg.fake.seed}")
        self.pace_s = self.cfg.fake.pace_s
        self.version = 0
        self.best: tuple[float, str] | None = None  # (score, policy file) of this agent's best training score
        self.phase = "onboard"
        self.calls = 0

    # -- the Codex plumbing a fake agent does not have
    async def _connect(self, stack: contextlib.AsyncExitStack) -> Any:
        self.thread_id = self.thread_id or f"fake-{self.slot.name}"
        self._record({"_type": "codex_thread", "resumed": False, "thread": self.thread_id, "fake": True,
                      "tools": sorted(self.tools)})
        return _NoServer()

    async def _call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """One tool call through the real path: transcript, guard, tool, pulse."""
        self.calls += 1
        ns, _, tool = name.partition(".")
        params = {"tool": tool, "namespace": ns, "callId": f"fake-{self.slot.name}-{self.calls}",
                  "arguments": args} if tool else {"tool": name, "callId": f"fake-{self.slot.name}-{self.calls}",
                                                   "arguments": args}
        res = await self._tool_call(params)
        text = "\n".join(i.get("text", "") for i in res.get("contentItems", []))
        await asyncio.sleep(self.pace_s * self.rng.uniform(0.5, 1.5))
        return text, not res.get("success", True)

    def _say(self, text: str) -> None:
        self._record({"_type": "AssistantMessage", "content": [{"type": "text", "text": text}],
                      "model": self.cfg.model, "message_id": None, "usage": None})

    # -- the script
    async def _turn(self, srv: Any, message: str) -> bool:
        self._active = False
        if self.phase == "onboard":
            await self._onboard()
            self.phase = "round" if self.opening_round is not None else "work"
        if self.phase == "round":
            await self._opening_round()
            if self.slot.name in self.opening_round.answered:
                self.phase = "work"
            else:
                self._record({"_type": "ResultMessage", "total_cost_usd": 0.0, "fake": True})
                self.state.turns += 1
                return True  # try the round again next turn
        for _ in range(3):  # a few experiments per turn, then the harness's continue message
            if self._stop_reason():
                break
            await self._experiment()
        self._record({"_type": "ResultMessage", "total_cost_usd": 0.0, "fake": True})
        self.state.turns += 1
        return self._active

    async def _onboard(self) -> None:
        self._say("Onboarding: reading the canon before anything else.")
        text, _ = await self._call("korax.korax_onboard", {"fetch": True})
        ids = sorted({int(m) for m in re.findall(r'"id":\s*(\d+)', text)})
        try:
            view = json.loads(text)
            unread = [d["id"] for d in view.get("canon", []) if not d.get("read")]
            ids = unread or ids
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
            pass
        if ids:
            await self._call("korax.korax_ack", {"ids": ids})

    async def _opening_round(self) -> None:
        eps = self.rng.choice([0.0, 0.02, 0.05])
        safety = self.rng.random() < 0.5
        self._planned = (eps, safety)
        pad = " The scripted agent writes the same kind of policy every time and varies two knobs."
        await self._call("lab.propose", {
            "problem": "Snake on a 10x10 grid in 1000 steps: an apple chaser dies once its body can trap it, so "
                       "the score is lost in the late game, when free space shrinks." + pad,
            "plan": f"A breadth-first apple chaser with exploration rate {eps} and the flood-fill safety check "
                    f"{'on' if safety else 'off'}; then vary both and build on whoever is ahead." + pad,
            "risks": "Random exploration may cost more apples than it saves, and the safety check may be too "
                     "slow or too timid; the training score against the parent policy will say which." + pad})
        text = (f"Taking exploration {eps} with the flood-fill safety check {'on' if safety else 'off'}. "
                "It differs from the answers before mine in at least one of the two knobs, so between us the "
                "swarm covers more of the space; I will build on whichever setting the gate scores highest.")
        reveal, _ = await self._call("lab.answer", {"text": text, "replies": []})
        ids = [int(x) for x in re.findall(r"#(\d+)", reveal)][:3]
        for _ in range(60):  # the first call may only show earlier answers; a later one posts
            if self._stop_reason():
                break
            out, err = await self._call("lab.answer", {"text": text, "replies": ids})
            if err:
                self._say(f"The round refused my answer: {out[:200]}")
                break
            if self.slot.name in self.opening_round.answered:
                break

    def _peer_best(self) -> Path | None:
        peers = [p for p in self.slot.workspace.parent.iterdir()
                 if p.is_dir() and p.name != self.slot.name and list(p.glob("policy_v*.py"))]
        if not peers:
            return None
        peer = self.rng.choice(peers)
        return max(peer.glob("policy_v*.py"), key=lambda f: f.stat().st_mtime)

    async def _experiment(self) -> None:
        self.version += 1
        parent: Path | None = None
        eps, safety = getattr(self, "_planned", (0.05, False))
        if self.version > 1 and self.rng.random() < 0.4:
            parent = self._peer_best()
        if parent is not None:
            m = PARAMS.search(parent.read_text())
            if m:
                eps, safety = float(m.group(1)), m.group(2) == "True"
        eps = round(max(0.0, eps + self.rng.choice([-0.02, 0.0, 0.01, 0.03])), 3)
        if self.rng.random() < 0.25:
            safety = not safety
        name = f"policy_v{self.version}.py"
        doc = f"Scripted agent {self.slot.name}, version {self.version}" + (
            f", from {parent.parent.name}/{parent.name}" if parent else "")
        (self.slot.workspace / name).write_text(POLICY_TEMPLATE.format(
            doc=doc, eps=eps, safety=safety, seed=self.rng.randrange(1 << 30)))
        self._active = True
        self._say(f"Trying exploration {eps}, safety {'on' if safety else 'off'}"
                  + (f", starting from {parent.parent.name}'s {parent.name}." if parent else "."))
        text, err = await self._call("lab.score", {"policy": name})
        score = _mean(text)
        if err or score is None:
            await self._call("korax.korax_post", {
                "ns": self.cfg.board.ns, "type": "NOTE", "grade": "n/a",
                "payload": f"{name} did not score: {text[:200]}"})
            return
        await self._call("lab.submit", {"policy": name})
        refs = await self._parent_ref(parent)
        if self.best is not None and score < self.best[0] - 1.0 and parent is not None:
            await self._call("korax.korax_post", {
                "ns": self.cfg.board.ns, "type": "WARN", "grade": "n/a", "refs": refs,
                "payload": (f"Tested on: {name} (exploration {eps}, safety {safety}), from "
                            f"{parent.parent.name}/{parent.name}, scored with lab score.\n"
                            f"Result: training mean {score:.2f}, below my best {self.best[0]:.2f}.\n"
                            "Revive if: the exploration rate is tuned together with the safety check.")})
        else:
            await self._call("korax.korax_post", {
                "ns": self.cfg.board.ns, "type": "NOTE", "grade": "n/a", "refs": refs,
                "payload": f"{name}: training mean {score:.2f} (exploration {eps}, safety {safety}); "
                           f"submitted for held-out scoring. Code at ../{self.slot.name}/{name}."})
        if self.best is None or score > self.best[0]:
            self.best = (score, name)
        if self.rng.random() < 0.3:
            await self._call("lab.wait", {})

    async def _parent_ref(self, parent: Path | None) -> list[dict[str, Any]]:
        """A derives-from edge to the post that announced `parent`, if the board has one."""
        if parent is None:
            return []
        text, err = await self._call("korax.korax_search", {"q": f"{parent.name}", "ns": self.cfg.board.ns,
                                                            "limit": 20})
        if err:
            return []
        try:
            hits = json.loads(text).get("results", [])
        except (json.JSONDecodeError, AttributeError):
            return []
        for e in hits:  # the author's own post announcing that file (excerpts are truncated)
            if (self.names.get(e.get("author", "")) == parent.parent.name
                    and str(e.get("excerpt") or "").startswith(f"{parent.name}:")):
                return [{"edge": "derives-from", "id": int(e["id"])}]
        return []

    async def live(self) -> AgentState:
        return await super().live()


def _mean(text: str) -> float | None:
    """The mean score in a lab score result (its JSON, or the first 'mean' number)."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("mean"), (int, float)):
            return float(obj["mean"])
    except json.JSONDecodeError:
        pass
    m = re.search(r"mean\W{0,4}([0-9]+(?:\.[0-9]+)?)", text)
    return float(m.group(1)) if m else None
