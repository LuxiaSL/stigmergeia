"""Responsive waiting: the lab's `wait` returns on the FIRST of a job of the
agent's finishing, board news addressed to it, or its cap; it shares the
pulse's cursors (nothing shown twice) and says nothing about time beyond a
single job's own duration."""
import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from test_lab import LabSubmitTests, config
from test_pulse import GATE, GATE_NS, ME, NS, OTHER, THIRD, Clock, FakeBoard, gate_result, make

from stigmergeia import lab as lab_mod
from stigmergeia.board import Credential
from stigmergeia.lab import AgentSlot, Lab

# the pulse's own time-word check, minus the single-job facts `wait` may state ("running for 3 s")
BANNED = ("deadline", "remaining", "elapsed", "budget", "$", "clock", "hour", "minutes left", "time left",
          "ends soon", "run ends", "last chance")


class WaitTests(unittest.TestCase):
    def setUp(self):
        LabSubmitTests.setUp(self)
        self.board, self.clock = FakeBoard(), Clock()
        self.board.add(OTHER, NS, payload="old news before the agent started")
        self.board.add(GATE, GATE_NS, "RESULT", gate_result(70.0))
        self.pulse, self.errors = make(self.board, self.clock)
        self.pulse.prime_sync()

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, delay: float = 0.3) -> tuple[Lab, AgentSlot]:
        lab = Lab(config(Path(self.tmp.name)), Credential(url="http://x", identity="band:gate", token="t",
                                                            display="gate"))
        lab._push = mock.AsyncMock(return_value=(0, ""))
        lab._pull = mock.AsyncMock(return_value=(0, ""))

        async def exe(argv, timeout):
            await asyncio.sleep(delay)
            return 0, "cem done: best 71.2\n"
        self.exe = exe
        slot = AgentSlot(name="a00", index=0, workspace=self.ws, private=self.ws, remote_ws="/r/agents/a00",
                         cores="0-3")
        return lab, slot

    def assert_no_time_framing(self, text: str) -> None:
        low = text.lower()
        for w in BANNED:
            self.assertNotIn(w, low, text)

    def test_returns_the_moment_a_job_finishes_with_its_result(self):
        async def go():
            lab, slot = self.lab(delay=0.3)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                t0 = time.monotonic()
                res = await lab.wait(slot, self.pulse, cap_s=10, poll_s=0.05)
                took = time.monotonic() - t0
            text = res["content"][0]["text"]
            self.assertLess(took, 1.0)  # not the cap: the job's end woke it
            self.assertIn("background run r1 (`python cem.py`) finished", text)
            self.assertIn("cem done: best 71.2", text)
            self.assertNotIn("still running", text)
            self.assert_no_time_framing(text)
            self.assertEqual(lab.take_notes(slot, "test"), "")  # delivered once
            rows = [json.loads(line) for line in (self.ws / "lab-jobs.jsonl").read_text().splitlines()]
            self.assertIn(("waited", "job"), [(r["event"], r.get("why")) for r in rows])
        asyncio.run(go())

    def test_board_news_for_the_agent_wakes_it_and_is_not_pulsed_again(self):
        async def go():
            lab, slot = self.lab(delay=5)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)

                async def later():
                    await asyncio.sleep(0.2)
                    self.board.add(THIRD, NS, "NOTE", "chatter nobody addressed to a00")
                    self.board.add(OTHER, f"/dm/{ME}", "NOTE", "psst: try the C sim")
                asyncio.create_task(later())
                t0 = time.monotonic()
                res = await lab.wait(slot, self.pulse, cap_s=10, poll_s=0.05)
                took = time.monotonic() - t0
                slot.jobs["r1"].task.cancel()
            text = res["content"][0]["text"]
            self.assertLess(took, 2.0)
            self.assertIn("Something on the board is for you", text)
            self.assertIn("psst: try the C sim", text)
            self.assertIn("DM to you", text)
            self.assertIn("r1 run `python cem.py`: running for", text)  # the job's own fact
            self.assertIn("1 other new post in /swarm/t", text)
            self.assertNotIn("chatter nobody", text)  # counted, not shown
            self.assert_no_time_framing(text)
            # the pulse hook after this call has nothing to repeat
            self.clock.t += 1000
            self.assertIsNone(await self.pulse.poll())
        asyncio.run(go())

    def test_a_new_record_wakes_it(self):
        async def go():
            lab, slot = self.lab(delay=5)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                self.board.add(GATE, GATE_NS, "RESULT", gate_result(81.5, agent="a02", code="../a02/q.py"))
                res = await lab.wait(slot, self.pulse, cap_s=10, poll_s=0.05)
                slot.jobs["r1"].task.cancel()
            text = res["content"][0]["text"]
            self.assertIn("NEW RECORD 81.5 by a02", text)
            self.assertIn("../a02/q.py", text)
        asyncio.run(go())

    def test_cap_returns_the_status_and_only_what_is_new(self):
        async def go():
            lab, slot = self.lab(delay=5)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                self.board.add(THIRD, NS, "NOTE", "chatter")
                res = await lab.wait(slot, self.pulse, cap_s=0.3, poll_s=0.05)
                slot.jobs["r1"].task.cancel()
            text = res["content"][0]["text"]
            self.assertIn("None of your jobs has finished yet and nothing new is addressed to you", text)
            self.assertIn("still running: r1 run", text)
            self.assertIn("Call `wait` again", text)
            self.assertIn("1 other new post in /swarm/t", text)
            self.assert_no_time_framing(text)
        asyncio.run(go())

    def test_an_undelivered_finished_job_returns_at_once(self):
        async def go():
            lab, slot = self.lab(delay=0.01)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                await slot.jobs["r1"].task
                t0 = time.monotonic()
                res = await lab.wait(slot, self.pulse, cap_s=10, poll_s=0.05)
                self.assertLess(time.monotonic() - t0, 0.5)
            self.assertIn("cem done", res["content"][0]["text"])
        asyncio.run(go())

    def test_nothing_to_wait_for_without_a_board(self):
        lab, slot = self.lab()
        res = asyncio.run(lab.wait(slot, None, cap_s=10))
        self.assertIn("Nothing to wait for", res["content"][0]["text"])

    def test_solo_waits_on_the_job_only(self):
        async def go():
            lab, slot = self.lab(delay=0.2)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                res = await lab.wait(slot, None, cap_s=10)
            self.assertIn("cem done", res["content"][0]["text"])
        asyncio.run(go())

    def test_board_down_still_returns_on_the_job(self):
        async def go():
            lab, slot = self.lab(delay=0.2)
            self.board.down = True
            with mock.patch.object(lab_mod, "_exec", self.exe):
                await lab.run(slot, "python cem.py", None, background=True)
                res = await lab.wait(slot, self.pulse, cap_s=10, poll_s=0.05)
            self.assertIn("cem done", res["content"][0]["text"])
            self.assertTrue(self.errors)
        asyncio.run(go())

    def test_the_tool_is_offered_with_and_without_a_board(self):
        lab, slot = self.lab()
        with_board = {s.name: s for s in lab_mod.lab_tool_specs(lab, slot, pulse=self.pulse)}
        solo = {s.name: s for s in lab_mod.lab_tool_specs(lab, slot)}
        self.assertIn("addressed to you", with_board["wait"].description)
        self.assertNotIn("board", solo["wait"].description)
        for d in (with_board["wait"].description, solo["wait"].description):
            self.assert_no_time_framing(d)
        res = asyncio.run(solo["wait"].handler({}))
        self.assertIn("Nothing to wait for", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
