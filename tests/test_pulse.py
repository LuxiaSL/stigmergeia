"""The in-turn board pulse: rate-limited, only when there is news, deduplicated
by cursor, compact at any swarm size, never a word about time; and its two
deliveries: the Claude PostToolUse hook and the Codex tool result."""
import asyncio
import re
import unittest
from typing import Any
from urllib.parse import parse_qs, urlsplit

from stigmergeia.board import Credential
from stigmergeia.pulse import FOR_YOU_MAX, Pulse

ME, OTHER, THIRD, GATE = "band:aaaaaaaaaaaa", "band:bbbbbbbbbbbb", "band:cccccccccccc", "band:gggggggggggg"
NS, GATE_NS = "/swarm/t", "/swarm/t/gate"
NAMES = {ME: "a00", OTHER: "a01", THIRD: "a02", GATE: "GATE"}
TIME_WORDS = re.compile(r"\b(min(ute)?s?|seconds?|hours?|elapsed|remaining|deadline|budget|\$|clock|time)\b", re.I)


class FakeBoard:
    """/read (ns subtree, since, limit, summary), /feed (mailbox,
    mentions, replies to the requester's posts), as the server answers them."""

    def __init__(self):
        self.envs: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.down = False

    def add(self, author, ns, type_="NOTE", payload="", refs=(), mentions=()):
        env = {"id": len(self.envs) + 1, "author": author, "ns": ns, "type": type_, "payload": payload,
               "refs": [{"edge": e, "id": i} for e, i in refs],
               "ext": {"korax": {"mentions": list(mentions)}} if mentions else {}}
        self.envs.append(env)
        return env["id"]

    def fetch(self, path: str, token: str) -> dict[str, Any]:
        self.calls.append(path)
        if self.down:
            raise OSError("board down")
        who = {"t-me": ME, "t-gate": GATE}[token]
        u = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        since = int(q.get("since", -1))
        if u.path == "/feed":
            mine = {e["id"] for e in self.envs if e["author"] == who}
            out, reasons = [], {}
            for e in self.envs:
                if e["id"] <= since:
                    continue
                lanes = []
                if e["ns"] == f"/dm/{who}":
                    lanes.append({"lane": "mailbox"})
                if e["author"] != who:
                    if any(r["id"] in mine for r in e["refs"]):
                        lanes.append({"lane": "to_author"})
                    if who in (e.get("ext", {}).get("korax", {}).get("mentions") or []):
                        lanes.append({"lane": "mention"})
                if lanes:
                    out.append(e)
                    reasons[str(e["id"])] = lanes
            return {"envelopes": out, "reasons": reasons, "cursor": out[-1]["id"] if out else since}
        ns, limit = q["ns"], int(q.get("limit", 500))
        # like the server: a plain /read includes the requester's own posts (self-drop is only for to_author)
        hits = [e for e in self.envs if e["id"] > since
                and (e["ns"] == ns or e["ns"].startswith(ns + "/"))][:limit]
        if q.get("summary") == "true":
            hits = [{k: v for k, v in e.items() if k != "payload"} | {"payload_bytes": len(str(e["payload"]))}
                    for e in hits]
        return {"envelopes": hits, "cursor": hits[-1]["id"] if hits else since}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gate_result(score, agent="a01", record=True, code="../a01/p.py"):
    return {"kind": "gate-result", "agent": agent, "policy": "p.py", "score": score, "record": record,
            "confirmed": True, "code": code}


