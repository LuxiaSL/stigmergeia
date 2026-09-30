"""The in-turn board pulse: board news delivered INSIDE a long turn.

An agent can work through a whole run in one continuous turn, and then the
between-turn digest never reaches it: a DM or an OPEN addressed to it goes
unanswered because it never sees it, and agents do not park `korax watch` on
their own. The pulse rides on tool results instead
(Claude: a PostToolUse hook's `additionalContext`; Codex: appended to the next
harness-hosted tool result), rate-limited, and only when there IS news:

- posts for this agent (its feed: DMs, mentions, replies to its posts), up to
  FOR_YOU_MAX lines;
- a new gate RECORD (score, agent, code path);
- one line counting the other new posts in the run's namespace, and how to
  read them.

Fixed maximum size at any swarm size. No clock, no durations, no budgets:
nothing in the text says when anything happened or how long is left.
Deduplicated by cursors: each item is shown once, and the continue message
advances the same cursors (so a post shown there is never pulsed again).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

from . import board, boardtext
from .board import Credential

log = logging.getLogger("swarm")

PULSE_MIN_INTERVAL_S = 150.0  # between board checks while the agent works (never shown)
FOR_YOU_MAX = 4
LINE_CHARS = 180
REQUEST_TIMEOUT_S = 5.0

# (path with query, token) -> JSON body; injectable for tests
Fetch = Callable[[str, str], dict[str, Any]]


@dataclass
class PulseNews:
    for_you: list[str] = field(default_factory=list)
    for_you_more: int = 0
    record: str | None = None
    others: int = 0
    others_from: int = -1
    shown_ids: set[Any] = field(default_factory=set)  # feed items shown here: never counted among "others"

    def empty(self) -> bool:
        return not (self.for_you or self.record or self.others)

    def addressed(self) -> bool:
        """News that wakes a waiting agent: something for it, or a new record."""
        return bool(self.for_you or self.record)


class Pulse:
    def __init__(self, cred: Credential, gate: Credential, ns: str, gate_ns: str,
                 names: Callable[[], dict[str, str]], higher_is_better: bool = True,
                 min_interval_s: float = PULSE_MIN_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic, fetch: Fetch | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.cred, self.gate, self.ns, self.gate_ns = cred, gate, ns, gate_ns
        self._names = names  # a getter: the runner fills the Agent's names after construction
        self.higher_is_better = higher_is_better
        self.min_interval_s, self.clock = min_interval_s, clock
        self._fetch = fetch or (lambda path, token: board._request(f"{cred.url}{path}", token,
                                                                    timeout=REQUEST_TIMEOUT_S))
        self.on_error = on_error or (lambda msg: log.warning("pulse: %s", msg))
        self.feed_cursor = -1
        self.ns_cursor = -1
        self.gate_cursor = -1
        self.primed = False
        self.last_check: float | None = None
        self.best: float | None = None  # the best gate record seen, so a pulse names only a NEW one
        self.lock = asyncio.Lock()

    # -- board reads (blocking; called in a thread)
    def _drain(self, path_base: str, since: int, token: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        """All envelopes after `since` (pages of up to 1000), and the new cursor."""
        out: list[dict[str, Any]] = []
        extra: dict[str, Any] = {}
        cur = since
        for _ in range(20):
            body = self._fetch(f"{path_base}&since={cur}", token)
            envs = body.get("envelopes") or []
            out += envs
            extra.update(body.get("reasons") or {})
            nxt = body.get("cursor", cur)
            if not envs or not isinstance(nxt, int) or nxt <= cur:
                break
            cur = nxt
            if "/feed" in path_base:
                break  # the feed is not paged: one call returns everything since the cursor
        return out, cur, extra

    def _feed_path(self) -> str:
        return "/feed?timeout=0"

    def _ns_path(self, ns: str, summary: bool) -> str:
        return f"/read?ns={quote(ns, safe='/')}&limit=1000" + ("&summary=true" if summary else "")

    def prime_sync(self, ns_cursor: int | None = None) -> None:
        """Start every cursor at the board's head (nothing old is 'news'),
        and learn the current best so only a later record is announced."""
        _, self.feed_cursor, _ = self._drain(self._feed_path(), -1, self.cred.token)
        if ns_cursor is not None:
            self.ns_cursor = ns_cursor
        else:
            _, self.ns_cursor, _ = self._drain(self._ns_path(self.ns, True), -1, self.cred.token)
        envs, self.gate_cursor, _ = self._drain(self._ns_path(self.gate_ns, False), -1, self.gate.token)
        self._records(envs)
        self.primed = True

    def _better(self, a: float, b: float | None) -> bool:
        return b is None or (a > b if self.higher_is_better else a < b)

    def _records(self, envs: list[dict[str, Any]]) -> str | None:
        """The best NEW record among these gate envelopes, as one line."""
        line = None
        for e in envs:
            p = e.get("payload")
            if e.get("author") != self.gate.identity or not (isinstance(p, dict) and p.get("kind") == "gate-result"):
                continue
            if not p.get("record"):
                continue
            val = p.get("score", p.get("mean"))
            if isinstance(val, (int, float)) and self._better(val, self.best):
                self.best = val
                code = f"; its code: {p['code']}" if p.get("code") else ""
                line = f"NEW RECORD {val} by {p.get('agent', '?')} (#{e.get('id')}){code}"
        return line

    def check_sync(self, skip_ids: frozenset[int] = frozenset()) -> PulseNews:
        news = self._addressed_sync(PulseNews(), skip_ids)
        self._others_sync(news)
        return news

    def _addressed_sync(self, news: PulseNews, skip_ids: frozenset[int] = frozenset()) -> PulseNews:
        """Drain the feed (DMs, mentions, replies) and the gate (a new record) into `news`; advances those
        two cursors. What `lab.wait` polls: the part of the pulse that wakes an agent."""
        feed, self.feed_cursor, reasons = self._drain(self._feed_path(), self.feed_cursor, self.cred.token)
        mine = [e for e in feed if e.get("author") != self.cred.identity and e.get("id") not in skip_ids]
        for e in mine:
            why = self._why(reasons.get(str(e.get("id"))) or [], e)
            news.for_you.append(f"{why}: {boardtext.one_line(e, self._names(), self.cred.identity, LINE_CHARS)}")
            news.shown_ids.add(e.get("id"))
        if len(news.for_you) > FOR_YOU_MAX:  # keep the newest; say how many more
            news.for_you_more += len(news.for_you) - FOR_YOU_MAX
            news.for_you = news.for_you[-FOR_YOU_MAX:]
        gate_envs, self.gate_cursor, _ = self._drain(self._ns_path(self.gate_ns, False), self.gate_cursor,
                                                     self.gate.token)
        news.record = self._records(gate_envs) or news.record
        return news

    def _others_sync(self, news: PulseNews) -> PulseNews:
        """Count the other new posts in the run's namespace (not the ones already in `news`); advances its cursor."""
        others, new_ns_cursor, _ = self._drain(self._ns_path(self.ns, True), self.ns_cursor, self.cred.token)
        rest = [e for e in others if e.get("id") not in news.shown_ids and e.get("author") not in
                (self.cred.identity, self.gate.identity)]
        news.others, news.others_from = len(rest), self.ns_cursor
        self.ns_cursor = new_ns_cursor
        return news

    def _why(self, reasons: list[Any], env: dict[str, Any]) -> str:
        lanes = {r.get("lane") for r in reasons if isinstance(r, dict)}
        if "mailbox" in lanes:
            return f"DM to you (reply: korax dm {env.get('author')} \"…\" --re {env.get('id')})"
        if "mention" in lanes or self.cred.identity in boardtext.mentions(env):
            return "mentions you"
        if "to_author" in lanes or "to_worked" in lanes:
            return "on your post"
        return "for you"

    def render(self, news: PulseNews) -> str:
        lines = ["[board pulse: new since you last looked]"]
        lines += [f"- {x}" for x in news.for_you]
        if news.for_you_more:
            lines.append(f"- (+{news.for_you_more} more for you: korax feed --for-me)")
        if news.record:
            lines.append(f"- gate: {news.record}")
        if news.others:
            lines.append(f"- {news.others} other new post{'s' if news.others != 1 else ''} in {self.ns}: "
                         f"korax feed --since {news.others_from}")
        return "\n".join(lines)

    # -- the async surface the agents call
    async def poll(self, force: bool = False, skip_ids: frozenset[int] = frozenset()) -> str | None:
        """The pulse text, or None when it is not time yet, or there is no news.
        `skip_ids`: posts already put in front of the agent some other way (the
        continue digest). Never raises: a board hiccup must not break a tool call."""
        now = self.clock()
        if not force and self.last_check is not None and now - self.last_check < self.min_interval_s:
            return None
        if self.lock.locked():
            return None  # a check is already in flight (parallel tool calls)
        async with self.lock:
            self.last_check = now
            try:
                if not self.primed:
                    await asyncio.to_thread(self.prime_sync)
                    return None
                news = await asyncio.to_thread(self.check_sync, skip_ids)
            except Exception as e:
                self.on_error(f"{type(e).__name__}: {e}")
                return None
        return None if news.empty() else self.render(news)

    async def wait_check(self, acc: PulseNews | None = None) -> PulseNews | None:
        """For `lab.wait`: drain what is ADDRESSED to the agent (feed, gate record) into `acc`, without the
        namespace count (that is taken once, by `wait_finish`). Never raises. None until the pulse is primed."""
        if self.lock.locked():
            return acc  # a pulse check is in flight (a parallel tool call): its cursors are moving
        async with self.lock:
            try:
                if not self.primed:
                    await asyncio.to_thread(self.prime_sync)
                    return acc
                return await asyncio.to_thread(self._addressed_sync, acc or PulseNews())
            except Exception as e:
                self.on_error(f"wait: {type(e).__name__}: {e}")
                return acc

    async def wait_finish(self, acc: PulseNews | None = None) -> str | None:
        """For `lab.wait`, when it returns: one last addressed check, the count of other new posts, rendered
        like a pulse (None when there is nothing). Counts as a pulse check, so the hook does not repeat it."""
        async with self.lock:
            self.last_check = self.clock()
            try:
                if not self.primed:
                    await asyncio.to_thread(self.prime_sync)
                    return None
                news = await asyncio.to_thread(self._addressed_sync, acc or PulseNews())
                news = await asyncio.to_thread(self._others_sync, news)
            except Exception as e:
                self.on_error(f"wait: {type(e).__name__}: {e}")
                news = acc or PulseNews()
        return None if news.empty() else self.render(news)

    def seen_ns_through(self, cursor: int) -> None:
        """The continue digest showed the namespace up to `cursor`: never count those again."""
        self.ns_cursor = max(self.ns_cursor, cursor)
