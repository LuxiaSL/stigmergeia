"""Baseline: shortest path (BFS) to the apple; otherwise any safe move.

Fast, and dies once the body is long enough to trap it."""
from collections import deque

DELTA = {0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}


def act(s):
    blocked = set(s.body[:-1])
    start = s.body[0]
    first = {}
    q = deque([start])
    seen = {start}
    while q:
        cur = q.popleft()
        if cur == s.apple:
            return first[cur]
        for m, (dx, dy) in DELTA.items():
            nxt = (cur[0] + dx, cur[1] + dy)
            if (0 <= nxt[0] < s.width and 0 <= nxt[1] < s.height
                    and nxt not in blocked and nxt not in seen):
                seen.add(nxt)
                first[nxt] = first.get(cur, m)
                q.append(nxt)
    hx, hy = start
    for m, (dx, dy) in DELTA.items():
        n = (hx + dx, hy + dy)
        if 0 <= n[0] < s.width and 0 <= n[1] < s.height and n not in blocked:
            return m
    return s.heading
