"""Idle time per 5-minute window for swarm runs, counting ALL FOUR kinds of idle.

  uv run python -m stigmergeia.analysis.idle <run> [<run> ...] [--runs-dir runs] [--window 300] [--blocked-min 3]

- waits: the harness's idle waits between turns (harness_idle_wait rows)
- sleeps: foreground shell `sleep N` (N >= 30) inside a turn, timed from the
  call to its result. Agents waiting on their own slow evaluations sleep
  inside turns, so a run can look almost never idle on waits alone and
  still spend a large share of its agent-time asleep.
- blocked: a lab call that kept the agent waiting for its result at least
  --blocked-min minutes, counted in full, split by tool: blocked-run (run and
  score, e.g. a long parameter sweep), blocked-submit (one synchronous submit
  of a slow policy can hold an agent for most of a run) and blocked-other
  (propose/answer).
- lab-wait: time inside the lab's `wait` tool (waiting on the agent's own
  background job, or for board news addressed to it), counted in full at any
  length. Its own category, apart from sleeps: `wait` returns the moment the
  job finishes or someone addresses the agent, where a `sleep` overshoots and
  hears nothing.
Background compute is reported beside idle, NOT as idle: lab jobs running in
the background (runs, held-out submissions, confirmation batches) while the
agent is free, from the lab's own private/<agent>/lab-jobs.jsonl.
Reads transcripts from <runs-dir>/<run>/private/<agent>/ or the older layout
<runs-dir>/<run>/agents/<agent>/private/. A live run is measured up to now.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path


KINDS = ("waits", "sleeps", "blocked-run", "blocked-submit", "blocked-other", "lab-wait")
BLOCKED_TOOL = {"run": 2, "score": 2, "submit": 3}  # index into KINDS; anything else is blocked-other
LAB_WAIT = 5  # the lab's `wait`: counted in full, whatever its length


RUNS_DIR = Path("runs")  # relative to the working directory: run from the repository root


def _private(run: str, a: str, name: str, runs_dir: Path = RUNS_DIR) -> Path | None:
    for p in (runs_dir / run / "private" / a / name, runs_dir / run / "agents" / a / "private" / name):
        if p.exists():
            return p
    return None


def _jsonl(p: Path) -> list[dict]:
    out = []
    for line in p.open():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line still being written
    return out


def transcript(run: str, a: str, runs_dir: Path = RUNS_DIR) -> list[dict] | None:
    p = _private(run, a, "transcript.jsonl", runs_dir)
    return _jsonl(p) if p else None


def background(run: str, a: str, end: float, runs_dir: Path = RUNS_DIR) -> list[tuple[float, float]]:
    """Lab jobs that ran in the background: every finished one from its own
    took_s, and any started one with no finish yet (a live run) up to `end`."""
    p = _private(run, a, "lab-jobs.jsonl", runs_dir)
    if p is None:
        return []
    out, started, done = [], {}, set()
    for r in _jsonl(p):
        ev = r.get("event")
        if ev == "finished" and r.get("background") and r.get("took_s") is not None:
            out.append((r["t"] - float(r["took_s"]), r["t"]))
            done.add(r.get("job"))
        elif ev in ("submit", "run", "job") and r.get("job") and (ev != "submit" or r.get("background")):
            started[r["job"]] = r["t"]
    out += [(t0, end) for j, t0 in started.items() if j not in done]
    return out


Iv = list[tuple[float, float]]


def intervals(rows: list[dict], end: float, blocked_s: float = 180.0) -> tuple[Iv, Iv, Iv, Iv, Iv, Iv]:
    """(waits, sleeps, blocked-run, blocked-submit, blocked-other, lab-wait) intervals of one agent's transcript."""
    waits, sleeps, open_sleeps, open_lab = [], [], {}, {}
    blocked: dict[int, Iv] = {2: [], 3: [], 4: [], LAB_WAIT: []}
    for j, r in enumerate(rows):
        kind = r.get("_type")
        if kind == "harness_idle_wait":
            nxt = next((x["t"] for x in rows[j + 1:] if x.get("_type") != "harness_idle_wait"), end)
            waits.append((r["t"], nxt))
        elif kind == "AssistantMessage":
            for b in r.get("content") or []:
                if not isinstance(b, dict):
                    continue
                name = str(b.get("name", ""))
                if name.startswith("mcp__lab__"):
                    tool = name.removeprefix("mcp__lab__")
                    open_lab[b.get("id")] = (r["t"], LAB_WAIT if tool == "wait" else BLOCKED_TOOL.get(tool, 4))
                if name == "Bash" and not (b.get("input") or {}).get("run_in_background"):
                    n = [int(x) for x in re.findall(r"\bsleep\s+(\d+)", str(b["input"].get("command", "")))]
                    if n and max(n) >= 30:
                        open_sleeps[b.get("id")] = r["t"]
        elif kind == "UserMessage" and isinstance(r.get("content"), list):
            for b in r["content"]:
                if not isinstance(b, dict):
                    continue
                if b.get("tool_use_id") in open_sleeps:
                    sleeps.append((open_sleeps.pop(b["tool_use_id"]), r["t"]))
                if b.get("tool_use_id") in open_lab:
                    s0, k = open_lab.pop(b["tool_use_id"])
                    if k == LAB_WAIT or r["t"] - s0 >= blocked_s:
                        blocked[k].append((s0, r["t"]))
    sleeps += [(s, end) for s in open_sleeps.values()]
    for s0, k in open_lab.values():
        if k == LAB_WAIT or end - s0 >= blocked_s:
            blocked[k].append((s0, end))
    return waits, sleeps, blocked[2], blocked[3], blocked[4], blocked[LAB_WAIT]


