"""Snake, deterministic and seedable. Stdlib only.

The episode is a pure function of (seed, the policy's moves): apple
placement draws from a `random.Random(seed)` that only the environment
touches, so two runs of the same policy on the same seed are identical and
a score can be replicated exactly.

Rules
- Grid `width` x `height`; the snake starts length 3 in the middle, heading
  right.
- Each step the policy returns a move: 0=up, 1=right, 2=down, 3=left.
  Reversing into the neck is treated as "keep going straight".
- Eating an apple grows the snake by one; the next apple appears on a
  uniformly random free cell.
- The episode ends on a wall or self collision, when the board is full, or
  when `max_steps` is reached. Score = apples eaten.

Moving into the cell the tail is leaving this step is legal (the tail moves
out first), except on the step the snake eats, when the tail stays put.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

UP, RIGHT, DOWN, LEFT = 0, 1, 2, 3
DELTA = {UP: (0, -1), RIGHT: (1, 0), DOWN: (0, 1), LEFT: (-1, 0)}
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

Cell = tuple[int, int]


@dataclass(frozen=True)
class State:
    """What a policy sees each step. `body[0]` is the head."""

    width: int
    height: int
    body: tuple[Cell, ...]
    apple: Cell | None
    heading: int
    step: int
    max_steps: int
    score: int


@dataclass
class Result:
    seed: int
    score: int
    steps: int
    end: str  # "wall" | "self" | "full" | "budget" | "error" | "timeout"
    detail: str = ""


@dataclass
class Snake:
    width: int = 10
    height: int = 10
    max_steps: int = 1000
    seed: int = 0
    body: list[Cell] = field(init=False)
    apple: Cell | None = field(init=False)
    heading: int = field(init=False)
    steps: int = field(init=False)
    score: int = field(init=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.width < 4 or self.height < 4:
            raise ValueError("grid must be at least 4x4")
        self._rng = random.Random(self.seed)
        cx, cy = self.width // 2, self.height // 2
        self.body = [(cx, cy), (cx - 1, cy), (cx - 2, cy)]
        self.heading = RIGHT
        self.steps = 0
        self.score = 0
        self.apple = self._place_apple()

    def _place_apple(self) -> Cell | None:
        occupied = set(self.body)
        free = [(x, y) for y in range(self.height) for x in range(self.width)
                if (x, y) not in occupied]
        return self._rng.choice(free) if free else None

    def state(self) -> State:
        return State(self.width, self.height, tuple(self.body), self.apple,
                     self.heading, self.steps, self.max_steps, self.score)

    def step(self, move: int) -> str | None:
        """Advance one step. Returns an end reason, or None to continue."""
        if move not in DELTA:
            raise ValueError(f"illegal move {move!r}; expected 0..3")
        if move == OPPOSITE[self.heading]:
            move = self.heading
        self.heading = move
        dx, dy = DELTA[move]
        hx, hy = self.body[0]
        head = (hx + dx, hy + dy)
        self.steps += 1
        if not (0 <= head[0] < self.width and 0 <= head[1] < self.height):
            return "wall"
        eats = head == self.apple
        # The tail vacates this step unless we are growing.
        blocking = self.body if eats else self.body[:-1]
        if head in blocking:
            return "self"
        self.body.insert(0, head)
        if eats:
            self.score += 1
            self.apple = self._place_apple()
            if self.apple is None:
                return "full"
        else:
            self.body.pop()
        if self.steps >= self.max_steps:
            return "budget"
        return None
