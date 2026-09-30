"""Background lab jobs are visible, and counted as compute rather than idle:
the panel folds the lab's own private/<agent>/lab-jobs.jsonl, and
stigmergeia.analysis.idle splits blocked lab calls by tool."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from stigmergeia.config import RunConfig
from stigmergeia.panel.server import Panel

from stigmergeia.analysis import idle as idle_windows  # noqa: E402


def config(tmp: Path) -> RunConfig:
    return RunConfig.model_validate({
        "run_name": "paneltest", "runs_dir": str(tmp / "runs"), "task_dir": str(tmp / "task"),
        "n_agents": 1, "per_agent_budget_usd": 1, "total_budget_usd": 1,
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "op.token")},
        "gate": {},
    })


def write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


class PanelJobsTests(unittest.TestCase):
    def test_running_and_finished_background_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d))
            now = time.time()
            priv = cfg.run_dir / "private" / "a00"
            write_jsonl(priv / "transcript.jsonl", [{"t": now - 600, "_type": "SystemMessage"}])
            write_jsonl(priv / "lab-jobs.jsonl", [
                {"t": now - 500, "event": "submit", "policy": "p.py", "background": False},  # foreground: not a job
                {"t": now - 400, "event": "finished", "policy": "p.py", "background": False, "took_s": 90},
                {"t": now - 300, "event": "submit", "policy": "p.py", "background": True, "job": "s1"},
                {"t": now - 180, "event": "finished", "job": "s1", "kind": "submit", "background": True, "took_s": 120},
                {"t": now - 170, "event": "job", "job": "c1", "kind": "confirm", "policy": "p.py"},
                {"t": now - 120, "event": "run", "job": "r1", "label": "`python cem.py`", "background": True},
            ])
            a = Panel(cfg).state()["agents"][0]
            self.assertEqual({j["id"] for j in a["jobs"]}, {"c1", "r1"})
            r1 = next(j for j in a["jobs"] if j["id"] == "r1")
            self.assertEqual((r1["kind"], r1["what"]), ("run", "`python cem.py`"))
            self.assertAlmostEqual(r1["age"], 120, delta=5)
            self.assertEqual(a["bg_done"], 1)
            self.assertAlmostEqual(a["bg_min"], (120 + 170 + 120) / 60, delta=0.2)


class IdleWindowsTests(unittest.TestCase):
    def test_blocked_split_and_background(self):
        rows = [
            {"t": 0, "_type": "AssistantMessage", "content": [{"id": "u1", "name": "mcp__lab__run", "input": {}}]},
            {"t": 400, "_type": "UserMessage", "content": [{"tool_use_id": "u1"}]},
            {"t": 400, "_type": "AssistantMessage", "content": [{"id": "u2", "name": "mcp__lab__submit", "input": {}}]},
            {"t": 700, "_type": "UserMessage", "content": [{"tool_use_id": "u2"}]},
            {"t": 700, "_type": "AssistantMessage", "content": [{"id": "u3", "name": "mcp__lab__score", "input": {}}]},
            {"t": 760, "_type": "UserMessage", "content": [{"tool_use_id": "u3"}]},  # short: not blocked
            {"t": 760, "_type": "AssistantMessage", "content": [{"id": "u4", "name": "mcp__lab__answer", "input": {}}]},
        ]
        waits, sleeps, brun, bsub, both, lwait = idle_windows.intervals(rows, 1000)
        self.assertEqual((brun, bsub, both, lwait), ([(0, 400)], [(400, 700)], [(760, 1000)], []))
        # the lab's wait: its own category, counted at ANY length (never blocked-other)
        waited = [
            {"t": 0, "_type": "AssistantMessage", "content": [{"id": "w1", "name": "mcp__lab__wait", "input": {}}]},
            {"t": 40, "_type": "UserMessage", "content": [{"tool_use_id": "w1"}]},
            {"t": 50, "_type": "AssistantMessage", "content": [{"id": "w2", "name": "mcp__lab__wait", "input": {}}]},
        ]
        *_, both, lwait = idle_windows.intervals(waited, 100)
        self.assertEqual((both, lwait), ([], [(0, 40), (50, 100)]))
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                write_jsonl(Path("runs/r/private/a00/lab-jobs.jsonl"), [
                    {"t": 100, "event": "run", "job": "r1"},
                    {"t": 300, "event": "finished", "job": "r1", "background": True, "took_s": 200},
                    {"t": 500, "event": "submit", "job": "s1", "background": True},
                ])
                self.assertEqual(idle_windows.background("r", "a00", 900), [(100, 300), (500, 900)])
            finally:
                os.chdir(cwd)
            # an explicit runs dir reads the same run from anywhere
            self.assertEqual(idle_windows.background("r", "a00", 900, Path(d) / "runs"), [(100, 300), (500, 900)])


if __name__ == "__main__":
    unittest.main()
