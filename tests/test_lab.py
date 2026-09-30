"""The lab's held-out bookkeeping: pooling, confirmation, records, in both
score directions. The node, the gate and the board are faked; what is under
test is only the decision logic in Lab.submit and _pooled."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from stigmergeia import board
from stigmergeia import lab as lab_mod
from stigmergeia.board import Credential
from stigmergeia.config import RunConfig
from stigmergeia.lab import AgentSlot, Lab, _pooled


def config(tmp: Path, **gate) -> RunConfig:
    return RunConfig.model_validate({
        "run_name": "labtest", "runs_dir": str(tmp / "runs"), "task_dir": str(tmp / "task"),
        "n_agents": 2, "per_agent_budget_usd": 1, "total_budget_usd": 2,
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "op.token")},
        "node": {"host": "nodeX", "run_root": "/r", "harness_dir": "/h", "venv": "/v"},
        "gate": gate,
    })


class FakeNode:
    """Scripted held-out batch means, in call order, plus a record of the argv."""

    def __init__(self, means: list[float], std: float = 0.5, episodes: int = 256,
                 probe: tuple[int, float, float] | None = None, batch_delay: float = 0.0):
        self.means, self.std, self.episodes = list(means), std, episodes
        self.cmds: list[list[str]] = []
        self.counter = 0
        self.probe = probe  # (gate exit, start-up seconds, probe-episodes seconds)
        self.batch_delay = batch_delay

    async def exec(self, argv, timeout):
        self.cmds.append(argv)
        if "PROBE" in argv[-1]:
            rc, s, e = self.probe or (0, 0.5, 1.0)
            return 0, f'{{"mean": 1}}\nPROBE {rc} 100.0 {100.0 + s} {100.0 + s + e}\n'
        return 0, f"{len(self.cmds):08x}  -\n"  # the snapshot step's digest line: a new submission each time

    async def allocate(self, n):
        self.counter += n
        return self.counter - n

    async def batch(self, slot, snap, policy, offset, tag):
        if self.batch_delay:
            await asyncio.sleep(self.batch_delay)
        m = self.means.pop(0)
        return 0, "", {"mean": m, "std": self.std, "episodes": self.episodes, "ends": {"ok": self.episodes},
                       "mean_steps": 1024, "seed_range": [offset, offset + self.episodes]}


class LabSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.ws = t / "ws"
        self.ws.mkdir()
        (self.ws / "train.py").write_text("x = 1\n")
        self.posts: list[dict] = []
        patches = [
            mock.patch.object(board, "post", lambda cred, ns, act, payload, refs=None:
                              (self.posts.append(payload), {"id": len(self.posts)})[1]),
            mock.patch.object(board, "read_since", lambda *a, **k: []),
            # a test must never reach a real node: a background task that outlives its test's own _exec
            # patch would run a real ssh and hang the suite
            mock.patch.object(lab_mod, "_exec", mock.AsyncMock(side_effect=RuntimeError("real _exec in a test"))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, node: FakeNode, **gate) -> tuple[Lab, AgentSlot]:
        cfg = config(Path(self.tmp.name), **gate)
        lab = Lab(cfg, Credential(url="http://x", identity="band:gate", token="t", display="gate"))
        lab._push = mock.AsyncMock(return_value=(0, ""))
        lab._allocate = node.allocate
        lab._batch = node.batch
        slot = AgentSlot(name="a00", index=0, workspace=self.ws, private=self.ws, remote_ws="/r/agents/a00",
                         cores="0-3")
        return lab, slot

    def submit(self, lab, slot, node, policy="train.py"):
        with mock.patch.object(lab_mod, "_exec", node.exec):
            asyncio.run(lab.submit(slot, policy))
        return self.posts[-1]

    def test_lower_is_better_records_confirmation_and_ties(self):
        node = FakeNode([2.00, 2.02, 1.99, 1.99, 1.80, 1.81, 2.50])
        lab, slot = self.lab(node, higher_is_better=False, run_sd=0.02, episode_cpu_s=None,
                             heldout_timeout_s=1200, heldout_episodes=256)
        p = self.submit(lab, slot, node)  # first: a candidate, confirmed by a second run
        self.assertTrue(p["confirmed"] and p["record"])
        self.assertEqual(p["score"], 2.01)
        self.assertEqual(lab.record, 2.01)
        p = self.submit(lab, slot, node)  # 1.99 < 2.01 but inside the noise: confirmed, a tie
        self.assertTrue(p["confirmed"])
        self.assertFalse(p["record"])
        self.assertTrue(p["tie"])
        self.assertEqual(lab.record, 2.01)
        p = self.submit(lab, slot, node)  # clearly lower: record
        self.assertTrue(p["record"])
        self.assertEqual(lab.record, 1.805)
        p = self.submit(lab, slot, node)  # worse than the record: one run, no confirmation spent
        self.assertFalse(p["confirmed"])
        self.assertEqual(len(p["batches"]), 1)
        self.assertEqual(lab.record, 1.805)

    def test_higher_is_better_default_is_unchanged(self):
        node = FakeNode([50.0, 52.0, 10.0], std=10.0, episodes=500)
        lab, slot = self.lab(node)
        p = self.submit(lab, slot, node)
        self.assertTrue(p["record"])
        self.assertEqual(lab.record, 51.0)
        p = self.submit(lab, slot, node)
        self.assertFalse(p["confirmed"])  # below the record: not a candidate

    def test_timeout_and_episode_cpu_flags(self):
        node = FakeNode([])
        lab, _ = self.lab(node, higher_is_better=False, episode_cpu_s=None, heldout_timeout_s=1300)
        self.assertEqual(lab._batch_timeout(), 1300)
        lab2, _ = self.lab(node, heldout_episodes=500, episode_cpu_s=20.0)
        self.assertEqual(lab2._batch_timeout(), 500 * 20 // 4 + 120)  # the snake formula, unchanged

    def test_dir_digest_covers_every_py_file_of_the_submission(self):
        node = FakeNode([])
        lab, _ = self.lab(node, digest="dir")
        cmd = lab._digest_cmd("/r/sub/a00/s1", "train.py")
        self.assertIn("find . -type f -name '*.py'", cmd)
        self.assertTrue(cmd.startswith("cd /r/sub/a00/s1 "))
        lab_f, _ = self.lab(node)
        self.assertEqual(lab_f._digest_cmd("/r/s", "p.py"), "sha256sum /r/s/p.py")


class BackgroundSubmitTests(unittest.TestCase):
    """A submission estimated to take longer than background_after_s returns at
    once with a job id; its result is posted exactly as a synchronous one and
    reaches the agent on its next lab call (or `jobs`); meanwhile the agent's
    cores are the job's, so run/score/submit refuse instead of blocking."""

    def setUp(self):
        LabSubmitTests.setUp(self)

    def tearDown(self):
        self.tmp.cleanup()

    lab = LabSubmitTests.lab

    def test_estimate_from_the_probe(self):
        async def go():
            # start-up 0.5 s (paid once per batch); 8 probe episodes took 10 s in all
            node = FakeNode([], probe=(0, 0.5, 10.0))
            lab, slot = self.lab(node, heldout_episodes=500, probe_episodes=8)
            with mock.patch.object(lab_mod, "_exec", node.exec):
                est, how = await lab._estimate(slot, "train.py")
            self.assertEqual(est, round(0.5 + (10.0 - 0.5) * 500 / 8))
            self.assertIn("timed on 8", how)
            self.assertIn("--episodes 0", node.cmds[-1][-1])
            node.probe = (124, 0.5, 60.0)  # cut off at the cap: a lower bound, still an estimate
            with mock.patch.object(lab_mod, "_exec", node.exec):
                est, how = await lab._estimate(slot, "train.py")
            self.assertEqual(est, 60 * 500 // 8)
            self.assertIn("cut off", how)
            node.probe = (1, 0.5, 0.1)  # the policy crashed: unknown, scored in the foreground
            with mock.patch.object(lab_mod, "_exec", node.exec):
                est, _ = await lab._estimate(slot, "train.py")
            self.assertIsNone(est)
            lab2, slot2 = self.lab(node, estimate_s=700)  # a fixed estimate: no probe at all
            n0 = len(node.cmds)
            with mock.patch.object(lab_mod, "_exec", node.exec):
                est, how = await lab2._estimate(slot2, "train.py")
            self.assertEqual((est, len(node.cmds)), (700, n0))
        asyncio.run(go())

    def test_slow_submission_runs_in_background_and_result_is_delivered(self):
        async def go():
            node = FakeNode([50.0, 52.0], std=10.0, episodes=500, probe=(0, 0.5, 20.0), batch_delay=0.2)
            lab, slot = self.lab(node, background_after_s=180)
            with mock.patch.object(lab_mod, "_exec", node.exec):
                t0 = time.monotonic()
                res = await lab.submit(slot, "train.py")
                self.assertLess(time.monotonic() - t0, 0.15)  # did not wait for the batch
                text = res["content"][0]["text"]
                self.assertIn("IN THE BACKGROUND as job s1", text)
                self.assertFalse(res["is_error"])
                # the cores are the job's: run and submit refuse at once
                busy = await lab.run(slot, "echo hi", None)
                self.assertTrue(busy["is_error"])
                self.assertIn("running your held-out submission", busy["content"][0]["text"])
                self.assertTrue((await lab.submit(slot, "train.py"))["is_error"])
                self.assertEqual(lab.take_notes(slot, "test"), "")  # nothing finished yet
                await slot.jobs["s1"].task
            self.assertEqual(self.posts[-1]["kind"], "gate-result")  # posted by the gate, same semantics
            self.assertTrue(self.posts[-1]["record"])
            self.assertEqual(lab.record, 51.0)
            notes = lab.take_notes(slot, "test")
            self.assertIn("held-out job s1 (train.py) finished", notes)
            self.assertIn("NEW RECORD", notes)
            self.assertEqual(lab.take_notes(slot, "test"), "")  # delivered once
            self.assertIn("finished", lab.jobs_text(slot))
            rows = [json.loads(l) for l in (self.ws / "lab-jobs.jsonl").read_text().splitlines()]
            self.assertEqual([r["event"] for r in rows], ["submit", "finished", "delivered"])
            self.assertTrue(rows[0]["background"])
        asyncio.run(go())

    def test_fast_submission_stays_synchronous_with_its_estimate(self):
        async def go():
            node = FakeNode([50.0, 52.0], std=10.0, episodes=500, probe=(0, 0.5, 0.2))
            lab, slot = self.lab(node, background_after_s=180)
            with mock.patch.object(lab_mod, "_exec", node.exec):
                res = await lab.submit(slot, "train.py")
            text = res["content"][0]["text"]
            self.assertIn("one batch estimated at", text)
            self.assertIn("NEW RECORD", text)
            self.assertEqual(slot.jobs, {})
        asyncio.run(go())

    def test_background_off_is_the_old_synchronous_submit(self):
        async def go():
            node = FakeNode([50.0, 52.0], std=10.0, episodes=500, probe=(0, 0.5, 500.0))
            lab, slot = self.lab(node, background_after_s=None)
            with mock.patch.object(lab_mod, "_exec", node.exec):
                res = await lab.submit(slot, "train.py")
            self.assertNotIn("PROBE", " ".join(c[-1] for c in node.cmds))  # no probe at all
            self.assertIn("NEW RECORD", res["content"][0]["text"])
        asyncio.run(go())


class BackgroundRunTests(unittest.TestCase):
    """`run` can go to the background (asked, a long timeout, or outlasting
    run_background_after_s); one background run per agent; light foreground
    calls share its cores under a cap; submit waits for it."""

    def setUp(self):
        LabSubmitTests.setUp(self)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, delay: float = 0.0, rc: int = 0, out: str = "hello\n", **node):
        base = config(Path(self.tmp.name)).model_dump()
        base["node"].update(node)
        cfg = RunConfig.model_validate(base)
        lab = Lab(cfg, Credential(url="http://x", identity="band:gate", token="t", display="gate"))
        lab._push = mock.AsyncMock(return_value=(0, ""))
        self.pulls = []

        async def pull(slot, update=False):
            self.pulls.append(update)
            return 0, ""
        lab._pull = pull
        self.cmds = []

        async def exe(argv, timeout):
            self.cmds.append((argv, timeout))
            if delay:
                await asyncio.sleep(delay)
            return rc, out
        self.exe = exe
        slot = AgentSlot(name="a00", index=0, workspace=self.ws, private=self.ws, remote_ws="/r/agents/a00",
                         cores="0-3")
        return lab, slot

    def test_background_true_returns_a_job_and_delivers_its_output(self):
        async def go():
            lab, slot = self.lab(delay=0.2)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                t0 = time.monotonic()
                res = await lab.run(slot, "python cem.py", None, background=True)
                self.assertLess(time.monotonic() - t0, 0.15)
                text = res["content"][0]["text"]
                self.assertFalse(res["is_error"])
                self.assertIn("IN THE BACKGROUND as job r1", text)
                self.assertIn("up to 1800 s", text)
                self.assertIn("r1 run `python cem.py`: running", lab.jobs_text(slot))
                await slot.jobs["r1"].task
            self.assertIn("--timeout 1800", self.cmds[0][0][-1])  # the background cap reaches the jail
            self.assertEqual(self.pulls, [True])  # pulled with --update: local edits meanwhile survive
            notes = lab.take_notes(slot, "test")
            self.assertIn("background run r1 (`python cem.py`) finished", notes)
            self.assertIn("exit 0\nhello", notes)
            self.assertEqual(lab.take_notes(slot, "test"), "")
            self.assertTrue(list((self.ws / ".runs").glob("*-run.log")))
            rows = [json.loads(l) for l in (self.ws / "lab-jobs.jsonl").read_text().splitlines()]
            self.assertEqual([(r["event"], r.get("job")) for r in rows],
                             [("run", "r1"), ("finished", "r1"), ("delivered", "r1")])
        asyncio.run(go())

    def test_long_timeout_goes_to_background_and_says_why(self):
        async def go():
            lab, slot = self.lab(delay=0.05)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab.run(slot, "bash grid.sh", 3400)
                text = res["content"][0]["text"]
                self.assertIn("job r1", text)
                self.assertIn("you asked for up to 3400 s", text)
                self.assertIn("capped at 1800 s", text)
                await slot.jobs["r1"].task
            res = None
            lab2, slot2 = self.lab(delay=0.0)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab2.run(slot2, "echo hi", 150)  # under the threshold: foreground
            self.assertTrue(res["content"][0]["text"].startswith("exit 0"))
            self.assertEqual(slot2.jobs, {})
        asyncio.run(go())

    def test_a_foreground_run_that_outlasts_the_threshold_moves_to_the_background(self):
        async def go():
            lab, slot = self.lab(delay=0.4, run_background_after_s=0.1)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                t0 = time.monotonic()
                res = await lab.run(slot, "python slow.py", None)
                self.assertLess(time.monotonic() - t0, 0.3)
                self.assertIn("moved to the BACKGROUND as job r1", res["content"][0]["text"])
                self.assertFalse(slot.fg_lock.locked())  # the agent's next foreground call is not held up
                await slot.jobs["r1"].task
            self.assertIn("exit 0", lab.take_notes(slot, "test"))
            self.assertIn("--timeout 600", self.cmds[0][0][-1])  # it keeps its foreground cap
        asyncio.run(go())

    def test_off_is_the_old_synchronous_run(self):
        async def go():
            lab, slot = self.lab(delay=0.2, run_background_after_s=None)
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab.run(slot, "python slow.py", 5000, background=True)
            self.assertTrue(res["content"][0]["text"].startswith("exit 0"))
            self.assertEqual(slot.jobs, {})
            names = {s.name: s for s in lab_mod.lab_tool_specs(lab, slot)}
            self.assertNotIn("background", names["run"].schema["properties"])
        asyncio.run(go())

    def test_two_background_runs_share_light_calls_share_submit_queues(self):
        async def go():
            lab, slot = self.lab()
            timeline: list[tuple[str, str, float]] = []

            async def exe(argv, timeout):  # the background runs are slow, the light calls quick
                self.cmds.append((argv, timeout))
                tag = "cem" if "cem.py" in argv[-1] else "grid" if "grid.py" in argv[-1] else "light"
                timeline.append((tag, "start", time.monotonic()))
                await asyncio.sleep(0.5 if tag == "cem" else 0.3 if tag == "grid" else 0.01)
                timeline.append((tag, "end", time.monotonic()))
                if "PROBE" in argv[-1]:
                    return 0, "PROBE 0 1 2 3\n"
                return 0, "ok\n" if tag != "light" or "sha256sum" not in argv[-1] else "abcd  -\n"
            node = FakeNode([50.0, 50.0], batch_delay=0.05)
            lab._allocate, lab._batch = node.allocate, node.batch
            orig_batch = lab._batch

            async def batch(*a):
                timeline.append(("heldout", "start", time.monotonic()))
                r = await orig_batch(*a)
                timeline.append(("heldout", "end", time.monotonic()))
                return r
            lab._batch = batch
            with mock.patch.object(lab_mod, "_exec", exe):
                await lab.run(slot, "python cem.py", None, background=True)
                await asyncio.sleep(0.05)  # r1 is on the node
                second = await lab.run(slot, "python grid.py", None, background=True)
                self.assertFalse(second["is_error"])
                self.assertIn("job r2", second["content"][0]["text"])
                self.assertIn("shares your cores with background run r1", second["content"][0]["text"])
                await asyncio.sleep(0.05)  # r2 is on the node
                third = await lab.run(slot, "python other.py", None, background=True)
                self.assertTrue(third["is_error"])
                self.assertIn("Nothing was started: your background runs r1", third["content"][0]["text"])
                self.assertIn("up to 2 at a time", third["content"][0]["text"])
                long_ask = await lab.run(slot, "ls", 500)  # a long timeout is a background ask: refused too
                self.assertIn("Nothing was started", long_ask["content"][0]["text"])
                light = await lab.run(slot, "ls", 90)  # a light call shares the cores, capped
                self.assertFalse(light["is_error"])
                self.assertIn("shared your cores with background run r1", light["content"][0]["text"])
                self.assertIn("--timeout 90", self.cmds[-1][0][-1])
                nolimit = await lab.run(slot, "ls", None)
                self.assertIn("--timeout 120", self.cmds[-1][0][-1])
                self.assertIn("capped at 120 s", nolimit["content"][0]["text"])
                sub = await lab.submit(slot, "train.py")  # queued behind r1 and r2, snapshot frozen now
                text = sub["content"][0]["text"]
                self.assertFalse(sub["is_error"], text)
                self.assertIn("QUEUED as job s1", text)
                self.assertIn("r1", text)
                self.assertIn("r2", text)
                self.assertIn("s1 submit train.py: queued", lab.jobs_text(slot))
                again = await lab.submit(slot, "train.py")  # one held-out submission at a time
                self.assertTrue(again["is_error"])
                self.assertIn("s1 (train.py) is queued", again["content"][0]["text"])
                bg_while_queued = await lab.run(slot, "python more.py", None, background=True)
                self.assertTrue(bg_while_queued["is_error"])
                self.assertIn("is queued for your cores", bg_while_queued["content"][0]["text"])
                await slot.jobs["s1"].task
            self.assertEqual(set(slot.jobs), {"r1", "r2", "s1"})
            self.assertIsNotNone(slot.jobs["s1"].result)
            self.assertFalse(slot.jobs["s1"].is_error, slot.jobs["s1"].result)
            self.assertEqual(len(self.posts), 1)  # the gate posted it
            # the held-out batch started only after BOTH runs ended: it had the cores to itself
            runs_end = max(t for tag, ev, t in timeline if tag in ("cem", "grid") and ev == "end")
            heldout_start = next(t for tag, ev, t in timeline if tag == "heldout" and ev == "start")
            self.assertGreaterEqual(heldout_start, runs_end)
            rows = [json.loads(line) for line in (self.ws / "lab-jobs.jsonl").read_text().splitlines()]
            q = next(r for r in rows if r.get("event") == "submit")
            self.assertTrue(q["queued"])
            self.assertEqual(q["behind"], ["r1", "r2"])
            self.assertTrue(any(r.get("event") == "dequeued" and r["job"] == "s1" for r in rows))
        asyncio.run(go())

    def test_a_queued_submission_waits_for_a_foreground_call_in_flight(self):
        async def go():
            lab, slot = self.lab()
            order: list[str] = []

            async def exe(argv, timeout):
                if "cem.py" in argv[-1]:
                    await asyncio.sleep(0.1)
                elif "light.py" in argv[-1]:
                    order.append("light start")
                    await asyncio.sleep(0.4)
                    order.append("light end")
                elif "sha256sum" in argv[-1]:
                    return 0, "abcd  -\n"
                return 0, "ok\n"
            node = FakeNode([50.0, 50.0])  # a first result is a record candidate: confirmed by a second batch

            async def batch(*a):
                order.append("heldout")
                return await node.batch(*a)
            lab._allocate, lab._batch = node.allocate, batch
            with mock.patch.object(lab_mod, "_exec", exe):
                await lab.run(slot, "python cem.py", None, background=True)
                light = asyncio.create_task(lab.run(slot, "python light.py", 60))
                await asyncio.sleep(0.02)
                sub = await lab.submit(slot, "train.py")
                self.assertIn("QUEUED", sub["content"][0]["text"])
                await light
                await slot.jobs["s1"].task
            self.assertEqual(order, ["light start", "light end", "heldout", "heldout"])
        asyncio.run(go())

    def test_held_out_job_still_holds_the_cores(self):
        async def go():
            lab, slot = self.lab()
            slot.jobs["c1"] = lab_mod.Job(id="c1", policy="p.py", kind="confirm", started=time.time())
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab.run(slot, "ls", None, background=True)
            self.assertTrue(res["is_error"])
            self.assertIn("confirmation batch", res["content"][0]["text"])
            self.assertEqual(self.cmds, [])
        asyncio.run(go())


