"""The opening round: sealed proposals, revealed together, then answers in turn.

Without an opening round, each agent onboards, writes a quick policy,
submits it, and only then posts, so the first round is N independent solo
attempts that converge on the obvious ideas, and the swarm collapses onto
one line early. Early talk instead anchors everyone on the first confident
post. So the opening round keeps agents independent until one moment,
then couples them:

1. After onboarding, each agent submits a proposal with the lab's
   `propose` tool: its read of the problem, what it would try, and what it
   expects to be hardest. The harness holds it privately; nothing reaches
   the board, and the agent cannot post or DM, until the reveal.
2. When every agent has proposed (or `reveal_after_s` after the start, as a
   safety net), the harness posts every proposal to the board at once, each
   under its author's own identity, and every blocked `propose` call returns
   the full set.
3. Then the agents ANSWER, one at a time, in a fixed order, so each answer
   can take the earlier ones into account (answers given all at once tend
   to converge on the same idea, then flip together to the same
   complement). The lab's `answer` tool waits until every agent before
   this one has answered (or had its turn and let it pass), shows this
   agent every answer posted so far, and only then posts its answer, as a
   PROPOSAL under its own identity with `replies` edges. An agent whose
   turn passes (`answer_turn_s`) is skipped and may still answer later.

The answer order is agent index order. The agents are identical up to
their names, so index order is uncorrelated with anything about them. The
order in which proposals were sealed was the alternative, and it is not
neutral: it puts the FASTEST proposer first, which can be an agent that
sealed within seconds without reading the task files, and the first answer
is the one every later answer reads.

Until an agent's answer is in, it cannot post or DM, and its lab stays
closed; both open the moment the answer is posted. Every event is logged
as one JSON line to `log_path`, outside every workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import board
from .board import Credential

log = logging.getLogger("swarm")

PROPOSAL_MIN_CHARS = 200
ANSWER_MIN_CHARS = 150


@dataclass
class Answer:
    name: str
    id: int
    text: str
    position: int  # 1-based place in the answer order
    late: bool  # posted after its turn had passed


@dataclass
class OpeningRound:
    ns: str
    names: list[str]  # every agent in the run, in order
    creds: dict[str, Credential]
    reveal_after_s: float
    answer_turn_s: float = 90.0
    started: float = field(default_factory=time.time)
    proposals: dict[str, str] = field(default_factory=dict)
    submitted_at: dict[str, float] = field(default_factory=dict)
    posted: dict[str, int] = field(default_factory=dict)  # name -> envelope id of its revealed proposal
    revealed: asyncio.Event = field(default_factory=asyncio.Event)
    revealed_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    log_path: Any = None  # Path: a JSONL record of the round, outside every workspace
    # -- the answers, in turn
    order: list[str] = field(default_factory=list)  # fixed at the reveal; late proposers are appended
    turn: int = -1  # order[:turn + 1] have had their turn begin
    turn_started: float | None = None
    answers: list[Answer] = field(default_factory=list)  # in posting order
    answered: dict[str, int] = field(default_factory=dict)  # name -> envelope id of its answer
    skipped: set[str] = field(default_factory=set)
    shown: dict[str, int] = field(default_factory=dict)  # name -> how many answers it has been shown
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _turns_task: asyncio.Task | None = None

    def _log(self, **row: Any) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"t": round(time.time(), 3), **row}) + "\n")
        except OSError as e:  # the record is for analysis; it must never break the round
            log.error("opening round: could not write %s: %s", self.log_path, e)

    # -- waiting on state changes (single-threaded asyncio: no lock needed to read state)
    def _notify(self) -> None:
        ev, self._changed = self._changed, asyncio.Event()
        ev.set()

    async def _wait_until(self, pred: Callable[[], bool], timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + timeout
        while not pred():
            ev = self._changed
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                return pred()
            try:
                await asyncio.wait_for(ev.wait(), left)
            except asyncio.TimeoutError:
                return pred()
        return True

    # -- the sealed round
    async def propose(self, name: str, text: str) -> str:
        """Seal `name`'s proposal; block until the reveal; return every proposal."""
        text = text.strip()
        if len(text) < PROPOSAL_MIN_CHARS:
            return (f"A proposal needs substance (at least {PROPOSAL_MIN_CHARS} characters; yours has {len(text)}): "
                    "your read of the problem, what you would try and why, and what you expect to be hardest "
                    "or most likely wrong. Nothing was sealed; call propose again.")
        async with self.lock:
            if name in self.proposals:
                return self._already(name)
            self.proposals[name] = text
            self.submitted_at[name] = time.time()
            self._log(event="sealed", agent=name, chars=len(text), t_since_start=round(time.time() - self.started, 1))
            late = self.revealed.is_set()
            everyone = len(self.proposals) == len(self.names)
        if late:
            await self._post_one(name)  # after the reveal: posted at once, and queued to answer last
            if name not in self.order:
                self.order.append(name)
                self._log(event="queued_late", agent=name, position=len(self.order))
                self._notify()
        elif everyone:
            await self.reveal("all proposals in")
        await self.revealed.wait()
        return self.render_for(name)

    def _already(self, name: str) -> str:
        if self.revealed.is_set():
            return "You already proposed. " + self.render_for(name)
        return ("You already proposed; it is sealed. The call that sealed it returns every proposal at the reveal. "
                "Nothing more to do here until then.")

    async def reveal(self, why: str) -> None:
        async with self.lock:
            if self.revealed.is_set():
                return
            names = [n for n in self.names if n in self.proposals]
            for n in names:
                await self._post_one(n)
            self.revealed_at = time.time()
            self.order = list(names)
            missing = [n for n in self.names if n not in self.proposals]
            self._log(event="revealed", why=why, t_since_start=round(self.revealed_at - self.started, 1),
                      proposed=names, missing=missing, order=self.order, answer_turn_s=self.answer_turn_s)
            log.info("opening round: revealed %d proposals (%s); missing %s", len(names), why, missing or "none")
            self.revealed.set()
            self._turns_task = asyncio.create_task(self._run_turns())

    async def _post_one(self, name: str) -> None:
        if name in self.posted:
            return
        stamp = "revealed together with the others" if not self.revealed.is_set() else "submitted after the reveal"
        payload = {"text": f"{name}: opening proposal (sealed, {stamp}).\n\n{self.proposals[name]}",
                   "kind": "opening-proposal", "agent": name}
        try:
            out = await asyncio.to_thread(board.post, self.creds[name], self.ns, "PROPOSAL", payload)
            self.posted[name] = int(out.get("id", -1))
        except Exception as e:
            self._log(event="post_error", agent=name, error=f"{type(e).__name__}: {e}")
            log.error("opening round: posting %s's proposal failed: %s", name, e)

    async def deadline(self) -> None:
        """The safety net: reveal whatever is in after reveal_after_s."""
        try:
            await asyncio.wait_for(self.revealed.wait(), timeout=self.reveal_after_s)
        except asyncio.TimeoutError:
            await self.reveal(f"deadline ({self.reveal_after_s:.0f}s)")

    def close(self) -> None:
        if self._turns_task is not None:
            self._turns_task.cancel()

    # -- the answers, in turn
    async def _run_turns(self) -> None:
        """Give each agent in `order` its turn: it ends when that agent has
        answered, or after answer_turn_s (skipped: it may answer later)."""
        k = 0
        try:
            while True:
                await self._wait_until(lambda: k < len(self.order))
                name = self.order[k]
                self.turn, self.turn_started = k, time.time()
                self._notify()
                if name in self.answered:  # a late proposer can have answered before its turn (never: guarded)
                    k += 1
                    continue
                self._log(event="turn", agent=name, position=k + 1, of=len(self.order))
                ok = await self._wait_until(lambda: name in self.answered, self.answer_turn_s)
                if not ok:
                    self.skipped.add(name)
                    self._log(event="skipped", agent=name, position=k + 1, after_s=self.answer_turn_s)
                    log.info("opening round: %s let its turn pass (%ss); next", name, self.answer_turn_s)
                k += 1
        except asyncio.CancelledError:
            pass

    def _turn_came(self, name: str) -> bool:
        return name in self.order and self.order.index(name) <= self.turn

    def _position(self, name: str) -> int | None:
        return self.order.index(name) + 1 if name in self.order else None

    def _render_answers(self, answers: list[Answer]) -> str:
        return "\n".join(f"=== answer {i}: {a.name}, #{a.id}{' (after its turn)' if a.late else ''} ===\n{a.text}\n"
                         for i, a in enumerate(answers, start=1))

    def _order_line(self, name: str) -> str:
        pos = self._position(name)
        whose = self.order[self.turn] if 0 <= self.turn < len(self.order) else None
        now = (f"current turn: #{self.turn + 1} ({whose})" if whose else "no turn has started yet")
        return (f"Answer order (agent index order): {', '.join(self.order)}. You are #{pos}; {now}; "
                f"{len(self.answers)} answer(s) posted so far.")

    async def answer(self, name: str, text: str, replies: list[int]) -> tuple[str, bool]:
        """Post `name`'s answer when its turn has come and it has seen every
        answer posted so far. Returns (message, is_error)."""
        text = text.strip()
        if not self.revealed.is_set():
            return ("The opening round has not been revealed yet: first `propose`; its call returns every "
                    "proposal at the reveal. Nothing was posted.", True)
        if name not in self.proposals:
            return ("Propose first (the lab's `propose` tool); after the reveal a late proposal is posted at once "
                    "and you join the end of the answer order. Nothing was posted.", True)
        if name in self.answered:
            return (f"You already answered (#{self.answered[name]}). The lab, posting and messaging are open "
                    "for you.", False)
        if len(text) < ANSWER_MIN_CHARS:
            return (f"An answer needs substance (at least {ANSWER_MIN_CHARS} characters; yours has {len(text)}): "
                    "which direction you take and why it adds most to the swarm's chance of beating the best. "
                    "Nothing was posted.", True)
        waited = time.time()
        if not self._turn_came(name):
            self._log(event="waiting_for_turn", agent=name, position=self._position(name))
            await self._wait_until(lambda: self._turn_came(name) or name in self.answered)
        seen = self.shown.get(name, 0)
        unseen = self.answers[seen:]
        if unseen:
            self.shown[name] = len(self.answers)
            self._log(event="shown", agent=name, answers=len(self.answers), waited_s=round(time.time() - waited, 1))
            turn_left = ""
            if self.order and 0 <= self.turn < len(self.order) and self.order[self.turn] == name and self.turn_started:
                turn_left = (f" Your turn has {max(0, int(self.answer_turn_s - (time.time() - self.turn_started)))} s "
                             "left before it passes to the next agent (you can still answer after that).")
            return (f"Your turn to answer has come. {self._order_line(name)}\n\n"
                    f"Answers posted since you last looked ({len(unseen)}):\n\n{self._render_answers(unseen)}\n"
                    "Your answer was NOT posted yet, because you had not seen these. Revise it if they change "
                    f"anything (or keep it) and call `answer` again: it posts at once.{turn_left}", False)
        refs = [{"edge": "replies", "id": int(i)} for i in replies]
        late = name in self.skipped
        pos = self._position(name) or 0
        payload = {"text": f"{name}: answer to the opening round (#{pos} in turn{', after its turn passed' if late else ''}).\n\n{text}",
                   "kind": "opening-answer", "agent": name, "position": pos}
        try:
            out = await asyncio.to_thread(board.post, self.creds[name], self.ns, "PROPOSAL", payload, refs)
        except Exception as e:
            self._log(event="answer_post_error", agent=name, error=f"{type(e).__name__}: {e}")
            return (f"The board refused the answer ({type(e).__name__}: {str(e)[:300]}). Check the `replies` ids "
                    "(envelope ids of proposals or answers) and call `answer` again. Nothing was posted.", True)
        env_id = int(out.get("id", -1))
        self.answered[name] = env_id
        self.answers.append(Answer(name, env_id, text, pos, late))
        self.shown[name] = len(self.answers)
        since_turn = round(time.time() - self.turn_started, 1) if self.turn_started else None
        self._log(event="answered", agent=name, id=env_id, position=pos, late=late, chars=len(text),
                  replies=[int(i) for i in replies], t_since_reveal=round(time.time() - (self.revealed_at or 0), 1),
                  t_since_turn_start=since_turn if not late else None)
        self._notify()
        return (f"Answer posted as #{env_id} in {self.ns}. The lab is open for you now, and so are posting and "
                "messaging.", False)

    def render_for(self, name: str) -> str:
        parts = [f"The opening round is revealed: {len(self.proposals)} sealed proposal(s), now on the board "
                 f"in {self.ns} under each author's own identity (envelope ids below). Read them all.\n"]
        for n in self.names:
            if n in self.proposals:
                pid = self.posted.get(n)
                mine = " (yours)" if n == name else ""
                parts.append(f"=== {n}{mine}, #{pid if pid is not None else '?'} ===\n{self.proposals[n]}\n")
            else:
                parts.append(f"=== {n}: no proposal yet ===\n")
        if self.answers:
            parts.append(f"Answers posted so far ({len(self.answers)}):\n\n{self._render_answers(self.answers)}")
            self.shown[name] = max(self.shown.get(name, 0), len(self.answers))
        parts.append(
            "Next: answer the round, in turn. " + self._order_line(name) + "\n"
            "Your answer says which direction you take (yours, someone else's, a split of one, or a combination) "
            "and why that adds most to the swarm's chance of beating the best, given the answers before yours. "
            "Post it with the lab's `answer` tool (text, plus `replies`: the envelope ids of the proposals and "
            "answers it takes up or argues with). If the agents before you have not all answered yet, `answer` "
            "waits for them and shows you their answers before anything of yours is posted. "
            f"Each turn lasts up to {self.answer_turn_s:.0f} s, then passes on; you can still answer after yours "
            "passed. The lab, posting and messaging open for you as soon as your answer is posted.")
        return "\n".join(parts)

    # -- gates
    async def lab_blocked(self, name: str, cred: Credential | None = None) -> str | None:
        """Why the lab is closed for `name`, or None once it may use it."""
        if name in self.answered:
            return None
        if name not in self.proposals:
            return ("The lab opens after the opening round. First submit your sealed proposal with the lab's "
                    "`propose` tool (see *The opening round* in the swarm-environment canon): your read of the "
                    "problem, what you would try and why, and what you expect to be hardest. You can think, read "
                    "the task and write code in your workspace meanwhile.")
        if not self.revealed.is_set():
            return ("Your proposal is sealed; the lab opens after the reveal and your answer. Your `propose` call "
                    "returns every proposal at the reveal.")
        return ("One step left before the lab opens for you: answer the opening round with the lab's `answer` "
                "tool. " + self._order_line(name))

    def posting_blocked(self, name: str) -> str | None:
        """Nothing an agent writes may reach the others before the reveal,
        nor before its own answer is in (answers go in turn)."""
        if name in self.answered:
            return None
        if not self.revealed.is_set():
            return ("Posting and messaging open after the opening round, so that every proposal is made "
                    "independently. Put what you want to say into your sealed proposal (the lab's `propose` tool); "
                    "it is posted under your identity at the reveal. Nothing was posted.")
        return ("Posting and messaging open once your answer to the opening round is posted (the lab's `answer` "
                "tool; answers go in turn). " + self._order_line(name) + " Nothing was posted.")
