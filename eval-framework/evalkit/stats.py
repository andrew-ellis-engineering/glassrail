"""Statistics: pass@k (capability ceiling) and pass^k (reliability floor).

pass@k uses the Chen 2021 unbiased estimator, NOT the biased
``1 - (1 - p_hat)**k`` (principle 2).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from evalkit.models import Score


def pass_at_k(n: int, c: int, k: int) -> float:
    """Chen 2021 unbiased estimator: ``1 - C(n-c, k) / C(n, k)``.

    Probability that at least one of ``k`` sampled trials passes, given ``c``
    of ``n`` trials passed.
    """
    if n < k:
        return 0.0
    if c == 0:
        return 0.0
    if c >= n:
        return 1.0
    if n - c < k:
        # CRITICAL EDGE CASE: every k-subset must contain a passing trial, so
        # pass@k == 1. Without this guard, the product below hits math.log(0),
        # which raises "expected a positive input" on Python 3.13+.
        return 1.0
    log_ratio = sum(math.log(n - c - i) - math.log(n - i) for i in range(k))
    return 1.0 - math.exp(log_ratio)


def pass_pow_k(scores: Sequence[Score]) -> float:
    """Fraction of trials where ALL criteria passed (pass^k)."""
    if not scores:
        return 0.0
    perfect = sum(1 for s in scores if s.pass_rate == 1.0)
    return perfect / len(scores)


def wilson_ci(successes: int, trials: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if trials == 0:
        return (0.0, 0.0)
    # Two-sided z for the given alpha (z ~= 1.96 at alpha=0.05).
    z = _z_for_alpha(alpha)
    p = successes / trials
    denom = 1.0 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    margin = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def mean_pass_rate(scores: Sequence[Score]) -> float:
    """Average criterion pass rate across trials."""
    if not scores:
        return 0.0
    return sum(s.pass_rate for s in scores) / len(scores)


def _z_for_alpha(alpha: float) -> float:
    """Two-sided normal critical value via the inverse-erf relation.

    z = sqrt(2) * erfinv(1 - alpha). Python's stdlib has ``math.erf`` but not
    ``erfinv``; a short bisection on ``erf`` is exact enough and dependency-free.
    """
    target = 1.0 - alpha
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.erf(mid / math.sqrt(2)) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
