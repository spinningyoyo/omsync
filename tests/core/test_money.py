"""Money as integer cents: parsing, formatting, and largest-remainder allocation."""

import random
from fractions import Fraction

from omsync.core.money import allocate


def test_allocate_sum_invariant_over_random_inputs() -> None:
    rng = random.Random(0)
    for _ in range(2000):
        cents = rng.randint(-10_000_000, 10_000_000)
        weights = [rng.randint(0, 50) for _ in range(rng.randint(1, 8))]
        if sum(weights) == 0:
            weights[0] = 1

        shares = allocate(cents, weights)

        assert len(shares) == len(weights)
        assert sum(shares) == cents
        assert all(share * cents >= 0 for share in shares)
        for weight, share in zip(weights, shares, strict=True):
            exact = Fraction(cents * weight, sum(weights))
            assert abs(share - exact) < 1
