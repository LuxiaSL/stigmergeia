"""Token economics of swarm runs: what each API call was spent on, and cost per point.

Every assistant message is one API call; its usage is priced with the run's
config prices and attributed to one activity from the tool calls it made:

- work:  edits, local code/shell work, file reads, lab run/score/submit
- board: reading or posting on the board (korax CLI or MCP)
- wait:  sleeps, job polling (lab.jobs / lab.wait), watches, and turns with no
         tool call at all (e.g. "nothing new this turn")

Sleeping itself costs nothing; the calls that decide to sleep, poll, or say
there is nothing to do are what "wait" counts.

    uv run python -m stigmergeia.analysis.economics <run> [<run> ...] [--baseline 28.7]
        [--runs-dir runs] [--boards-dir boards] [--configs-dir .]

A run's transcripts are read from <runs-dir>/<run>/, its best confirmed gate
score from <boards-dir>/<run>/board.db, and its prices from the run config
<configs-dir>/<run>.yaml (without it, calls are counted but not priced). The
defaults are relative to the working directory: run from the repository root.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import yaml

from stigmergeia.config import RunConfig

RUNS_DIR = Path("runs")
BOARDS_DIR = Path("boards")
CONFIGS_DIR = Path(".")
SLEEP = re.compile(r"\bsleep\s+\d")
# the median chars per reported output token over agents with turn ends (range 1.0-2.0; --calibrate measures it
# for any run): visible text undercounts thinking
CHARS_PER_TOKEN = 1.42


def classify(blocks: list[dict]) -> str:
    calls = [b for b in blocks if "name" in b and "input" in b]
    if not calls:
        return "wait"
    kinds = set()
    for b in calls:
        n, inp = b["name"], b["input"]
        cmd = str(inp.get("command", "")) if isinstance(inp, dict) else ""
        if n in ("mcp__lab__jobs", "mcp__lab__wait") or n.endswith("__jobs") or n.endswith("__wait"):
            kinds.add("wait")
        elif n in ("Bash", "Monitor") and (SLEEP.search(cmd) or "korax watch" in cmd):
            kinds.add("wait")
        elif n.startswith("mcp__korax") or (n == "Bash" and re.match(r"\s*(cd [^;&]*[;&]+\s*)?korax\b", cmd)):
            kinds.add("board")
        elif n in ("TaskOutput", "TaskStop"):
            kinds.add("wait")
        else:
            kinds.add("work")
    for k in ("work", "board", "wait"):  # a call that did any work counts as work
        if k in kinds:
            return k
    return "work"


def best_score(run: str, higher: bool = True, boards_dir: Path = BOARDS_DIR) -> float | None:
    db = boards_dir / run / "board.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    best = None
    for (rec,) in con.execute("select record from envelopes"):
        p = json.loads(rec).get("payload")
        if isinstance(p, dict) and p.get("kind") == "gate-result" and p.get("confirmed") is not False:
            s = p.get("score")
            if isinstance(s, (int, float)) and (best is None or (s > best if higher else s < best)):
                best = s
    return best


def analyse(run: str, runs_dir: Path = RUNS_DIR, boards_dir: Path = BOARDS_DIR,
            configs_dir: Path = CONFIGS_DIR) -> dict:
    cfgf = configs_dir / f"{run}.yaml"
    cfg = RunConfig.model_validate(yaml.safe_load(cfgf.read_text())) if cfgf.exists() else None
    files = sorted(glob.glob(str(runs_dir / run / "private" / "*" / "transcript.jsonl")) +
                   glob.glob(str(runs_dir / run / "agents" / "*" / "private" / "transcript.jsonl")))
    cost = defaultdict(float)
    toks = defaultdict(lambda: defaultdict(int))
    calls = defaultdict(int)
    for f in files:
        # One API call arrives as several rows (thinking, text, each tool use) sharing a
        # message_id: merge their blocks, and take each usage field's max across the rows.
        msgs: dict[str, dict] = {}
        for line in open(f):
            r = json.loads(line)
            if r.get("_type") != "AssistantMessage":
                continue
            mid = r.get("message_id") or f"row{len(msgs)}"
            m = msgs.setdefault(mid, {"blocks": [], "usage": {}, "chars": 0})
            m["blocks"].extend(r.get("content") or [])
            for b in r.get("content") or []:  # generated text: the real output-token count arrives only at turn end
                m["chars"] += len(b.get("text", "") or "") + len(b.get("thinking", "") or "") + (len(json.dumps(b["input"])) if "input" in b else 0)
            for kk, v in (r.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    m["usage"][kk] = max(m["usage"].get(kk, 0), v)
        for m in msgs.values():
            if not m["usage"]:
                continue
            k = classify(m["blocks"])
            u = dict(m["usage"])
            # streamed per-message usage carries only a sliver of output tokens; estimate from generated chars
            u["output_tokens"] = max(u.get("output_tokens", 0), int(m["chars"] / CHARS_PER_TOKEN))
            calls[k] += 1
            if cfg:
                cost[k] += cfg.prices.cost(u)
            for t in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                toks[k][t] += u.get(t, 0) or 0
    return {"run": run, "agents": len(files), "cost": dict(cost), "calls": dict(calls), "toks": toks,
            "best": best_score(run, not cfg or cfg.gate.higher_is_better if cfg else True, boards_dir)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="run names, each a directory under --runs-dir")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help="the directory holding the runs (default: runs)")
    ap.add_argument("--boards-dir", type=Path, default=BOARDS_DIR,
                    help="the directory holding each run's board.db (default: boards)")
    ap.add_argument("--configs-dir", type=Path, default=CONFIGS_DIR,
                    help="the directory holding each run's <run>.yaml config, for its prices (default: .)")
    ap.add_argument("--calibrate", action="store_true", help="compare estimated vs reported output tokens on agents with turn ends")
    ap.add_argument("--baseline", type=float, default=28.7, help="score of the trivial baseline (snake greedy: 28.7)")
    a = ap.parse_args()
    if a.calibrate:
        for run in a.runs:
            for f in sorted(glob.glob(str(a.runs_dir / run / "private" / "*" / "transcript.jsonl"))):
                chars, rep = 0, 0
                for line in open(f):
                    r = json.loads(line)
                    if r.get("_type") == "AssistantMessage":
                        for b in r.get("content") or []:
                            chars += len(b.get("text", "") or "") + len(b.get("thinking", "") or "") + (len(json.dumps(b["input"])) if "input" in b else 0)
                    elif r.get("_type") == "ResultMessage":
                        rep += (r.get("usage") or {}).get("output_tokens", 0) or 0
                if rep:
                    print(f"{run} {Path(f).parent.name}: chars/token = {chars / rep:.2f}")
        return
    print(f"{'run':9s} {'agents':>6s} {'best':>6s} {'$total':>7s} {'$work':>6s} {'$board':>6s} {'$wait':>6s} "
          f"{'wait%':>5s} {'calls w/b/wt':>14s} {'$/pt':>6s} {'$work/pt':>8s} {'Mtok/pt':>7s} {'Mout':>5s}")
    for run in a.runs:
        r = analyse(run, a.runs_dir, a.boards_dir, a.configs_dir)
        c = r["cost"]
        tot = sum(c.values())
        pts = (r["best"] - a.baseline) if r["best"] is not None else None
        tok_all = sum(sum(v.values()) for v in r["toks"].values())
        out = sum(v.get("output_tokens", 0) for v in r["toks"].values())
        cw = r["calls"]
        per = (lambda x: f"{x / pts:6.3f}" if pts else "   n/a")
        print(f"{run:9s} {r['agents']:6d} {r['best'] or 0:6.2f} {tot:7.2f} {c.get('work', 0):6.2f} {c.get('board', 0):6.2f} "
              f"{c.get('wait', 0):6.2f} {100 * c.get('wait', 0) / tot if tot else 0:4.0f}% "
              f"{cw.get('work', 0):>4d}/{cw.get('board', 0):>3d}/{cw.get('wait', 0):>4d} "
              f"{per(tot)} {(c.get('work', 0) / pts if pts else 0):8.3f} {(tok_all / 1e6 / pts if pts else 0):7.3f} {out / 1e6:5.2f}")


if __name__ == "__main__":
    main()
