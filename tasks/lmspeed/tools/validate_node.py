"""Run a submission through the held-out gate on the node the way lab.py does
(jail prefix, FIFO transport, frozen snapshot), and measure it.

    python tools/validate_node.py SUBMISSION_DIR --harness HARNESS_DIR --root SCRATCH_DIR \
        --cores 100-103 --offset K --tag NAME

Run on the node, from the venv, OUTSIDE the jail. HARNESS_DIR holds jail.py,
tasks/ and secrets/ as the lab provisions them; SCRATCH_DIR takes the frozen
submission and its reports. The venv comes from --venv or
$STIGMERGEIA_NODE_VENV, and the public corpus from $STIGMERGEIA_DATA_ROOT (see
corpus.json). Beyond the gate's own report it records: total wall time, the jail unit's peak memory (sampled),
and — to separate training noise from window noise — the same checkpoint
scored on the FIXED validation windows (`--split train --ckpt`).
One JSON line on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CORPUS = json.loads((HERE / "corpus.json").read_text())
VENV_ENV = "STIGMERGEIA_NODE_VENV"


def _trained_bytes(log: str) -> int | None:
    """The baseline's own count ('done: N steps, M bytes'), if present."""
    import re
    m = re.search(r"done: \d+ steps, (\d+) bytes", log)
    return int(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("submission", type=Path)
    ap.add_argument("--harness", type=Path, required=True, help="the provisioned harness dir (jail.py, tasks/, secrets/)")
    ap.add_argument("--root", type=Path, required=True, help="scratch dir for the frozen submission and its reports")
    ap.add_argument("--venv", default=os.environ.get(VENV_ENV) or None,
                    help=f"the node venv (default: ${VENV_ENV})")
    ap.add_argument("--secret", type=Path)
    ap.add_argument("--cores", default="100-103")
    ap.add_argument("--mem", default="4G")
    ap.add_argument("--offset", type=int, required=True)
    ap.add_argument("--episodes", type=int, default=256)
    ap.add_argument("--timeout", type=int, default=1200, help="jail RuntimeMaxSec (lab: gate.heldout_timeout_s)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--gate-args", default="", help="extra gate args, e.g. '--budget-s 60'")
    a = ap.parse_args()
    if not a.venv:
        ap.error(f"no venv: pass --venv or set {VENV_ENV}")
    root_env = CORPUS["node"]["data_root_env"]
    if not os.environ.get(root_env):
        ap.error(f"no data root: set {root_env} (the corpus is under it, see corpus.json)")
    public_dir = Path(os.environ[root_env]) / CORPUS["node"]["public_dir"]

    py = f"{a.venv}/bin/python"
    task = a.harness / "tasks" / HERE.name
    secret = a.secret or a.harness / "secrets" / "lmspeed-val.secret"
    snap = a.root / "submissions" / "a00" / a.tag
    (a.root / "agents").mkdir(parents=True, exist_ok=True)
    shutil.rmtree(snap, ignore_errors=True)
    shutil.copytree(a.submission, snap, ignore=shutil.ignore_patterns("__pycache__", ".gate*"))
    unit = f"lmspeed-val-{a.tag}"
    prefix = (f"{py} {a.harness}/jail.py --ws {shlex.quote(str(snap))} --ro {a.root}/agents --ro {task} "
              f"--ro {public_dir} --venv {a.venv} --cores {a.cores} --mem {a.mem} --tasks 256 "
              f"--timeout {a.timeout} --torch-ipc copy --unit {unit} --")
    cmd = [py, str(task / "gate.py"), str(snap / "train.py"), "--split", "heldout", "--secret", str(secret),
           "--episodes", str(a.episodes), "--seed-offset", str(a.offset), "--workers", "4",
           "--policy-cmd", prefix, "--fifo-dir", str(snap), "--json", str(snap / ".heldout-a.json"),
           *shlex.split(a.gate_args)]

    peak = {"bytes": 0}
    freqs: list[float] = []
    done = threading.Event()
    cores = sorted({c for part in a.cores.split(",") for c in (
        range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1))})

    def sample() -> None:
        while not done.is_set():
            try:  # the pinned cores' clock: does throughput track it?
                fs = [int(Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq").read_text())
                      for c in cores]
                freqs.append(sum(fs) / len(fs) / 1000)
            except (OSError, ValueError):
                pass
            r = subprocess.run(["systemctl", "--user", "show", "-p", "MemoryCurrent", "--value", unit],
                               capture_output=True, text=True)
            v = r.stdout.strip()
            if v.isdigit():
                peak["bytes"] = max(peak["bytes"], int(v))
            done.wait(2.0)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.time() - t0
    done.set()
    th.join()
    try:
        gate = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        print(json.dumps({"tag": a.tag, "error": "gate output unparseable", "rc": r.returncode,
                          "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]}))
        return 1
    fixed = None
    ckpt = snap / ".gate-ckpt"
    if ckpt.is_dir() and any(ckpt.iterdir()):
        f = subprocess.run(["taskset", "-c", a.cores, py, str(task / "gate.py"), str(snap / "train.py"),
                            "--split", "train", "--ckpt", str(ckpt), "--episodes", str(a.episodes),
                            "--json", str(snap.parent / f"{a.tag}.fixed.json")],
                           capture_output=True, text=True, env={"OMP_NUM_THREADS": "4", "PATH": "/usr/bin:/bin",
                                                             root_env: os.environ[root_env]})
        try:
            fixed = json.loads(f.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            fixed = {"error": f.stdout[-1000:] + f.stderr[-1000:]}
    tr = gate.get("train", {})
    print(json.dumps({
        "tag": a.tag, "rc": r.returncode, "wall_s": round(wall, 1), "peak_mem_mb": round(peak["bytes"] / 2**20),
        "heldout_bpc": gate.get("mean"), "heldout_std": gate.get("std"), "heldout_ci95": gate.get("ci95"),
        "ends": gate.get("ends"), "eval_s": gate.get("eval_seconds"), "seed": gate.get("seed"),
        "mean_freq_mhz": round(sum(freqs) / len(freqs)) if freqs else None,
        "train_bytes": _trained_bytes(tr.get("log_tail") or ""),
        "train_s": tr.get("seconds"), "train_exit": tr.get("exit"), "stopped": tr.get("stopped"),
        "log_tail": (tr.get("log_tail") or "")[-600:], "first_error": gate.get("first_error"),
        "infra_error": gate.get("infra_error"),
        "fixed_valid_bpc": (fixed or {}).get("mean"), "fixed_valid_std": (fixed or {}).get("std"),
        "fixed_error": (fixed or {}).get("error"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
