"""The opening round: proposals stay sealed until everyone has proposed (or the
deadline), are then posted together under each author's identity; then the
agents answer ONE AT A TIME (agent index order), each seeing every earlier
answer before its own is posted, and the lab, posting and DMs open for an
agent only once its answer is in. A turn passes after answer_turn_s."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from stigmergeia import board, opening_round as opening_round_mod
from stigmergeia.board import Credential
from stigmergeia.opening_round import OpeningRound
from stigmergeia.guard import make_guard

LONG = "x" * 250


class FakeBoard:
    def __init__(self):
        self.envs: list[dict] = []

    def post(self, cred, ns, act, payload, refs=None):
        e = {"id": len(self.envs) + 1, "author": cred.identity, "ns": ns, "type": act, "payload": payload,
             "refs": refs or []}
        self.envs.append(e)
        return {"id": e["id"]}

    def read_since(self, cred, ns, since, limit=1000):
        return [e for e in self.envs if e["ns"] == ns and e["id"] > since]


def creds(n):
    return {f"a0{i}": Credential(url="http://x", identity=f"band:{i:012d}", token="t", display=f"a0{i}")
            for i in range(n)}


class OpeningRoundTests(unittest.TestCase):
    def setUp(self):
        self.fb = FakeBoard()
        self.p1 = mock.patch.object(board, "post", self.fb.post)
        self.p2 = mock.patch.object(board, "read_since", self.fb.read_since)
        self.p1.start(); self.p2.start()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def make(self, n=3, deadline=60.0, turn=30.0):
        c = creds(n)
        return OpeningRound(ns="/swarm/t", names=list(c), creds=c, reveal_after_s=deadline, answer_turn_s=turn,
                       log_path=Path(self.tmp.name) / "opening_round.jsonl"), c

    def events(self):
        return [json.loads(l) for l in (Path(self.tmp.name) / "opening_round.jsonl").read_text().splitlines()]

    def test_sealed_until_all_then_revealed_together(self):
        async def go():
            co, c = self.make(3)
            t0 = asyncio.create_task(co.propose("a00", "plan zero " + LONG))
            t1 = asyncio.create_task(co.propose("a01", "plan one " + LONG))
            await asyncio.sleep(0.05)
            self.assertFalse(co.revealed.is_set())
            self.assertEqual(self.fb.envs, [])  # nothing on the board while sealed
            self.assertIsNotNone(co.posting_blocked("a00"))
            self.assertIn("sealed", await co.lab_blocked("a00", c["a00"]))
            self.assertIn("propose", await co.lab_blocked("a02", c["a02"]))
            out2 = await co.propose("a02", "plan two " + LONG)
            out0 = await t0
            await t1
            self.assertTrue(co.revealed.is_set())
            self.assertEqual([e["type"] for e in self.fb.envs], ["PROPOSAL"] * 3)
            self.assertEqual({e["author"] for e in self.fb.envs}, {cr.identity for cr in c.values()})
            for out in (out0, out2):
                self.assertIn("plan zero", out); self.assertIn("plan one", out); self.assertIn("plan two", out)
            self.assertIn("You are #1", out0)
            self.assertIn("You are #3", out2)
            # posting and the lab stay closed until the agent's own answer is in
            self.assertIn("answer", co.posting_blocked("a00"))
            self.assertIn("answer", await co.lab_blocked("a00", c["a00"]))
            msg, err = await co.answer("a00", "I take plan one " + LONG, [2])
            self.assertFalse(err)
            self.assertIn("posted as #4", msg)
            self.assertEqual(self.fb.envs[-1]["refs"], [{"edge": "replies", "id": 2}])
            self.assertEqual(self.fb.envs[-1]["payload"]["kind"], "opening-answer")
            self.assertIsNone(co.posting_blocked("a00"))
            self.assertIsNone(await co.lab_blocked("a00", c["a00"]))
            self.assertIsNotNone(co.posting_blocked("a01"))
            self.assertIsNotNone(await co.lab_blocked("a01", c["a01"]))
            co.close()
        asyncio.run(go())

    def test_answers_go_in_turn_and_each_sees_the_earlier_ones(self):
        async def go():
            co, c = self.make(3)
            ts = [asyncio.create_task(co.propose(n, f"plan {n} " + LONG)) for n in c]
            await asyncio.gather(*ts)
            # a02 asks first: it must wait for a00 and a01
            w2 = asyncio.create_task(co.answer("a02", "a02 draft " + LONG, [1]))
            await asyncio.sleep(0.05)
            self.assertFalse(w2.done())
            self.assertEqual(len(self.fb.envs), 3)  # nothing of a02's posted
            m0, _ = await co.answer("a00", "a00 answer " + LONG, [1])
            self.assertIn("posted", m0)
            await asyncio.sleep(0.05)
            self.assertFalse(w2.done())  # a01's turn now, not a02's
            # a01 has seen a00's answer? no: it must be shown first, and nothing is posted
            m1, _ = await co.answer("a01", "a01 draft " + LONG, [1])
            self.assertIn("NOT posted", m1)
            self.assertIn("a00 answer", m1)
            m1, _ = await co.answer("a01", "a01 answer " + LONG, [4])
            self.assertIn("posted", m1)
            m2, _ = await asyncio.wait_for(w2, 2)
            self.assertIn("NOT posted", m2)  # shown a00's and a01's, not posted
            self.assertIn("a00 answer", m2); self.assertIn("a01 answer", m2)
            m2, _ = await co.answer("a02", "a02 answer " + LONG, [4, 5])
            self.assertIn("posted", m2)
            answers = [e for e in self.fb.envs if e["payload"].get("kind") == "opening-answer"]
            self.assertEqual([e["payload"]["agent"] for e in answers], ["a00", "a01", "a02"])
            self.assertEqual([e["payload"]["position"] for e in answers], [1, 2, 3])
            ev = [e["event"] for e in self.events()]
            self.assertEqual(ev.count("turn"), 3)
            self.assertEqual(ev.count("answered"), 3)
            self.assertIn("shown", ev)
            co.close()
        asyncio.run(go())

    def test_a_turn_passes_after_its_time_and_the_skipped_agent_answers_later(self):
        async def go():
            co, c = self.make(2, turn=0.2)
            await asyncio.gather(*(co.propose(n, f"plan {n} " + LONG) for n in c))
            t0 = time.monotonic()
            m1, _ = await asyncio.wait_for(co.answer("a01", "a01 answer " + LONG, [1]), 2)
            self.assertGreaterEqual(time.monotonic() - t0, 0.15)  # waited for a00's turn to pass
            self.assertIn("posted", m1)  # nothing unseen: posted at once
            self.assertIn("a00", co.skipped)
            m0, _ = await co.answer("a00", "a00 late " + LONG, [3])
            self.assertIn("NOT posted", m0)  # it must see a01's answer first
            m0, _ = await co.answer("a00", "a00 late " + LONG, [3])
            self.assertIn("posted", m0)
            self.assertIn("after its turn passed", self.fb.envs[-1]["payload"]["text"])
            ev = {e["event"]: e for e in self.events()}
            self.assertEqual(ev["skipped"]["agent"], "a00")
            co.close()
        asyncio.run(go())

    def test_thin_or_early_answers_post_nothing(self):
        async def go():
            co, c = self.make(2)
            msg, err = await co.answer("a00", LONG, [])
            self.assertTrue(err)  # before the reveal
            await asyncio.gather(*(co.propose(n, f"plan {n} " + LONG) for n in c))
            msg, err = await co.answer("a00", "too short", [])
            self.assertTrue(err)
            self.assertEqual(len(self.fb.envs), 2)
            co.close()
        asyncio.run(go())

    def test_deadline_reveals_partial_and_late_proposal_posts_at_once(self):
        async def go():
            co, c = self.make(3, deadline=0.1)
            t0 = asyncio.create_task(co.propose("a00", "early " + LONG))
            await co.deadline()
            out0 = await t0
            self.assertIn("a01: no proposal yet", out0)
            self.assertEqual(len(self.fb.envs), 1)
            out1 = await co.propose("a01", "late " + LONG)  # returns immediately, posted at once
            self.assertEqual(len(self.fb.envs), 2)
            self.assertIn("late", out1)
            self.assertIn("submitted after the reveal", self.fb.envs[1]["payload"]["text"])
            # its own opening proposal does not count as its answer; it joins the end of the order
            self.assertIsNotNone(await co.lab_blocked("a01", c["a01"]))
            self.assertEqual(co.order, ["a00", "a01"])
            co.close()
        asyncio.run(go())

    def test_thin_and_repeat_proposals(self):
        async def go():
            co, _ = self.make(2)
            self.assertIn("Nothing was sealed", await co.propose("a00", "short"))
            self.assertNotIn("a00", co.proposals)
            t = asyncio.create_task(co.propose("a00", LONG))
            await asyncio.sleep(0.02)
            self.assertIn("already proposed", await co.propose("a00", LONG + "again"))
            await co.propose("a01", LONG)
            await t
        asyncio.run(go())


class SealedPostingGuardTests(unittest.TestCase):
    def test_post_and_dm_blocked_only_while_sealed(self):
        with tempfile.TemporaryDirectory() as d:
            sealed = {"on": True}
            g = make_guard(Path(d), ("mcp__korax__", "mcp__lab__"), [], shell=True,
                           posting_blocked=lambda: "sealed" if sealed["on"] else None)

            def decide(tool, **args):
                out = asyncio.run(g({"tool_name": tool, "tool_input": args}, None, None))
                return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

            self.assertEqual(decide("mcp__korax__korax_post", type="NOTE", payload="x"), "deny")
            self.assertEqual(decide("mcp__korax__korax_dm", to="band:1", text="x"), "deny")
            self.assertEqual(decide("Bash", command='korax post --ns /swarm/t --type NOTE --payload "x"'), "deny")
            self.assertEqual(decide("Bash", command='cd task && korax dm band:1 "hi"'), "deny")
            self.assertEqual(decide("Bash", command="korax read --ns /swarm/t"), "allow")
            self.assertEqual(decide("Bash", command="korax ack 16 20"), "allow")
            self.assertEqual(decide("mcp__korax__korax_ack", ids=[1]), "allow")
            sealed["on"] = False
            self.assertEqual(decide("mcp__korax__korax_post", type="NOTE", payload="x"), "allow")
            self.assertEqual(decide("Bash", command='korax dm band:1 "hi"'), "allow")


class NoHandoverGuardTests(unittest.TestCase):
    def test_handover_refused_both_routes(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_guard(Path(d), ("mcp__korax__", "mcp__lab__"), [], shell=True)

            def decide(tool, **args):
                out = asyncio.run(g({"tool_name": tool, "tool_input": args}, None, None))
                return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")

            self.assertEqual(decide("mcp__korax__korax_post", type="HANDOVER", payload="bye"), "deny")
            self.assertEqual(decide("Bash", command='korax post --ns /swarm/t --type HANDOVER --payload "bye"'), "deny")
            self.assertEqual(decide("Bash", command="korax post --ns /swarm/t --type=HANDOVER --payload-file h.md"), "deny")
            self.assertEqual(decide("mcp__korax__korax_post", type="FINDING", payload="x"), "allow")
            self.assertEqual(decide("Bash", command='korax post --ns /swarm/t --type NOTE --payload "handover jokes"'), "allow")


if __name__ == "__main__":
    unittest.main()