class FailureTextTests(unittest.TestCase):
    """An 'exit 1 with an empty log' is almost always the jail's wall cap
    (systemd stops the unit and prints nothing), and an agent that asks for
    more than the cap is cut at the cap. Every such failure says so."""

    def setUp(self):
        LabSubmitTests.setUp(self)

    def tearDown(self):
        self.tmp.cleanup()

    lab = BackgroundRunTests.lab

    def test_empty_nonzero_exit_says_so(self):
        async def go():
            lab, slot = self.lab(rc=1, out="")
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab.run(slot, "cd c && python x.py > log 2>&1", 30)
            text = res["content"][0]["text"]
            self.assertTrue(res["is_error"])
            self.assertIn("NO output at all", text)
            self.assertIn("workspace root", text)
        asyncio.run(go())

    def test_stopped_at_the_cap_says_so_and_how_to_get_more(self):
        lab, _ = self.lab()
        note = lab._failure_note(1, "", 606.0, 600, 3400, "foreground", None)
        self.assertIn("STOPPED at this call's 600 s limit before it printed anything", note)
        self.assertIn("You asked for 3400 s", note)
        self.assertIn("background: true", note)
        self.assertIn("pulled back", note)
        self.assertIn("background run is capped at 1800 s", lab._failure_note(124, "x", 5, 1800, None, "background", None))
        self.assertIn("memory limit", lab._failure_note(137, "", 3, 600, None, "foreground", None))
        self.assertEqual(lab._failure_note(1, "Traceback: boom", 3, 600, None, "foreground", None), "")

    def test_score_failure_names_itself_and_keeps_the_output(self):
        async def go():
            lab, slot = self.lab(rc=1, out="Traceback (most recent call last):\nValueError: bad move\n")
            with mock.patch.object(lab_mod, "_exec", self.exe):
                res = await lab.score(slot, "train.py")
                text = res["content"][0]["text"]
                self.assertTrue(text.startswith("score of train.py FAILED"))
                self.assertIn("ValueError: bad move", text)
                missing = await lab.score(slot, "c/nothere.py")
            self.assertTrue(missing["is_error"])
            self.assertIn("is not a file in your workspace", missing["content"][0]["text"])
            self.assertEqual(len(self.cmds), 1)  # the missing file never reached the node
        asyncio.run(go())