def make(board: FakeBoard, clock: Clock, interval=150.0):
    cred = Credential(url="http://x", identity=ME, token="t-me", display="t-a00")
    gate = Credential(url="http://x", identity=GATE, token="t-gate", display="t-gate")
    errors: list[str] = []
    p = Pulse(cred, gate, NS, GATE_NS, lambda: NAMES, min_interval_s=interval, clock=clock,
              fetch=board.fetch, on_error=errors.append)
    return p, errors


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.board, self.clock = FakeBoard(), Clock()
        self.board.add(OTHER, NS, payload="old news before the agent started")
        self.board.add(GATE, GATE_NS, "RESULT", gate_result(70.0))
        self.pulse, self.errors = make(self.board, self.clock)
        self.pulse.prime_sync()

    def poll(self, **kw):
        return asyncio.run(self.pulse.poll(**kw))

    def test_nothing_new_says_nothing_and_old_posts_are_not_news(self):
        self.clock.t += 1000
        self.assertIsNone(self.poll())
        self.assertEqual(self.pulse.best, 70.0)

    def test_dm_mention_reply_record_and_count(self):
        mine = self.board.add(ME, NS, "FINDING", "my finding")
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "psst, try the C sim")
        self.board.add(THIRD, NS, "OPEN", {"text": "a00: what depth did you use?"}, mentions=[ME])
        self.board.add(OTHER, NS, "FINDING", "reproduced it", refs=[("corroborates", mine)])
        self.board.add(THIRD, NS, "NOTE", "unrelated chatter")
        self.board.add(OTHER, NS, "PROPOSAL", "trying beam search")
        self.board.add(GATE, GATE_NS, "RESULT", gate_result(80.5, agent="a02", code="../a02/q.py"))
        self.board.add(GATE, GATE_NS, "RESULT", gate_result(79.0, record=False))
        self.clock.t += 200
        text = self.poll()
        self.assertIsNotNone(text)
        self.assertIn("DM to you", text)
        self.assertIn(f"korax dm {OTHER}", text)
        self.assertIn("psst, try the C sim", text)
        self.assertIn("mentions you", text)
        self.assertIn("a02 OPEN", text)
        self.assertIn("on your post", text)
        self.assertIn("corroborates #", text)
        self.assertIn("NEW RECORD 80.5 by a02", text)
        self.assertIn("../a02/q.py", text)
        self.assertNotIn("79.0", text)  # not a record
        # 2 others in the namespace: the gate's posts and the ones already shown don't count
        self.assertIn("2 other new posts in /swarm/t: korax feed --since", text)
        self.assertNotIn("unrelated chatter", text)  # counted, not shown
        self.assertIsNone(TIME_WORDS.search(text), text)
        # shown once: the same news never comes back
        self.clock.t += 200
        self.assertIsNone(self.poll())

    def test_rate_limited_but_news_waits_for_the_next_window(self):
        self.clock.t += 200
        self.assertIsNone(self.poll())  # a check, no news
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "hello")
        self.clock.t += 10
        self.assertIsNone(self.poll())  # too soon after the last check: no board call at all
        n = len(self.board.calls)
        self.clock.t += 10
        self.poll()
        self.assertEqual(len(self.board.calls), n)
        self.clock.t += 150
        self.assertIn("hello", self.poll())

    def test_force_skips_the_rate_limit_and_skip_ids_dedupes_the_digest(self):
        a = self.board.add(THIRD, NS, "OPEN", "a00?", mentions=[ME])
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "a dm")
        self.pulse.seen_ns_through(a)  # the continue digest showed the namespace through #a
        text = self.poll(force=True, skip_ids=frozenset({a}))
        self.assertIn("a dm", text)
        self.assertNotIn("a00?", text)
        self.assertNotIn("other new post", text)

    def test_fixed_size_at_any_swarm_size(self):
        for i in range(200):
            self.board.add(OTHER, NS, "NOTE", f"hey a00 number {i} " + "x" * 2000, mentions=[ME])
        for i in range(300):
            self.board.add(THIRD, NS, "NOTE", "chatter " * 100)
        self.clock.t += 200
        text = self.poll()
        self.assertEqual(sum(1 for line in text.splitlines() if "mentions you" in line), FOR_YOU_MAX)
        self.assertIn(f"+{200 - FOR_YOU_MAX} more for you: korax feed --for-me", text)
        self.assertIn("300 other new posts", text)
        self.assertLess(len(text), 1400)

    def test_board_errors_are_swallowed_and_recorded(self):
        self.board.down = True
        self.clock.t += 200
        self.assertIsNone(self.poll())
        self.assertTrue(self.errors and "board down" in self.errors[0])
        self.board.down = False
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "back")
        self.clock.t += 200
        self.assertIn("back", self.poll())

    def test_unprimed_pulse_primes_first_and_reports_nothing_old(self):
        p, _ = make(self.board, self.clock)
        self.board.add(OTHER, f"/dm/{ME}", "NOTE", "before prime")
        self.assertIsNone(asyncio.run(p.poll()))  # primes at the head
        self.clock.t += 200
        self.assertIsNone(asyncio.run(p.poll()))


