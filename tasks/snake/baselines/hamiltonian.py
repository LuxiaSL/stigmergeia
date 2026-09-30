"""Baseline: follow a fixed Hamiltonian cycle. Never dies; ignores the apple.

Column 0 is a return lane running DOWN. Rows are swept over columns
1..W-1 from the bottom row up, alternating direction, so the bottom row
runs left-to-right and row 0 ends at column 1, stepping left into the lane.
With an even height the snake's start row (H//2, heading right) runs
left-to-right, so it starts ON the cycle, moving with it.

Safe forever, but slow: the step budget is what makes it lose."""

UP, RIGHT, DOWN, LEFT = 0, 1, 2, 3


class Policy:
    def __init__(self, width, height):
        if height % 2:
            raise ValueError("this cycle needs an even height")
        nxt = {}
        for y in range(height - 1, -1, -1):
            left_to_right = (height - 1 - y) % 2 == 0
            xs = list(range(1, width)) if left_to_right else list(range(width - 1, 0, -1))
            for a in xs[:-1]:
                nxt[(a, y)] = RIGHT if left_to_right else LEFT
            nxt[(xs[-1], y)] = UP if y > 0 else LEFT
        for y in range(height - 1):
            nxt[(0, y)] = DOWN
        nxt[(0, height - 1)] = RIGHT
        self.next = nxt

    def act(self, s):
        return self.next[s.body[0]]