class AutoConfirmTests(unittest.TestCase):
    """A single batch that is no record candidate but clearly beats its agent's
    own confirmed best earns one background confirmation batch; records and ties
    are judged as for any confirmed score."""

    def setUp(self):
        LabSubmitTests.setUp(self)

    def tearDown(self):
        self.tmp.cleanup()

    lab = LabSubmitTests.lab

    def test_first_batch_below_the_record_is_confirmed_in_the_background(self):
        async def go():
            node = FakeNode([80.0, 81.0], std=10.0, episodes=500)
            lab, slot = self.lab(node, background_after_s=None)
            lab.record, lab._board_loaded = 88.0, True  # someone else's record
            with mock.patch.object(lab_mod, "_exec", node.exec):
                res = await lab.submit(slot, "train.py")
                text = res["content"][0]["text"]
                self.assertIn("UNCONFIRMED", text)
                self.assertIn("confirmation batch is running", text)
                self.assertIn("as job c1", text)
                self.assertFalse(self.posts[-1]["confirmed"])
                busy = await lab.run(slot, "ls", None)  # the confirmation holds the cores
                self.assertIn("confirmation batch", busy["content"][0]["text"])
                await slot.jobs["c1"].task
            p = self.posts[-1]
            self.assertTrue(p["confirmed"])
            self.assertFalse(p["record"])
            self.assertEqual(len(p["batches"]), 2)
            self.assertEqual(p["score"], 80.5)
            self.assertEqual(lab.agent_best["a00"], 80.5)
            self.assertEqual(lab.record, 88.0)
            self.assertIn("confirmation batch c1 (train.py) finished", lab.take_notes(slot, "test"))
        asyncio.run(go())

    def test_margin_and_bound(self):
        async def go():
            # se = 10 / sqrt(500) = 0.447: 80.3 does not clear 80 + 1 se, 81.0 does
            node = FakeNode([80.3, 81.0, 81.1, 82.0, 83.0], std=10.0, episodes=500)
            lab, slot = self.lab(node, background_after_s=None, auto_confirm_per_hour=1)
            lab.record, lab._board_loaded = 88.0, True
            lab.agent_best["a00"] = 80.0
            with mock.patch.object(lab_mod, "_exec", node.exec):
                await lab.submit(slot, "train.py")
                self.assertEqual(slot.jobs, {})  # inside the margin: no confirmation
                await lab.submit(slot, "train.py")  # a new file each time (FakeNode's digest): 81.0 clears it
                self.assertIn("c1", slot.jobs)
                await slot.jobs["c1"].task
                res = await lab.submit(slot, "train.py")  # 82.0 would clear 81.05, but the bound is reached
                self.assertEqual(set(slot.jobs), {"c1"})
                self.assertNotIn("confirmation batch is running", res["content"][0]["text"])
                self.assertNotIn("hour", res["content"][0]["text"])  # the bound is behaviour, never shown
                slot.auto_confirms[:] = [time.time() - 3700]  # an hour later
                await lab.submit(slot, "train.py")
                self.assertIn("c2", slot.jobs)
                await slot.jobs["c2"].task
        asyncio.run(go())

    def test_off(self):
        node = FakeNode([80.0], std=10.0, episodes=500)
        lab, slot = self.lab(node, background_after_s=None, auto_confirm_margin_se=None)
        lab.record, lab._board_loaded = 88.0, True
        LabSubmitTests.submit(self, lab, slot, node)
        self.assertEqual(slot.jobs, {})

    def test_agent_best_is_seeded_from_the_board(self):
        node = FakeNode([])
        lab, _ = self.lab(node)
        env = [{"id": 1, "author": "band:gate"}, {"id": 2, "author": "band:gate"}]
        payloads = {1: {"kind": "gate-result", "agent": "a00", "score": 70.0, "confirmed": True, "record": True},
                    2: {"kind": "gate-result", "agent": "a00", "score": 90.0, "confirmed": False}}
        with mock.patch.object(board, "read_since", lambda *a, **k: env), \
                mock.patch.object(board, "_request", lambda url, tok: {"payload": payloads[int(url.rsplit("/", 1)[1])]}):
            asyncio.run(lab._current_record())
        self.assertEqual(lab.agent_best, {"a00": 70.0})


