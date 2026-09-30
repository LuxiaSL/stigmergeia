"""The afterparty: survey storage and privacy gate, the round barrier, and one
guest's whole flow (wake -> survey -> board opens -> rounds) against a fake
SDK client and a fake board. No API calls."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_agent_sdk
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from stigmergeia import afterparty as ap
from stigmergeia import board
from stigmergeia.board import Credential
from stigmergeia.config import RunConfig
from stigmergeia.lab import AgentSlot


def make_cfg(tmp: Path, **kw) -> RunConfig:
    base = dict(run_name="t-run", runs_dir=tmp, task_dir=tmp, n_agents=2, per_agent_budget_usd=1,
                total_budget_usd=2, board={"url": "http://127.0.0.1:1", "operator_token_file": tmp / "op"},
                gate={}, shell=False)
    base.update(kw)
    return RunConfig.model_validate(base)


ANSWERS = {k: "fine" for k in ap.QUESTIONS}


def iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SurveyTests(unittest.TestCase):
    def test_fields_follow_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            qs, ratings = ap.survey_fields(make_cfg(Path(d)))
            self.assertNotIn("opening_round", qs)
            self.assertIn("watch", qs)
            qs, _ = ap.survey_fields(make_cfg(Path(d), opening_round={"reveal_after_s": 60}))
            self.assertIn("opening_round", qs)
            qs, ratings = ap.survey_fields(make_cfg(Path(d), solo=True, n_agents=1, total_budget_usd=1))
            self.assertNotIn("watch", qs)
            self.assertNotIn("korax_board", ratings)
            self.assertIn("brief", qs["canon"])

    def test_validate_and_store(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(Path(d))
            box = ap.SurveyBox(Path(d) / "survey.jsonl", cfg)
            _, missing = box.validate({"surprised": "x"})
            self.assertIn("one_change", missing)
            args = {**ANSWERS, "ratings": {"lab_tools": 4, "watch": 9, "bogus": 3, "canon": "2"}}
            answers, missing = box.validate(args)
            self.assertEqual(missing, [])
            self.assertEqual(answers["ratings"], {"lab_tools": 4, "canon": 2})  # out of range and unknown dropped
            asyncio.run(box.submit("a00", "band:x", args, False))
            row = json.loads((Path(d) / "survey.jsonl").read_text())
            self.assertEqual(row["agent"], "a00")
            self.assertFalse(row["amends_earlier"])

    def test_tool_refuses_incomplete_and_flags_done(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(Path(d))
            box = ap.SurveyBox(Path(d) / "survey.jsonl", cfg)
            state = {"done": False}
            spec = ap.survey_spec(box, "a01", "band:y", lambda: state.update(done=True), lambda: state["done"])
            r = asyncio.run(spec.handler({"surprised": "x"}))
            self.assertTrue(r["is_error"])
            self.assertFalse((Path(d) / "survey.jsonl").exists())
            r = asyncio.run(spec.handler(dict(ANSWERS)))
            self.assertFalse(r["is_error"])
            self.assertTrue(state["done"])
            asyncio.run(spec.handler(dict(ANSWERS)))  # a second call is kept, marked as an amendment
            rows = [json.loads(line) for line in (Path(d) / "survey.jsonl").read_text().splitlines()]
            self.assertEqual([r["amends_earlier"] for r in rows], [False, True])


class RoundsTests(unittest.TestCase):
    def test_barrier_waits_for_all_then_releases(self):
        async def go():
            r = ap.Rounds(["a", "b"])
            await r.finish("a", 0)
            t = asyncio.create_task(r.wait(0, 5))
            await asyncio.sleep(0.05)
            self.assertFalse(t.done())
            await r.leave("b")  # a guest that stops early leaves, and holds nobody up
            await asyncio.wait_for(t, 1)

        asyncio.run(go())

    def test_barrier_times_out(self):
        async def go():
            r = ap.Rounds(["a", "b"])
            await asyncio.wait_for(r.wait(0, 0.05), 1)

        asyncio.run(go())


class FakeClient:
    """Answers each query with one assistant message and a result. A query
    that mentions the survey tool fills in the survey (as the model would by
    calling the tool); a query to post posts to the fake board."""
    instances: list["FakeClient"] = []

    def __init__(self, options=None):
        self.queries: list[str] = []
        self.guest = FakeClient.guest
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, message: str):
        self.queries.append(message)
        if "mcp__afterparty__survey" in message and FakeClient.will_survey:
            spec = ap.survey_spec(self.guest.party.box, self.guest.name, self.guest.cred.identity,
                                  on_done=lambda: setattr(self.guest, "surveyed", True),
                                  done=lambda: self.guest.surveyed)
            await spec.handler(dict(ANSWERS))

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="hi all")], model="m", message_id=f"m{len(self.queries)}",
                               usage={"input_tokens": 1000, "output_tokens": 100})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="forked", total_cost_usd=5.0)  # a resumed session reports the whole run's spend

    async def interrupt(self):
        pass


class FakeAgent:
    def __init__(self, cfg, ws: Path, log: Path):
        self.cfg, self.sandbox_failed = cfg, None
        self.slot = AgentSlot(name="a00", index=0, workspace=ws, private=ws, remote_ws="", cores="")
        self.log = log

    def _record(self, obj):
        with self.log.open("a") as f:
            f.write(json.dumps(obj, default=repr) + "\n")


class GuestFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "ws").mkdir()
        (self.d / "ws" / "policy.py").write_text("x")
        self.cfg = make_cfg(self.d)
        (self.cfg.run_dir / "afterparty").mkdir(parents=True)
        self.creds = [Credential(url="http://b", identity=f"band:{i:012d}", token="t", display=f"a0{i}")
                      for i in range(2)]
        gate = Credential(url="http://b", identity="band:gate", token="t", display="gate")
        facts = ap.RunFacts("t-run", 0.0, 3600.0, [ap.Record(5.0, "a01", 70.0)], {"a00": 60.0}, {})
        self.party = ap.Party(self.cfg, ap.PartySettings(rounds=2, round_timeout_s=0.2), gate, self.creds, facts,
                              "NOTE")
        self.posts = [{"id": 10, "author": self.creds[1].identity, "ns": self.party.party_ns, "payload": "yo a00!"}]

    def tearDown(self):
        self.tmp.cleanup()

    def run_guest(self, will_survey=True):
        agent = FakeAgent(self.cfg, self.d / "ws", self.d / "log.jsonl")
        guest = ap.Guest(self.party, "a00", agent, self.creds[0], "sess-1")
        FakeClient.guest, FakeClient.will_survey, FakeClient.instances = guest, will_survey, []
        read = lambda cred, ns, since, limit=1000: [e for e in self.posts if e["ns"] == ns and e["id"] > since]
        req = lambda url, token, *a, **k: {"envelope": next(e for e in self.posts if url.endswith(f"/{e['id']}"))}
        with mock.patch.object(claude_agent_sdk, "ClaudeSDKClient", FakeClient), \
             mock.patch.object(ap.Guest, "options", lambda self: None), \
             mock.patch.object(board, "read_since", read), mock.patch.object(board, "_request", req):
            self.party.rounds = ap.Rounds(["a00"])
            return asyncio.run(guest.live()), FakeClient.instances[0].queries

    def test_whole_flow(self):
        res, queries = self.run_guest()
        self.assertEqual(res.stop, "done")
        self.assertTrue(res.surveyed)
        self.assertEqual(res.rounds, 2)
        self.assertEqual(len(queries), 4)  # wake, open, round 1, round 2
        self.assertIn("70.00 by a01", queries[0])
        self.assertIn("/swarm/t-run/afterparty", queries[1])
        self.assertIn("yo a00!", queries[1])  # the digest carries others' posts
        self.assertIn("last round", queries[-1])
        self.assertEqual(res.ws_changed, [])
        self.assertEqual(res.new_session, "forked")
        rows = (self.cfg.run_dir / "afterparty" / "survey.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)

    def test_no_survey_nudges_then_opens_anyway(self):
        res, queries = self.run_guest(will_survey=False)
        self.assertFalse(res.surveyed)
        self.assertEqual(sum("Whenever you're ready" in q for q in queries), 2)
        self.assertEqual(res.stop, "done")

    def test_budget_cap_stops_the_guest(self):
        self.party.settings = ap.PartySettings(budget_usd=0.001)
        res, queries = self.run_guest()
        self.assertIn("budget", res.stop)
        self.assertEqual(len(queries), 1)

    def test_posting_blocked_until_surveyed(self):
        agent = FakeAgent(self.cfg, self.d / "ws", self.d / "log.jsonl")
        guest = ap.Guest(self.party, "a00", agent, self.creds[0], "s")
        self.assertIsNotNone(guest.posting_blocked())
        guest.surveyed = True
        self.assertIsNone(guest.posting_blocked())


class FactsTests(unittest.TestCase):
    def test_records_and_bests_from_gate_posts_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = make_cfg(d)
            t = d / "a00.jsonl"
            t.write_text(json.dumps({"t": 1790000000.0}) + "\n" + json.dumps(
                {"t": 1790003600.0, "_type": "agent_end", "stop_reason": "wall clock"}) + "\n")
            gate = Credential(url="http://b", identity="band:gate", token="t", display="gate")
            envs = [
                {"id": 1, "author": "band:gate", "ts": iso(1790000000.0 + 3000),
                 "payload": {"kind": "gate-result", "agent": "a00", "score": 70.0, "record": True, "confirmed": True}},
                {"id": 2, "author": "band:gate", "ts": iso(1790000000.0 + 3300),
                 "payload": {"kind": "gate-result", "agent": "a01", "score": 90.0, "record": False, "confirmed": False}},
                {"id": 3, "author": "band:someone", "ts": iso(1790000000.0 + 3300),
                 "payload": {"kind": "gate-result", "agent": "a01", "score": 99.0, "record": True}},
            ]
            read = lambda cred, ns, since, limit=1000: envs
            req = lambda url, token, *a, **k: {"envelope": next(e for e in envs if url.endswith(f"/{e['id']}"))}
            with mock.patch.object(board, "read_since", read), mock.patch.object(board, "_request", req):
                f = ap.run_facts(cfg, gate, {"a00": t})
            self.assertEqual([(r.agent, r.score) for r in f.records], [("a00", 70.0)])
            self.assertEqual(f.own_best, {"a00": 70.0})  # unconfirmed and non-gate results never count
            self.assertEqual(f.duration_min, 60)
            self.assertEqual(f.records[0].minute, 50.0)


class PromptFactsTests(unittest.TestCase):
    def test_generic_prompts_describe_a_run_by_name_alone(self):
        prompts = ap.load_prompts()
        self.assertEqual(prompts["runs"], {})
        self.assertEqual(prompts["series"], "")

    def test_facts_merge_over_the_generic_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d) / "facts.yaml"
            facts.write_text("runs:\n  r1: 'r1: two agents.'\nseries: 'r0 then r1.'\nwake_r1: 'Hi {name}.'\n")
            prompts = ap.load_prompts(facts=facts)
            generic = ap.load_prompts()
            self.assertEqual(prompts["runs"], {"r1": "r1: two agents."})
            self.assertEqual(prompts["series"], "r0 then r1.")
            self.assertEqual(prompts["wake_r1"], "Hi {name}.")
            self.assertEqual(prompts["wake"], generic["wake"])  # keys the facts leave out stay generic
            merged = ap.merge_facts({**generic, "runs": {"r0": "old", "r1": "old"}}, {"runs": {"r1": "new"}})
            self.assertEqual(merged["runs"], {"r0": "old", "r1": "new"})  # `runs` merges entry by entry

    def test_facts_reach_the_wake_message(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            facts = d / "facts.yaml"
            facts.write_text("runs:\n  t-run: 'a test run.'\nseries: 'THE SERIES'\n")
            cfg = make_cfg(d)
            gate = Credential(url="http://b", identity="band:gate", token="t", display="gate")
            rf = ap.RunFacts("t-run", 0.0, 3600.0, [], {}, {})
            with_facts = ap.Party(cfg, ap.PartySettings(facts_file=facts), gate, [], rf, "NOTE")
            self.assertIn("This was a test run.", with_facts.wake_message("a00"))
            self.assertIn("THE SERIES", with_facts.wake_message("a00"))
            plain = ap.Party(cfg, ap.PartySettings(), gate, [], rf, "NOTE")
            self.assertIn("This was t-run.", plain.wake_message("a00"))

    def test_bad_facts_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d) / "facts.yaml"
            for text in ("serise: 'typo'\n", "runs: [a, b]\n", "series: {a: 1}\n", "- a list\n"):
                facts.write_text(text)
                with self.assertRaises(ValueError, msg=text):
                    ap.load_prompts(facts=facts)
            with self.assertRaises(ValueError):
                ap.load_prompts(facts=Path(d) / "missing.yaml")


if __name__ == "__main__":
    unittest.main()