def overlap(iv: list[tuple[float, float]], a: float, b: float) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in iv)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="run names, each a directory under --runs-dir")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help="the directory holding the runs (default: runs)")
    ap.add_argument("--window", type=int, default=300)
    ap.add_argument("--blocked-min", type=float, default=3.0,
                    help="a lab call counts as blocked time when its result took at least this long")
    a = ap.parse_args()
    now = time.time()
    for run in a.runs:
        rows = {}
        for i in range(500):
            t = transcript(run, f"a{i:02d}", a.runs_dir)
            if t is None:
                break
            rows[f"a{i:02d}"] = t
        if not rows:
            print(f"{run}: no transcripts")
            continue
        live = not any(r.get("_type") == "agent_end" for t in rows.values() for r in t)
        start = min(t[0]["t"] for t in rows.values())
        end = now if live else max(t[-1]["t"] for t in rows.values())
        iv = {k: intervals(t, end, a.blocked_min * 60) for k, t in rows.items()}
        bg = {k: background(run, k, end, a.runs_dir) for k in rows}
        n, H = len(rows), end - start
        agent_min = n * H / 60
        tot_k = [sum(overlap(v[i], start, end) for v in iv.values()) / 60 for i in range(len(KINDS))]
        tot = sum(tot_k)
        tbg = sum(overlap(v, start, end) for v in bg.values()) / 60
        print(f"===== {run}: {n} agents, {H/60:.0f} min{' (live)' if live else ''}: "
              + " + ".join(f"{k} {x:.0f}" for k, x in zip(KINDS, tot_k))
              + f" = {tot:.0f} of {agent_min:.0f} agent-min ({100*tot/agent_min:.0f}% idle; blocked = lab calls >= "
              f"{a.blocked_min:g} min; lab-wait = time inside the lab's wait, any length). Background compute (not idle): {tbg:.0f} agent-min in {sum(map(len, bg.values()))} "
              f"jobs ({100*tbg/agent_min:.0f}% of agent-time)")
        print("  per agent " + "+".join(KINDS) + " | background (min):",
              {k: "+".join(f"{overlap(x, start, end)/60:.0f}" for x in v) + f" | {overlap(bg[k], start, end)/60:.0f}"
               for k, v in iv.items()})
        for w0 in range(0, int(H), a.window):
            x, y = start + w0, start + min(w0 + a.window, H)
            wt, st, br, bs, bo, lw = (sum(overlap(v[i], x, y) for v in iv.values()) / 60 for i in range(len(KINDS)))
            gt = sum(overlap(v, x, y) for v in bg.values()) / 60
            print(f"   {w0//60:3d}-{min(w0+a.window, H)//60:3.0f}  waits {wt:5.1f}  sleeps {st:5.1f}  "
                  f"blk-run {br:5.1f}  blk-sub {bs:5.1f}  blk-oth {bo:5.1f}  lab-wait {lw:5.1f}  "
                  f"total {wt+st+br+bs+bo+lw:5.1f}/{n*(y-x)/60:.0f}"
                  f"  | bg {gt:5.1f}  " + "#" * round(wt) + "~" * round(st) + "=" * round(br) + "%" * round(bs)
                  + "-" * round(bo) + "." * round(lw))


if __name__ == "__main__":
    main()
