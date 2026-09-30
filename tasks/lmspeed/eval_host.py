"""Host a submitted byte-level model in its own process for the gate.

    python eval_host.py SUBMISSION_DIR CKPT_DIR [--fifo-in P --fifo-out P]

Imports SUBMISSION_DIR's `model.py`, calls `model.load(CKPT_DIR)`, then serves
the gate. It only ever receives bytes the model has already predicted: the
gate feeds one byte per stream per step and scores the distribution the
model returned BEFORE that byte was sent. Nothing here sees the gate's
text, its window offsets, or its secret.

Binary protocol, one frame = tag (1 byte) + length (uint32 LE) + payload:
  host -> gate  O (empty)              loaded; or E <utf-8 traceback> on load failure
  gate -> host  R <uint32 B>           start B fresh streams   -> O (empty)
  gate -> host  S <B bytes>            one byte per stream     -> O <B*256 float32 LE> log-probs
  host -> gate  E <utf-8 text>         any failure (the host keeps serving)
Anything the model prints goes to stderr, never into the protocol stream.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import sys
import traceback
from pathlib import Path

VOCAB = 256
HDR = struct.Struct("<cI")


def read_exact(f, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def load_module(sub: Path):
    path = sub / "model.py"
    spec = importlib.util.spec_from_file_location("submitted_model", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(sub))  # let it import siblings
    sys.modules[spec.name] = mod  # dataclasses and pickling look the module up by name
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "load", None)):
        raise AttributeError("model.py defines no `load(ckpt_dir)`")
    return mod


def to_rows(out, b: int):
    """Whatever step() returned -> contiguous float32 [B, 256] bytes."""
    import numpy as np
    if hasattr(out, "detach"):  # a torch tensor
        out = out.detach().to("cpu").float().numpy()
    arr = np.ascontiguousarray(np.asarray(out, dtype=np.float32))
    if arr.shape != (b, VOCAB):
        raise ValueError(f"step() returned shape {arr.shape}, expected ({b}, {VOCAB})")
    return arr.tobytes()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("submission", type=Path)
    ap.add_argument("ckpt", type=Path)
    ap.add_argument("--fifo-in")
    ap.add_argument("--fifo-out")
    a = ap.parse_args()
    if bool(a.fifo_in) != bool(a.fifo_out):
        ap.error("--fifo-in and --fifo-out go together")
    if a.fifo_in:
        # Order matters (see gate.EvalProcess): write end first, then read end.
        proto = open(a.fifo_out, "wb", buffering=0)
        requests = open(a.fifo_in, "rb", buffering=0)
    else:
        proto, requests = sys.stdout.buffer, sys.stdin.buffer
    sys.stdout = sys.stderr  # model prints must not corrupt the protocol
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # CPU only: skip torch's CUDA probe

    def reply(tag: bytes, payload: bytes = b"") -> None:
        proto.write(HDR.pack(tag, len(payload)) + payload)
        proto.flush()

    try:
        mod = load_module(a.submission.resolve())
        pred = mod.load(str(a.ckpt))
        try:
            import torch
            torch.set_grad_enabled(False)
        except ImportError:
            pass
    except BaseException:
        reply(b"E", ("load failed:\n" + traceback.format_exc(limit=8)).encode()[-4000:])
        return 1
    reply(b"O")

    b = 0
    while True:
        hdr = read_exact(requests, HDR.size)
        if hdr is None:
            return 0
        tag, n = HDR.unpack(hdr)
        payload = read_exact(requests, n) if n else b""
        if payload is None:
            return 0
        try:
            if tag == b"R":
                (b,) = struct.unpack("<I", payload)
                pred.reset(b)
                reply(b"O")
            elif tag == b"S":
                if len(payload) != b:
                    raise ValueError(f"step with {len(payload)} bytes for {b} streams")
                try:
                    import torch
                    x = torch.frombuffer(bytearray(payload), dtype=torch.uint8).long()
                except ImportError:
                    import numpy as np
                    x = np.frombuffer(payload, dtype=np.uint8).astype(np.int64)
                reply(b"O", to_rows(pred.step(x), b))
            else:
                reply(b"E", f"unknown message {tag!r}".encode())
        except BaseException:
            reply(b"E", traceback.format_exc(limit=8).encode()[-4000:])


if __name__ == "__main__":
    sys.exit(main())