class PooledTests(unittest.TestCase):
    def test_episode_pooling_is_unchanged(self):
        m, ci = _pooled([{"mean": 50, "std": 10, "episodes": 500}, {"mean": 52, "std": 10, "episodes": 500}])
        self.assertEqual(m, 51.0)
        self.assertAlmostEqual(ci[1] - m, 1.96 * ((2 * 499 * 100 + 1000) / 999) ** 0.5 / 1000 ** 0.5, places=3)

    def test_run_noise_floor_does_not_shrink_with_episodes(self):
        few = _pooled([{"mean": 2.0, "std": 0.4, "episodes": 256}] * 2, run_sd=0.02)
        many = _pooled([{"mean": 2.0, "std": 0.4, "episodes": 10**6}] * 2, run_sd=0.02)
        self.assertAlmostEqual(many[1][1] - many[0], 1.96 * 0.02 / 2 ** 0.5, places=3)  # the floor remains
        self.assertGreater(few[1][1] - few[0], many[1][1] - many[0])
        # runs that disagree more than the floor widen it
        wide = _pooled([{"mean": 2.0, "std": 0.4, "episodes": 10**6}, {"mean": 2.2, "std": 0.4, "episodes": 10**6}],
                       run_sd=0.02)
        self.assertGreater(wide[1][1] - wide[0], 0.1)


if __name__ == "__main__":
    unittest.main()