class ClaudeHookTests(unittest.TestCase):
    """The hook's wire shape, and that options() registers it with the env fixes."""

    def _agent(self, tmp):
        import time
        from pathlib import Path
        from stigmergeia.agent import Agent, Ledger
        from stigmergeia.config import RunConfig
        from stigmergeia.lab import AgentSlot
        base = Path(tmp)
        (base / "op.token").write_text("x")
        ws, private = base / "ws" / "a00", base / "private" / "a00"
        ws.mkdir(parents=True)
        private.mkdir(parents=True)
        cfg = RunConfig.model_validate({
            "run_name": "pulse-test", "runs_dir": str(base / "runs"), "task_dir": str(base), "n_agents": 2,
            "per_agent_budget_usd": 1, "total_budget_usd": 2, "shell": False, "local_cpu_quota": None,
            "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(base / "op.token")}, "gate": {}})
        slot = AgentSlot(name="a00", index=0, workspace=ws, private=private, remote_ws="", cores="")
        cred = Credential(url="http://127.0.0.1:1", identity=ME, token="t-me", display="pulse-test-a00")
        gate = Credential(url="http://127.0.0.1:1", identity=GATE, token="t-gate", display="pulse-test-gate")
        return Agent(cfg, slot, cred, gate, None, "k", ["korax-mcp"], Ledger(100), time.time() + 60, "go")

    def test_hook_returns_additional_context_and_logs_it(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            board, clock = FakeBoard(), Clock()
            agent.pulse._fetch, agent.pulse.clock = board.fetch, clock
            agent.names = NAMES
            agent.pulse.prime_sync()
            board.add(OTHER, f"/dm/{ME}", "NOTE", "ping from a01")
            clock.t += 500
            out = asyncio.run(agent._pulse_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, "tu1", None))
            hso = out["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "PostToolUse")
            self.assertIn("ping from a01", hso["additionalContext"])
            self.assertIn("a01", hso["additionalContext"])  # names come from the runner's roster, set later
            self.assertEqual(asyncio.run(agent._pulse_hook({"hook_event_name": "PostToolUse"}, "tu2", None)), {})
            rows = [json.loads(x) for x in agent.transcript.read_text().splitlines()]
            self.assertEqual([r["tool_use_id"] for r in rows if r["_type"] == "harness_pulse"], ["tu1"])

    def test_options_register_hooks_and_quiet_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            opts = agent.options(None)
            self.assertIn("PostToolUse", opts.hooks)
            self.assertIn("PostToolUseFailure", opts.hooks)
            self.assertEqual(opts.env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"], "1")
            self.assertEqual(opts.env["CLAUDE_CODE_SILENT_TURN_REMINDER"], "0")
            self.assertEqual(opts.env["CLAUDE_CODE_TOTAL_TOKENS_REMINDER"], "off")

    def test_pulse_off(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            from stigmergeia.agent import Agent
            a2 = Agent(agent.cfg.model_copy(update={"pulse_s": 0}), agent.slot, agent.cred, agent.gate, None, "k", ["korax-mcp"], agent.ledger, agent.deadline, "go")
            self.assertIsNone(a2.pulse)
            self.assertNotIn("PostToolUse", a2.options(None).hooks)


class CodexPulseTests(unittest.TestCase):
    def test_tool_result_carries_the_pulse_as_its_own_item(self):
        import tempfile
        from pathlib import Path
        from test_codex import make_agent
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = make_agent(Path(tmp), "tools")
            board, clock = FakeBoard(), Clock()
            agent.pulse._fetch, agent.pulse.clock = board.fetch, clock
            agent.pulse.cred = agent.pulse.cred.model_copy(update={"identity": ME, "token": "t-me"})
            agent.pulse.gate = agent.pulse.gate.model_copy(update={"identity": GATE, "token": "t-gate"})
            agent.names = NAMES
            agent.pulse.prime_sync()
            board.add(OTHER, f"/dm/{ME}", "NOTE", "codex ping")
            clock.t += 500
            params = {"tool": "Write", "callId": "c9", "arguments": {"file_path": "x.txt", "content": "hi"}}
            res = asyncio.run(agent._tool_call(params))
            self.assertTrue(res["success"])
            self.assertEqual(len(res["contentItems"]), 2)
            self.assertNotIn("codex ping", res["contentItems"][0]["text"])  # the tool's own output is untouched
            self.assertIn("codex ping", res["contentItems"][1]["text"])
            res2 = asyncio.run(agent._tool_call({**params, "callId": "c10"}))
            self.assertEqual(len(res2["contentItems"]), 1)  # nothing new, nothing added


if __name__ == "__main__":
    unittest.main()
