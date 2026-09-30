"""Baseline model: a small byte-level causal transformer.

The gate uses exactly two things from this file:

    load(ckpt_dir: str) -> predictor
    predictor.reset(batch_size: int) -> None
    predictor.step(x) -> log-probs      # x: int64 tensor [B] of bytes, returns [B, 256]

`step` receives ONE new byte per stream and returns the distribution of the
NEXT byte of every stream, given every byte fed since `reset`. It never sees
a byte before it has predicted it: that is what makes the score honest.
Incremental decoding therefore needs a cache; this one keeps per-layer
keys/values and, when the context window fills, re-encodes the most recent
half window (so positions stay inside the trained range).
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class Config:
    vocab: int = 256
    ctx: int = 256
    d: int = 256
    layers: int = 4
    heads: int = 4
    mlp: int = 4


class Block(nn.Module):
    def __init__(self, c: Config):
        super().__init__()
        self.h = c.heads
        self.ln1 = nn.LayerNorm(c.d)
        self.qkv = nn.Linear(c.d, 3 * c.d)
        self.proj = nn.Linear(c.d, c.d)
        self.ln2 = nn.LayerNorm(c.d)
        self.fc = nn.Linear(c.d, c.mlp * c.d)
        self.out = nn.Linear(c.mlp * c.d, c.d)

    def forward(self, x: torch.Tensor, past: tuple[torch.Tensor, torch.Tensor] | None = None):
        B, T, D = x.shape
        q, k, v = self.qkv(self.ln1(x)).split(D, dim=2)
        q, k, v = (t.view(B, T, self.h, D // self.h).transpose(1, 2) for t in (q, k, v))
        if past is not None:
            k = torch.cat([past[0], k], dim=2)
            v = torch.cat([past[1], v], dim=2)
        # with a cache and T == 1 the new query may attend to everything cached
        a = F.scaled_dot_product_attention(q, k, v, is_causal=past is None and T > 1)
        x = x + self.proj(a.transpose(1, 2).reshape(B, T, D))
        x = x + self.out(F.gelu(self.fc(self.ln2(x))))
        return x, (k, v)


class ByteLM(nn.Module):
    def __init__(self, c: Config):
        super().__init__()
        self.c = c
        self.tok = nn.Embedding(c.vocab, c.d)
        self.pos = nn.Embedding(c.ctx, c.d)
        self.blocks = nn.ModuleList(Block(c) for _ in range(c.layers))
        self.ln = nn.LayerNorm(c.d)
        self.head = nn.Linear(c.d, c.vocab, bias=False)

    def forward(self, idx: torch.Tensor, past=None, pos0: int = 0):
        """idx [B, T] -> logits [B, T, vocab], and the new per-layer cache."""
        T = idx.shape[1]
        x = self.tok(idx) + self.pos(torch.arange(pos0, pos0 + T, device=idx.device))
        cache = []
        for i, b in enumerate(self.blocks):
            x, kv = b(x, None if past is None else past[i])
            cache.append(kv)
        return self.head(self.ln(x)), cache


def save(model: ByteLM, out_dir: str) -> None:
    """Atomic: the gate may snapshot the directory at any moment."""
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, ".model.pt.tmp")
    torch.save({"config": asdict(model.c), "state_dict": model.state_dict()}, tmp)
    os.replace(tmp, os.path.join(out_dir, "model.pt"))


class Predictor:
    def __init__(self, model: ByteLM):
        self.m = model.eval()
        self.ctx = model.c.ctx

    def reset(self, batch_size: int) -> None:
        self.hist = torch.zeros(batch_size, 0, dtype=torch.long)
        self.past = None

    @torch.inference_mode()
    def step(self, x: torch.Tensor) -> torch.Tensor:
        x = x.long().view(-1, 1)
        self.hist = torch.cat([self.hist, x], dim=1)[:, -self.ctx:]
        n_cached = 0 if self.past is None else self.past[0][0].shape[2]
        if self.past is None or n_cached + 1 > self.ctx:
            # (re)encode the most recent half window from scratch
            keep = self.hist[:, -(self.ctx // 2):]
            logits, self.past = self.m(keep)
        else:
            logits, self.past = self.m(x, self.past, pos0=n_cached)
        return F.log_softmax(logits[:, -1].float(), dim=-1)


def load(ckpt_dir: str) -> Predictor:
    torch.set_grad_enabled(False)
    blob = torch.load(os.path.join(ckpt_dir, "model.pt"), map_location="cpu", weights_only=True)
    model = ByteLM(Config(**blob["config"]))
    model.load_state_dict(blob["state_dict"])
    return Predictor(model)


def n_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


if __name__ == "__main__":
    m = ByteLM(Config())
    print(f"{n_params(m) / 1e6:.2f}M parameters; ctx {m.c.ctx}; bits for uniform: {math.log2(256):.0f}")
