"""Token usage of Codex-backend runs, per agent and per agent-hour, from transcripts.

  uv run python -m stigmergeia.analysis.codex_usage <run> [<run> ...] [--runs-dir runs]

Counts the usage rows the Codex backend writes (uncached input, cached input,
output incl. reasoning) and the rate-limit snapshots the app-server reported
(usedPercent of each window), so a run's share of the plan can be read off.
Transcripts are read from <runs-dir>/<run>/private/<agent>/; the default runs
dir is relative to the working directory, so run it from the repository root.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

RUNS_DIR = Path("runs")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="run names, each a directory under --runs-dir")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help="the directory holding the runs (default: runs)")
    a = ap.parse_args()
    for run in a.runs:
        tot = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
        agent_s, pct = 0.0, []
        for t in sorted((a.runs_dir / run / "private").glob("a*/transcript.jsonl")):
            rows = [json.loads(line) for line in t.open()]
            if not rows:
                continue
            agent_s += rows[-1]["t"] - rows[0]["t"]
            for r in rows:
                if r.get("_type") == "AssistantMessage" and r.get("usage") and str(r.get("message_id", "")).startswith("usage:"):
                    for k in tot:
                        tot[k] += int(r["usage"].get(k) or 0)
                if r.get("_type") == "codex_event" and r.get("method") == "account/rateLimits/updated":
                    p = ((r.get("params") or {}).get("rateLimits") or {}).get("primary") or {}
                    if p.get("usedPercent") is not None:
                        pct.append((r["t"], p["usedPercent"], p.get("windowDurationMins")))
        h = agent_s / 3600 or 1e-9
        inp = tot["input_tokens"] + tot["cache_read_input_tokens"]
        print(f"{run}: {agent_s/60:.1f} agent-min; input {inp:,} ({tot['cache_read_input_tokens']:,} cached), "
              f"output {tot['output_tokens']:,} (reasoning {tot['reasoning_output_tokens']:,}); "
              f"per agent-hour: input {inp/h:,.0f}, uncached {tot['input_tokens']/h:,.0f}, output {tot['output_tokens']/h:,.0f}")
        if pct:
            print(f"  usedPercent {pct[0][1]} -> {pct[-1][1]} (window {pct[-1][2]} min)")


if __name__ == "__main__":
    main()
