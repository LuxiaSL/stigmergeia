"""Host a snake policy in its own process, speaking JSON lines on stdio.

The gate runs the environment; this process only ever sees serialized
states, never the environment object, its RNG, or the gate's seeds.

A policy file defines either:
  - `class Policy` with `__init__(self, width, height)` and `act(self, state) -> int`
    (a fresh instance per episode), or
  - a module-level `act(state) -> int`.
`state` is an env.State. Moves: 0=up, 1=right, 2=down, 3=left.

Protocol (one JSON object per line):
  gate -> host  {"reset": {"width": W, "height": H}}   host -> gate {"ok": true}
  gate -> host  {"state": {...State fields...}}         host -> gate {"move": m}
  host -> gate  {"error": "..."} on any policy failure (then keeps serving).
Anything the policy prints goes to stderr, never into the protocol stream.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import State  # noqa: E402


def load(path: str):
    spec = importlib.util.spec_from_file_location("submitted_policy", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(path).resolve().parent))  # let it import siblings
    spec.loader.exec_module(mod)
    if not hasattr(mod, "Policy") and not callable(getattr(mod, "act", None)):
        raise AttributeError("policy file defines neither `Policy` nor `act(state)`")
    return mod


def to_state(d: dict) -> State:
    return State(
        width=d["width"], height=d["height"],
        body=tuple(tuple(c) for c in d["body"]),
        apple=tuple(d["apple"]) if d["apple"] is not None else None,
        heading=d["heading"], step=d["step"], max_steps=d["max_steps"], score=d["score"],
    )


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("policy")
    ap.add_argument("--fifo-in", help="read requests from this FIFO instead of stdin")
    ap.add_argument("--fifo-out", help="write replies to this FIFO instead of stdout")
    a = ap.parse_args()
    if bool(a.fifo_in) != bool(a.fifo_out):
        ap.error("--fifo-in and --fifo-out go together")
    if a.fifo_in:
        # Order matters (see gate.PolicyProcess): write end first, then read end.
        proto = open(a.fifo_out, "w", buffering=1)
        requests = open(a.fifo_in, "r")
    else:
        proto, requests = sys.stdout, sys.stdin
    sys.stdout = sys.stderr  # policy prints must not corrupt the protocol
    reply = lambda obj: (proto.write(json.dumps(obj) + "\n"), proto.flush())  # noqa: E731
    try:
        mod = load(a.policy)
    except Exception:
        reply({"error": "load failed:\n" + traceback.format_exc(limit=5)})
        return 1
    instance = None
    for line in requests:
        msg = json.loads(line)
        try:
            if "reset" in msg:
                r = msg["reset"]
                instance = mod.Policy(r["width"], r["height"]) if hasattr(mod, "Policy") else None
                reply({"ok": True})
            elif "state" in msg:
                st = to_state(msg["state"])
                move = instance.act(st) if instance is not None else mod.act(st)
                reply({"move": int(move)})
            else:
                reply({"error": f"unknown message {sorted(msg)}"})
        except Exception:
            reply({"error": traceback.format_exc(limit=5)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
