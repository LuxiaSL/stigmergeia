"""The lmspeed gate: train a submission under a fixed budget, then measure
bits per byte on text windows its model has never seen.

  python gate.py <dir>/train.py [--split train|heldout] [--episodes N] [--seed S]
                 [--secret FILE --seed-offset K] [--budget-s 600] [--ckpt DIR]
                 [--policy-cmd 'jail.py ... --'] [--fifo-dir DIR] [--json OUT]

One evaluation = one training run + one scoring pass:

1. **Train.** `python <dir>/train.py --data DATA --out OUT --seed SEED --deadline T`
   runs with the budget as a WALL-CLOCK limit, T = launch + budget. At T the
   gate snapshots OUT (held-out: the whole workspace) and the run is killed
   at T + grace by a `timeout` wrapper inside the jail, whose exit ends the
   jail's unit and every process in it. The snapshot taken AT T is what gets
   scored: files written after the deadline are discarded.
2. **Score.** `<dir>/model.py` is loaded in a separate (jailed) process:
   `load(OUT)` returns a predictor with `reset(B)` and `step(x[B]) ->
   log-probs[B, 256]`. The gate feeds each of B text windows one byte at a
   time and scores the distribution returned BEFORE the next byte is sent,
   so a model can only ever predict from the past. Score = mean over windows
   of -log2 p(next byte), in bits per byte (lower is better; uniform = 8).

Splits:
- `train`: fixed windows of the public validation file (enwik8.valid), for
  iterating. `--ckpt DIR` scores an existing checkpoint without training.
- `heldout`: windows of the private test split, which no jail can read. Their
  positions derive from a secret: key = sha256(secret || offset || n). A
  harness that never reuses an offset (a persisted counter) scores every
  evaluation on freshly drawn windows and with a fresh training seed, so
  there is no fixed held-out set to overfit. Positions are never echoed.

Data: the public directory and the held-out file default to the paths
corpus.json gives under $STIGMERGEIA_DATA_ROOT (the directory prepare_data.sh
fills is <data root>/lmspeed-data). Without that variable, --data-dir (and,
for held-out, --heldout-file) must be given.

Failures: a model error or an evaluation over its time budget scores every
unscored byte at 8 bits (uniform), and is counted by its end reason. A model
process that never connects, or a jail that never starts, is infrastructure:
no score, exit 3, `infra_error` in the JSON.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import random
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CORPUS = json.loads((HERE / "corpus.json").read_text())
VOCAB = 256
UNIFORM_BITS = math.log2(VOCAB)  # 8.0: what an unscored or invalid prediction costs
MAX_BITS = 32.0  # one byte given ~zero probability costs this much, not infinity
HDR = struct.Struct("<cI")
LN2 = math.log(2)
# Calibrated on 4 pinned cores of a Xeon 6767P: see README "The rules".
DEFAULTS = {"budget_s": 600.0, "grace_s": 15.0, "window": 1024, "gap": 64, "streams": 128,
            "eval_s": 300.0, "load_s": 120.0, "step_timeout": 60.0}
NEVER_COPY = {".tmp", ".shm", ".to_policy", ".from_policy"}


def corpus_path(key: str) -> Path | None:
    """corpus.json's node path `key` under the data root named by the
    environment, or None when that variable is unset or empty."""
    root = os.environ.get(CORPUS["node"]["data_root_env"])
    return Path(root) / CORPUS["node"][key] if root else None


class InfraError(RuntimeError):
    """The model or training process never came up: infrastructure, not a score."""


# ------------------------------------------------------------------ windows


def window_starts(n_bytes: int, n: int, key: bytes, window: int, gap: int) -> list[int]:
    """n distinct, non-overlapping windows of window+1 bytes, drawn by `key`.
    A random shift then a random choice of slots on a grid of stride
    window+1+gap: windows in one evaluation never overlap or touch (a stream
    could otherwise read another stream's future), and two keys almost never
    produce the same window."""
    stride = window + 1 + gap
    rng = random.Random(int.from_bytes(hashlib.sha256(key).digest(), "big"))
    shift = rng.randrange(stride)
    slots = (n_bytes - shift - (window + 1)) // stride + 1
    if n > slots:
        raise ValueError(f"{n} windows of {window + 1} bytes do not fit in {n_bytes} bytes (max {slots})")
    return [shift + s * stride for s in rng.sample(range(slots), n)]


def heldout_key(secret: bytes, offset: int, n: int, what: bytes) -> bytes:
    if len(secret) < 16:
        raise ValueError("secret too short (need >= 16 bytes)")
    if offset < 0 or offset + n > 2**48:
        raise ValueError(f"index range [{offset}, {offset + n}) is outside [0, 2^48)")
    return secret + what + offset.to_bytes(8, "big") + n.to_bytes(4, "big")


def heldout_seed(secret: bytes, offset: int, n: int) -> int:
    return int.from_bytes(hashlib.sha256(heldout_key(secret, offset, n, b"train-seed")).digest()[:4], "big") & 0x7FFFFFFF


# ------------------------------------------------------------- the model host


def _blocking(fd: int) -> int:
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    return fd


class EvalProcess:
    """An eval_host child and the framed binary conversation with it.

    Transport is the child's stdin/stdout, or — with `fifo_dir` — two named
    pipes there, for a jailed child whose launcher does not pass stdin
    through. Handshake order (as in the snake gate): the gate opens its read
    end first (non-blocking), the child opens its write end then its read
    end, and the gate's write-open succeeds only once the child is fully
    connected. EOF can then only mean the child is gone."""

    def __init__(self, sub: Path, ckpt: Path, prefix: list[str], fifo_dir: Path | None,
                 connect_timeout: float):
        host = [sys.executable, str(HERE / "eval_host.py"), str(sub), str(ckpt)]
        self.fifos: list[Path] = []
        if fifo_dir is None:
            self.proc = subprocess.Popen(prefix + host, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
            assert self.proc.stdin and self.proc.stdout
            self.wfd, self.rfd = self.proc.stdin.fileno(), self.proc.stdout.fileno()
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
                raise InfraError(f"model process exited (code {self.proc.returncode}) before connecting")
            if time.monotonic() > deadline:
                os.close(rfd)
                self.proc.kill()
                raise InfraError(f"model process did not connect within {connect_timeout:.0f}s")
            time.sleep(0.05)
        self.wfd, self.rfd = _blocking(wfd), _blocking(rfd)

    def _read(self, n: int, deadline: float) -> bytes | str:
        buf = bytearray()
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout"
            ready, _, _ = select.select([self.rfd], [], [], left)
            if not ready:
                return "timeout"
            chunk = os.read(self.rfd, n - len(buf))
            if not chunk:
                return "dead"
            buf += chunk
        return bytes(buf)

    def recv(self, timeout: float) -> tuple[str, bytes]:
        """('O'|'E', payload), or ('timeout'|'dead', b'')."""
        deadline = time.monotonic() + max(timeout, 0.0)
        hdr = self._read(HDR.size, deadline)
        if isinstance(hdr, str):
            return hdr, b""
        tag, n = HDR.unpack(hdr)
        body = self._read(n, deadline) if n else b""
        if isinstance(body, str):
            return body, b""
        return tag.decode(errors="replace"), body

    def request(self, tag: bytes, payload: bytes, timeout: float) -> tuple[str, bytes]:
        try:
            os.write(self.wfd, HDR.pack(tag, len(payload)) + payload)
        except (BrokenPipeError, OSError):
            return "dead", b""
        return self.recv(timeout)

    def close(self) -> None:
        for fd in (self.wfd, self.rfd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for p in self.fifos:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ training


def _ignore_special(d: str, names: list[str]) -> list[str]:
    """copytree must never open a FIFO or socket (it would block or fail)."""
    out = []
    for n in names:
        if n in NEVER_COPY:
            out.append(n)
            continue
        try:
            st = os.lstat(os.path.join(d, n))
        except OSError:
            out.append(n)
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            out.append(n)
    return out


def freeze(src: Path, dst: Path) -> None:
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True, ignore=_ignore_special)


def restore(frozen: Path, dst: Path) -> None:
    """Make dst exactly the frozen copy (keeping the jail's own .tmp/.shm)."""
    dst.mkdir(parents=True, exist_ok=True)
    for child in dst.iterdir():
        if child.name in NEVER_COPY:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    shutil.copytree(frozen, dst, symlinks=True, dirs_exist_ok=True)
    shutil.rmtree(frozen, ignore_errors=True)


def train(train_py: Path, prefix: list[str], data_dir: Path, out: Path, seed: int, budget: float,
          grace: float, log_path: Path, freeze_root: Path | None) -> dict:
    """Run train.py under the budget. Returns what happened; the checkpoint is
    whatever `out` held at the deadline (or at exit, if it ended earlier)."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    t0 = time.time()
    deadline = t0 + budget
    cmd = prefix + ["timeout", "-k", "5", f"{budget + grace:.0f}", sys.executable, str(train_py),
                    "--data", str(data_dir), "--out", str(out), "--seed", str(seed),
                    "--deadline", f"{deadline:.3f}"]
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=train_py.parent, start_new_session=True)
    info: dict = {"budget_s": budget, "seed": seed, "stopped": True}
    try:
        rc = proc.wait(timeout=budget)
        info["ended_before_deadline"] = True
    except subprocess.TimeoutExpired:
        rc = None
        info["ended_before_deadline"] = False
        # THE deadline: whatever exists now is what gets scored.
        src = freeze_root if freeze_root is not None else out
        frozen = src.parent / (src.name + ".frozen")
        freeze(src, frozen)
        try:
            rc = proc.wait(timeout=grace + 30)
        except subprocess.TimeoutExpired:
            # timeout(1) inside the jail should have ended it. Something in
            # there outlived it: kill what we can see, and score nothing
            # (files it touches from here on must not reach the model).
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rc = None
            info["stopped"] = False
        if info["stopped"]:
            restore(frozen, src)
        else:
            shutil.rmtree(frozen, ignore_errors=True)
    info["exit"] = rc
    info["seconds"] = round(time.time() - t0, 1)
    info["ckpt_files"] = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
    return info


def log_tail(path: Path, n: int = 3000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-n:].decode(errors="replace")


# ------------------------------------------------------------------- scoring


def score_rows(logits: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, int]:
    """bits for each row's target byte, and how many rows were invalid.
    The gate normalizes: rows may be logits or log-probs. A row with NaN or
    +inf, or no finite value, is invalid and costs 8 bits (uniform)."""
    lg = logits.astype(np.float64)
    ar = np.arange(lg.shape[0])
    with np.errstate(all="ignore"):
        m = lg.max(axis=1)
        lse = m + np.log(np.exp(lg - m[:, None]).sum(axis=1))
        lt = lg[ar, target]
        bits = (lse - lt) / LN2
    invalid = ~np.isfinite(lse) | np.isnan(lt) | (lt == np.inf)
    zero_p = ~invalid & (lt == -np.inf)
    bits = np.where(zero_p, MAX_BITS, bits)
    bits = np.where(invalid, UNIFORM_BITS, bits)
    return np.clip(bits, 0.0, MAX_BITS), int(invalid.sum())


def evaluate(ep: EvalProcess, text: np.ndarray, starts: list[int], a: argparse.Namespace) -> dict:
    n, W = len(starts), a.window
    bits = np.full((n, W), UNIFORM_BITS)
    scored = np.zeros(n, dtype=np.int64)
    ends = ["ok"] * n
    first_error: str | None = None
    invalid = 0
    t_end = time.monotonic() + a.eval_s
    t0 = time.monotonic()

    def fail(rows: range, kind: str, detail: str) -> None:
        nonlocal first_error
        for i in rows:
            ends[i] = kind
        if first_error is None:
            first_error = detail[:3000]

    tag, body = ep.recv(a.load_s)  # the host's hello: load() succeeded
    if tag != "O":
        fail(range(n), "error" if tag in ("E", "dead") else "timeout",
             body.decode(errors="replace") if tag == "E" else f"model load: {tag}")
        return {"bits": bits, "scored": scored, "ends": ends, "first_error": first_error, "invalid_rows": 0,
                "eval_seconds": round(time.monotonic() - t0, 1)}
    for g0 in range(0, n, a.streams):
        g = range(g0, min(n, g0 + a.streams))
        if first_error is not None:
            fail(g, ends[g0 - 1], "")  # after a failure nothing more is asked: same end reason
            continue
        arr = np.stack([text[s:s + W + 1] for s in starts[g.start:g.stop]])
        B = len(g)
        tag, body = ep.request(b"R", struct.pack("<I", B), a.step_timeout)
        if tag != "O":
            fail(g, "timeout" if tag == "timeout" else "error",
                 f"reset({B}): " + (body.decode(errors="replace") if tag == "E" else tag))
            continue
        for t in range(W):
            left = t_end - time.monotonic()
            if left <= 0:
                fail(g, "timeout", f"evaluation exceeded its {a.eval_s:.0f}s budget at byte {t} of {W}")
                break
            tag, body = ep.request(b"S", arr[:, t].tobytes(), min(a.step_timeout, left))
            if tag != "O" or len(body) != B * VOCAB * 4:
                why = (body.decode(errors="replace") if tag == "E" else
                       f"step: {tag}" if tag != "O" else f"step: reply of {len(body)} bytes, expected {B * VOCAB * 4}")
                fail(g, "timeout" if tag == "timeout" else "error", f"at byte {t} of {W}: {why}")
                break
            b, bad = score_rows(np.frombuffer(body, dtype=np.float32).reshape(B, VOCAB), arr[:, t + 1])
            bits[g.start:g.stop, t] = b
            scored[g.start:g.stop] = t + 1
            invalid += bad
    return {"bits": bits, "scored": scored, "ends": ends, "first_error": first_error, "invalid_rows": invalid,
            "eval_seconds": round(time.monotonic() - t0, 1)}


def summarize(ev: dict, a: argparse.Namespace, starts: list[int]) -> dict:
    per = ev["bits"].mean(axis=1)
    n = len(per)
    mean = float(per.mean()) if n else UNIFORM_BITS
    std = float(per.std(ddof=1)) if n > 1 else 0.0
    half = 1.96 * std / math.sqrt(n) if n > 1 else float("nan")
    heldout = a.split == "heldout"
    return {
        "policy": str(a.policy), "split": a.split,
        "config": {k: getattr(a, k) for k in DEFAULTS},
        "episodes": n, "chars": int(n * a.window),
        "mean": round(mean, 4), "score": round(mean, 4), "lower_is_better": True, "unit": "bits per byte",
        "std": round(std, 4), "ci95": [round(mean - half, 4), round(mean + half, 4)],
        "ends": dict(Counter(ev["ends"])),
        "mean_steps": round(float(ev["scored"].mean()), 1) if n else 0,
        "first_error": ev["first_error"], "invalid_rows": ev["invalid_rows"],
        "eval_seconds": ev["eval_seconds"],
        "results": [{"window": -1 if heldout else s, "bpc": round(float(p), 4), "scored": int(k), "end": e}
                    for s, p, k, e in zip(starts, per, ev["scored"], ev["ends"])],
    }


def failed_everything(n: int, why: str, a: argparse.Namespace, starts: list[int]) -> dict:
    ev = {"bits": np.full((n, a.window), UNIFORM_BITS), "scored": np.zeros(n, dtype=np.int64),
          "ends": ["error"] * n, "first_error": why[-3000:], "invalid_rows": 0, "eval_seconds": 0.0}
    return summarize(ev, a, starts)


# ---------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("policy", type=Path, help="the submission's train.py; model.py sits beside it")
    ap.add_argument("--split", choices=["train", "heldout"], default="train")
    ap.add_argument("--episodes", type=int, default=256, help="text windows to score")
    ap.add_argument("--seed", type=int, default=0, help="training seed (train split; held-out derives its own)")
    ap.add_argument("--secret", type=Path, help="held-out secret (gate-only)")
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="held-out index offset; give each evaluation a range never used before")
    ap.add_argument("--data-dir", type=Path, default=corpus_path("public_dir"),
                    help="public data dir passed to train.py (enwik8.train, enwik8.valid); "
                         "default: under $STIGMERGEIA_DATA_ROOT")
    ap.add_argument("--heldout-file", type=Path, default=corpus_path("heldout_file"),
                    help="the private test split; default: under $STIGMERGEIA_DATA_ROOT")
    ap.add_argument("--ckpt", type=Path, help="train split only: score this checkpoint dir, skip training")
    ap.add_argument("--policy-cmd", default="", help="command prefix (the jail) for training and the model")
    ap.add_argument("--fifo-dir", type=Path, help="talk to the model over FIFOs in this dir, not stdio")
    ap.add_argument("--freeze-root", type=Path,
                    help="held-out: the tree snapshotted at the deadline (default: --fifo-dir, else train.py's dir)")
    ap.add_argument("--workers", type=int, default=1, help="accepted for harness compatibility; unused")
    ap.add_argument("--json", type=Path, help="write the full report here")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    a = ap.parse_args()

    if not a.policy.is_file():
        ap.error(f"no such file: {a.policy}")
    sub = a.policy.resolve().parent
    if not (sub / "model.py").is_file():
        ap.error(f"no model.py beside {a.policy}")
    heldout = a.split == "heldout"
    root_env = CORPUS["node"]["data_root_env"]
    if a.data_dir is None:
        ap.error(f"no data directory: set {root_env} (the corpus is under it, see corpus.json) or pass --data-dir")
    if heldout and a.heldout_file is None:
        ap.error(f"no held-out file: set {root_env} (the corpus is under it, see corpus.json) or pass --heldout-file")
    if heldout:
        if not a.secret:
            ap.error("--split heldout needs --secret")
        if a.ckpt:
            ap.error("--ckpt is for the train split: a held-out score always trains from scratch")
        secret = a.secret.read_bytes()
        text = np.fromfile(a.heldout_file, dtype=np.uint8)
        starts = window_starts(len(text), a.episodes, heldout_key(secret, a.seed_offset, a.episodes, b"windows"),
                               a.window, a.gap)
        seed = heldout_seed(secret, a.seed_offset, a.episodes)
    else:
        text = np.fromfile(a.data_dir / "enwik8.valid", dtype=np.uint8)
        starts = window_starts(len(text), a.episodes, b"lmspeed-valid" + a.seed_offset.to_bytes(8, "big"),
                               a.window, a.gap)
        seed = a.seed
    prefix = shlex.split(a.policy_cmd)
    base = a.json if a.json else sub / ".gate"
    freeze_root = (a.freeze_root or a.fifo_dir or sub).resolve() if heldout else None
    if freeze_root is not None:
        # beside the workspace, not in it: the jailed run cannot write there
        log_path = freeze_root.parent / f"{freeze_root.name}{base.name}.train.log"
    else:
        log_path = base.parent / (base.name + ".train.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    report_extra: dict = {"seed": seed}
    if a.ckpt:
        out = a.ckpt.resolve()
        report_extra["train"] = {"skipped": True, "ckpt": str(out)}
    else:
        out = sub / ".gate-ckpt"
        tr = train(a.policy.resolve(), prefix, a.data_dir, out, seed, a.budget_s, a.grace_s, log_path, freeze_root)
        tail = log_tail(log_path)
        tr["log_tail"] = tail
        report_extra["train"] = tr
        if tr["exit"] in (126, 127) and "jail:" in tail:
            print(json.dumps({"infra_error": f"training never started: {tail[-500:]}", "policy": str(a.policy),
                              "split": a.split}))
            return 3

    if not a.ckpt and not report_extra["train"]["stopped"]:
        report = failed_everything(len(starts), "training did not stop at the deadline (it outlived its timeout); "
                                   "nothing was scored", a, starts)
    elif not out.is_dir() or not any(out.iterdir()):
        why = f"no checkpoint in the out dir at the deadline ({report_extra['train'].get('seconds', 0)}s)"
        report = failed_everything(len(starts), why + "\n--- train log tail ---\n" + log_tail(log_path), a, starts)
    else:
        try:
            ep = EvalProcess(sub, out, prefix, a.fifo_dir, connect_timeout=a.load_s)
        except InfraError as e:
            print(json.dumps({"infra_error": str(e), "policy": str(a.policy), "split": a.split}))
            return 3
        try:
            report = summarize(evaluate(ep, text, starts, a), a, starts)
        finally:
            ep.close()
    report.update(report_extra)
    if heldout:
        report["seed_range"] = [a.seed_offset, a.seed_offset + a.episodes]
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(report, indent=2) + "\n")
    brief = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
