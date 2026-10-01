"""The Collatz map, exactly, with the gate's step cap.

C(n) = n / 2 for even n, 3n + 1 for odd n. delay(n) is the number of
applications of C until the orbit first reaches 1; delay(1) = 0. Big
integers throughout: nothing here touches a float.

The conjecture is verified only below 2^71, so an orbit above that is never
run unbounded: `orbit` stops at `cap` steps and says so.
"""

from __future__ import annotations

from dataclasses import dataclass

SIZES = (128, 256, 512, 1024)   # the bit sizes the task asks for
CAP_PER_BIT = 100               # an orbit is followed for at most CAP_PER_BIT * B steps


@dataclass(frozen=True)
class Orbit:
    delay: int | None     # steps to reach 1; None when the cap stopped it first
    peak_bits: int        # bit length of the largest value seen
    steps_run: int


def orbit(n: int, cap: int) -> Orbit:
    """Follow n under C for at most `cap` steps."""
    if n < 1:
        raise ValueError("the map is followed from positive integers only")
    peak, steps = n, 0
    while n != 1:
        if steps >= cap:
            return Orbit(None, peak.bit_length(), steps)
        n = n >> 1 if n & 1 == 0 else 3 * n + 1
        if n > peak:
            peak = n
        steps += 1
    return Orbit(steps, peak.bit_length(), steps)


def delay(n: int, cap: int = 10**7) -> int:
    """delay(n), or ValueError when the cap stops it first."""
    o = orbit(n, cap)
    if o.delay is None:
        raise ValueError(f"no 1 within {cap} steps")
    return o.delay
