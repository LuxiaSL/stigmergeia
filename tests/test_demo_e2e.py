"""The whole harness, end to end, with scripted agents: a real board, the
opening round, the lab on this machine, the gate, posts with edges. Slow (a
run of a minute and a half), so it is marked and runs as its own CI step:
    pytest -m slow tests/test_demo_e2e.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter

import pytest

from stigmergeia.demo import run_demo

pytestmark = pytest.mark.slow


@pytest.mark.skipif(shutil.which("rsync") is None, reason="the demo's lab syncs workspaces with rsync")
def test_a_scripted_swarm_runs_the_whole_protocol() -> None:
    rc, run_dir, db = run_demo(agents=2, minutes=1.5, port=None)
    try:
        assert rc == 0
        rounds = [json.loads(line) for line in (run_dir / "opening_round.jsonl").read_text().splitlines()]
        assert sum(r.get("event") == "answered" for r in rounds) == 2, "both agents answered the round"
        con = sqlite3.connect(db)
        edges: Counter[str] = Counter()
        gate_results = 0
        for (rec,) in con.execute("select record from envelopes where ns like '/swarm/%'"):
            e = json.loads(rec)
            edges.update(r["edge"] for r in e.get("refs") or [])
            p = e.get("payload")
            gate_results += isinstance(p, dict) and p.get("kind") == "gate-result"
        assert gate_results >= 2, "the gate posted held-out results"
        assert edges["replies"] >= 2, "the answers reply to the proposals"
        for name in ("a00", "a01"):
            rows = [json.loads(line) for line in (run_dir / "private" / name / "transcript.jsonl").read_text()
                    .splitlines()]
            errors = [b for r in rows if r.get("_type") == "UserMessage" for b in r["content"] if b.get("is_error")]
            assert not errors, f"{name}: tool errors {errors[:2]}"
            assert rows[-1]["_type"] == "agent_end"
        m = json.loads((run_dir / "run-manifest.json").read_text())
        assert m["korax_commit"] and m["board_db"]["sha256"] and m["results"]["failed"] == 0
        assert "02-swarm-environment.md" in m["canon"] and "gate.py" in m["task"]["files"]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(db.parent, ignore_errors=True)
