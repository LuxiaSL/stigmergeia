"""Baseline trainer: a small byte-level transformer, AdamW, time-based schedule.

The gate runs it as

    python train.py --data DATA_DIR --out OUT_DIR --seed SEED --deadline UNIX_TIME

and evaluates whatever `model.load(OUT_DIR)` returns from the files present at
the deadline. So: save early, save often, save atomically, and finish the
last save before the deadline.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # CPU only: skip torch's CUDA probe (it warns in the jail)

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import ByteLM, Config, n_params, save  # noqa: E402

BATCH = 32
LR = 1e-3
WARMUP_STEPS = 100
SAVE_EVERY_S = 60.0
FINAL_MARGIN_S = 8.0  # stop this long before the deadline: one step plus a save must fit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--deadline", type=float, required=True, help="unix time by which the last save must be done")
    a = ap.parse_args()

    t0 = time.time()
    c = Config()
    torch.manual_seed(a.seed)
    rng = np.random.default_rng(a.seed)
    # into RAM, once (~1 s): random reads through a memmap page-fault on the network
    # filesystem whenever its page cache is cold
    data = torch.from_numpy(np.fromfile(os.path.join(a.data, "enwik8.train"), dtype=np.uint8))
    offs = torch.arange(c.ctx + 1)
    model = ByteLM(c)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, betas=(0.9, 0.95), weight_decay=0.1)
    stop_at = a.deadline - FINAL_MARGIN_S
    print(f"{n_params(model) / 1e6:.2f}M params, {torch.get_num_threads()} threads, "
          f"{stop_at - t0:.0f}s to train", flush=True)

    step, last_save, seen, ema = 0, time.time(), 0, None
    while True:
        now = time.time()
        frac = (now - t0) / max(stop_at - t0, 1e-9)
        if frac >= 1.0:
            break
        # warmup, then cosine to 10% over the remaining WALL time
        lr = LR * min(1.0, (step + 1) / WARMUP_STEPS) * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * frac)))
        for g in opt.param_groups:
            g["lr"] = lr
        ix = torch.from_numpy(rng.integers(0, len(data) - c.ctx - 1, BATCH))
        chunk = data[ix[:, None] + offs].long()
        logits, _ = model(chunk[:, :-1])
        loss = F.cross_entropy(logits.reshape(-1, c.vocab), chunk[:, 1:].reshape(-1))
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        step += 1
        seen += BATCH * c.ctx
        ema = loss.item() if ema is None else 0.98 * ema + 0.02 * loss.item()
        if step % 100 == 0:
            el = time.time() - t0
            print(f"step {step} t {el:.0f}s train_bpc {ema / math.log(2):.3f} lr {lr:.2e} "
                  f"{seen / el:.0f} bytes/s", flush=True)
        if time.time() - last_save > SAVE_EVERY_S:
            save(model, a.out)
            last_save = time.time()
    save(model, a.out)
    print(f"done: {step} steps, {seen} bytes, final save at {a.deadline - time.time():.1f}s before the deadline",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
