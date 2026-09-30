"""The snake gate: score a policy on training or held-out seeds.

  python gate.py <policy>.py [--split train|heldout] [--episodes N]
                 [--secret FILE] [--policy-cmd 'jail.py ... --'] [--json OUT]

- `train` seeds are 0..N-1: public, for agents to iterate against.
- `heldout` seeds derive from a secret file only the gate can read:
  seed_i = sha256(secret || i) for i in [--seed-offset, --seed-offset + N).
  A harness that never reuses an index range (a persisted counter) scores
  every submission on FRESH seeds, so there is no fixed held-out set for
  repeated submissions to overfit. Seeds are never echoed back.
- The policy runs in a child process (policy_host.py) and sees serialized
  states only. `--policy-cmd` prefixes that child's command line, which is
  how the child is put inside the jail.
- Budgets: `--move-timeout` per move, `--episode-cpu` total policy seconds
  per episode. Exceeding either ends the episode as "timeout" with the
  apples eaten so far. A policy error ends it as "error". Nothing is
  silently dropped: every episode is counted, with its end reason.

Output: JSON with mean, std, 95% CI, end-reason counts, per-episode rows.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import os
import json
import math
import select
import shlex
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from env import Result, Snake  # noqa: E402

DEFAULTS = {"width": 10, "height": 10, "max_steps": 1000}  # calibrated: see README


def heldout_seeds(secret_file: Path, n: int, offset: int = 0) -> list[int]:
    secret = secret_file.read_bytes()
    if len(secret) < 16:
        raise ValueError(f"{secret_file}: secret too short (need >= 16 bytes)")
    if offset < 0 or offset + n > 2**32:
        raise ValueError(f"seed index range [{offset}, {offset + n}) is outside [0, 2^32)")
    return [int.from_bytes(hashlib.sha256(secret + i.to_bytes(4, "big")).digest()[:4], "big")
            for i in range(offset, offset + n)]


class PolicyStartError(RuntimeError):
    """The policy process never came up: an infrastructure failure, not a score."""


def _blocking(fd: int) -> int:
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    return fd


class PolicyProcess:
    """A policy_host child and the JSON-lines conversation with it.

    Transport is the child's stdin/stdout, or — with `fifo_dir` — two named
    pipes in that directory. The FIFO form exists for a sandboxed child
    whose launcher does not pass stdin through: the pipes live in the
    child's own workspace, which it may open. Handshake order is fixed so
    that EOF can only ever mean "the child is gone": the gate opens its read
    end first (non-blocking, always succeeds), the child opens its write end
    then its read end, and the gate's write-open succeeds only once the
    child is fully connected."""

    def __init__(self, policy: Path, prefix: list[str], fifo_dir: Path | None = None,
                 connect_timeout: float = 60.0):
        host = [sys.executable, str(HERE / "policy_host.py"), str(policy)]
        self.fifos: list[Path] = []
        if fifo_dir is None:
            self.proc = subprocess.Popen(prefix + host, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
            assert self.proc.stdin and self.proc.stdout
            self.wfile, self.rfile = self.proc.stdin, self.proc.stdout
            return
        fifo_dir.mkdir(parents=True, exist_ok=True)
        to_p, from_p = fifo_dir / ".to_policy", fifo_dir / ".from_policy"
        for p in (to_p, from_p):
            p.unlink(missing_ok=True)
            os.mkfifo(p, 0o600)
        self.fifos = [to_p, from_p]
        rfd = os.open(from_p, os.O_RDONLY | os.O_NONBLOCK)
        self.proc = subprocess.Popen(prefix + host + ["--fifo-in", str(to_p), "--fifo-out", str(from_p)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                wfd = os.open(to_p, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as e:
                if e.errno != errno.ENXIO:  # ENXIO: no reader yet
                    raise
            if self.proc.poll() is not None:
                os.close(rfd)
                raise PolicyStartError(f"policy process exited (code {self.proc.returncode}) before connecting")
            if time.monotonic() > deadline:
                os.close(rfd)
                self.proc.kill()
                raise PolicyStartError(f"policy process did not connect within {connect_timeout:.0f}s")
            time.sleep(0.05)
        self.wfile = os.fdopen(_blocking(wfd), "w", buffering=1)
        self.rfile = os.fdopen(_blocking(rfd), "r")

    def ask(self, msg: dict, timeout: float) -> dict:
        try:
            self.wfile.write(json.dumps(msg) + "\n")
            self.wfile.flush()
        except BrokenPipeError:
            return {"error": "policy process exited"}
        ready, _, _ = select.select([self.rfile], [], [], max(timeout, 0.0))
        if not ready:
            return {"timeout": True}
        line = self.rfile.readline()
        if not line:
            return {"error": f"policy process exited (code {self.proc.poll()})"}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"error": f"malformed reply: {line[:200]!r}"}

    def close(self) -> None:
        try:
            self.wfile.close()
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
            self.proc.wait()
        finally:
            try:
                self.rfile.close()
            except OSError:
                pass
            for p in self.fifos:
                p.unlink(missing_ok=True)


def run_episode(pp: PolicyProcess, seed: int, cfg: dict, move_timeout: float,
                episode_cpu: float) -> Result:
    env = Snake(width=cfg["width"], height=cfg["height"], max_steps=cfg["max_steps"], seed=seed)
    r = pp.ask({"reset": {"width": env.width, "height": env.height}}, move_timeout * 10)
    if "ok" not in r:
        return Result(seed, 0, 0, "timeout" if "timeout" in r else "error", str(r.get("error", ""))[:500])
    spent = 0.0
    while True:
        st = env.state()
        t0 = time.monotonic()
        r = pp.ask({"state": asdict(st)}, min(move_timeout, episode_cpu - spent))
        spent += time.monotonic() - t0
        if "move" not in r:
            end = "timeout" if "timeout" in r else "error"
            return Result(seed, env.score, env.steps, end, str(r.get("error", ""))[:500])
        if spent > episode_cpu:
            return Result(seed, env.score, env.steps, "timeout", "episode policy-time budget")
        try:
            end = env.step(r["move"])
        except ValueError as e:
            return Result(seed, env.score, env.steps, "error", str(e))
        if end:
            return Result(seed, env.score, env.steps, end)


def summarize(results: list[Result], split: str, cfg: dict, policy: Path) -> dict:
    scores = [r.score for r in results]
    n = len(scores)
    mean = statistics.fmean(scores) if n else 0.0
    std = statistics.stdev(scores) if n > 1 else 0.0
    half = 1.96 * std / math.sqrt(n) if n > 1 else float("nan")
    return {
        "policy": str(policy), "split": split, "config": cfg, "episodes": n,
        "mean": round(mean, 3), "std": round(std, 3),
        "ci95": [round(mean - half, 3), round(mean + half, 3)],
        "ends": dict(Counter(r.end for r in results)),
        "mean_steps": round(statistics.fmean(r.steps for r in results), 1) if n else 0,
        "first_error": next((r.detail[:1500] for r in results if r.end == "error"), None),
        "results": [asdict(r) for r in results],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("policy", type=Path)
    ap.add_argument("--split", choices=["train", "heldout"], default="train")
    ap.add_argument("--episodes", type=int, default=100)
    ap.add_argument("--secret", type=Path, help="held-out seed secret (gate-only)")
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="first held-out seed index; give each batch a range never used before")
    ap.add_argument("--policy-cmd", default="", help="command prefix for the policy process")
    ap.add_argument("--fifo-dir", type=Path, help="talk to the policy over FIFOs in this dir, not stdio")
    ap.add_argument("--move-timeout", type=float, default=1.0)
    ap.add_argument("--episode-cpu", type=float, default=20.0)
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument("--workers", type=int, default=1,
                    help="policy processes in parallel (one per reserved core); results keep seed order")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=int, default=v)
    a = ap.parse_args()

    if not a.policy.is_file():
        ap.error(f"no such policy file: {a.policy}")
    if a.split == "heldout":
        if not a.secret:
            ap.error("--split heldout needs --secret")
        seeds = heldout_seeds(a.secret, a.episodes, a.seed_offset)
    else:
        seeds = list(range(a.episodes))
    cfg = {"width": a.width, "height": a.height, "max_steps": a.max_steps}

    n = max(1, min(a.workers, len(seeds)))
    chunks = [seeds[i * len(seeds) // n:(i + 1) * len(seeds) // n] for i in range(n)]
    procs: list[PolicyProcess] = []
    try:
        for k in range(n):
            fifo = (a.fifo_dir / f"w{k}") if a.fifo_dir else None
            procs.append(PolicyProcess(a.policy.resolve(), shlex.split(a.policy_cmd), fifo))
    except PolicyStartError as e:
        for pp in procs:
            pp.close()
        # Infrastructure, not a score: no report, nonzero exit, stated plainly.
        print(json.dumps({"infra_error": str(e), "policy": str(a.policy), "split": a.split}))
        return 3

    def play(pp: PolicyProcess, chunk: list[int]) -> list[Result]:
        out: list[Result] = []
        for seed in chunk:
            res = run_episode(pp, seed, cfg, a.move_timeout, a.episode_cpu)
            if a.split == "heldout":
                res.seed = -1  # held-out seeds are never echoed back
            out.append(res)
            if res.end == "error" and pp.proc.poll() is not None:
                # the host died: count the rest of this chunk, don't pretend it ran
                out += [Result(-1 if a.split == "heldout" else s, 0, 0, "error", "policy process dead")
                        for s in chunk[len(out):]]
                break
        return out

    try:
        from concurrent.futures import ThreadPoolExecutor
        # Threads suffice: the policies compute in their own processes; the
        # gate thread per worker only steps the env and waits on its pipe.
        with ThreadPoolExecutor(max_workers=n) as pool:
            parts = list(pool.map(play, procs, chunks))
        results: list[Result] = [r for part in parts for r in part]
    finally:
        for pp in procs:
            pp.close()
        if a.fifo_dir:
            for k in range(n):
                try:
                    (a.fifo_dir / f"w{k}").rmdir()  # the pipes are gone; drop the empty per-worker dir
                except OSError:
                    pass

    report = summarize(results, a.split, cfg, a.policy)
    if a.split == "heldout":
        report["seed_range"] = [a.seed_offset, a.seed_offset + a.episodes]
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(report, indent=2) + "\n")
    brief = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
