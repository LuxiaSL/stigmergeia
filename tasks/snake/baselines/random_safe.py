"""Baseline: a uniformly random move among those that don't die this step."""
import random

DELTA = {0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}


class Policy:
    def __init__(self, width, height):
        self.rng = random.Random(0)

    def act(self, s):
        hx, hy = s.body[0]
        blocked = set(s.body[:-1])
        safe = [m for m, (dx, dy) in DELTA.items()
                if 0 <= hx + dx < s.width and 0 <= hy + dy < s.height and (hx + dx, hy + dy) not in blocked]
        return self.rng.choice(safe) if safe else s.heading
