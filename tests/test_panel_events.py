"""The panel's event log: the events endpoint folds transcripts, lab jobs, the round log and the board into
one append-only list, resumes by seq, never re-emits, and takes a half-written line only once it
is whole."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stigmergeia.config import RunConfig
from stigmergeia.panel import server
from stigmergeia.panel.server import Panel


def config(tmp: Path) -> RunConfig:
    return RunConfig.model_validate({
        "run_name": "eventtest", "runs_dir": str(tmp / "runs"), "task_dir": str(tmp / "task"),
        "n_agents": 2, "per_agent_budget_usd": 1, "total_budget_usd": 2,
        "board": {"url": "http://127.0.0.1:1", "operator_token_file": str(tmp / "op.token")},
        "gate": {},
    })


def append(p: Path, rows: list[dict], partial: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows) + partial)


class PanelEventsTests(unittest.TestCase):
    def test_fold_resume_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d))
            tr = cfg.run_dir / "private" / "a00" / "transcript.jsonl"
            append(tr, [
                {"t": 10, "_type": "AssistantMessage", "content": [
                    {"text": "I'll try BFS first."},
                    {"id": "u1", "name": "mcp__lab__score", "input": {"policy": "p.py"}}]},
                {"t": 12, "_type": "UserMessage", "content": [{"tool_use_id": "u1"}]},
            ], partial='{"t": 13, "_type": "Assistant')  # still being written
            append(cfg.run_dir / "opening_round.jsonl", [{"t": 5, "event": "sealed", "agent": "a00"}])
            append(cfg.run_dir / "private" / "a01" / "lab-jobs.jsonl",
                   [{"t": 20, "event": "run", "job": "r1", "label": "python cem.py", "background": True}])
            panel = Panel(cfg)
            first = panel.events_since(0)
            kinds = [(e["k"], e.get("agent")) for e in first["events"]]
            self.assertCountEqual(kinds, [("say", "a00"), ("act", "a00"), ("round", "a00"), ("job", "a01")])
            act = next(e for e in first["events"] if e["k"] == "act")
            self.assertEqual((act["tool"], act["what"]), ("score", "p.py"))
            self.assertEqual(first["meta"]["agents"][0], {"name": "a00", "backend": "claude", "model": cfg.model})

            # nothing new: the tail is empty and seq does not move
            again = panel.events_since(first["seq"])
            self.assertEqual((again["events"], again["seq"]), ([], first["seq"]))

            # the half-written line completes: it is folded exactly once
            with tr.open("a") as f:
                f.write('Message", "content": [{"text": "now CEM"}]}\n')
            append(cfg.run_dir / "private" / "a01" / "lab-jobs.jsonl",
                   [{"t": 60, "event": "finished", "job": "r1", "background": True, "took_s": 40}])
            more = panel.events_since(first["seq"])
            self.assertEqual([(e["k"], e.get("text") or e.get("phase")) for e in more["events"]],
                             [("say", "now CEM"), ("job", "finish")])
            self.assertEqual(more["seq"], first["seq"] + 2)
            # a seq past the end is clamped, never an error
            self.assertEqual(panel.events_since(10_000)["events"], [])

    def test_board_reads_are_incremental_and_paged(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config(Path(d))
            panel = Panel(cfg)
            panel.creds = {"a00": mock.Mock(identity="band:a", url="http://b", token="t"),
                           "a01": mock.Mock(identity="band:b", url="http://b", token="t")}
            panel.gate = mock.Mock(identity="band:g")
            board = [{"id": i, "ts": "2026-09-30T12:00:00Z", "author": "band:a", "type": "NOTE", "refs": [],
                      "payload": f"post {i}"} for i in range(1, 8)]
            board.append({"id": 8, "ts": "2026-09-30T12:00:05Z", "author": "band:g", "type": "FINDING",
                          "refs": [{"edge": "replies", "id": 3}],
                          "payload": {"kind": "gate-result", "agent": "a00", "policy": "p.py", "score": 71.2,
                                      "confirmed": True, "record": True}})
            seen = []

            def read(url, token):
                since = int(url.split("since=")[1].split("&")[0])
                seen.append(since)
                return {"envelopes": [e for e in board if e["id"] > since][:3]}

            with mock.patch.object(server, "BOARD_PAGE", 3), mock.patch.object(server.board, "_request", side_effect=read):
                panel._refresh_board()
                self.assertEqual(seen, [-1, 3, 6])  # pages until a short page
                evs = panel.events
                self.assertEqual([e["id"] for e in evs], list(range(1, 9)))
                g = evs[-1]
                self.assertEqual((g["k"], g["record"], g["score"], g["refs"]), ("gate", True, 71.2, [["replies", 3]]))
                self.assertEqual(evs[0]["who"], "a00")
                seen.clear()
                panel._refresh_board()  # nothing new: one read from the last id, no re-emits
                self.assertEqual((seen, len(panel.events)), ([8], 8))


if __name__ == "__main__":
    unittest.main()
