"""Small-sample statistics, stdlib only: bootstrap CIs, log loss, skill scores, calibration, paired tests.

Every resampling function takes a seed, so the same run always reports the same intervals.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

N_BOOT = 2000
SMALL_N = 30  # below this, intervals are wide: say so in every output
EPS = 1e-6  # probability floor for log loss (a confident miss costs ~13.8 nats, not infinity)


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def log_loss(probs: dict[str, float], outcome: str) -> float:
    """Negative log probability given to what happened (natural log). 0 = certain and right."""
    return -math.log(max(EPS, min(1.0, probs.get(outcome, 0.0))))


def _percentile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = mean,  # type: ignore[assignment]
    n_boot: int = N_BOOT,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap (1 - alpha) interval for ``stat`` over ``values``. (None, None) if empty."""
    xs = list(values)
    if not xs:
        return None, None
    if len(xs) == 1:
        return xs[0], xs[0]
    rng = random.Random(seed)
    n = len(xs)
    boots = sorted(stat([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    return _percentile(boots, alpha / 2), _percentile(boots, 1 - alpha / 2)


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], n_boot: int = N_BOOT, seed: int = 0, alpha: float = 0.05
) -> dict:
    """Mean of (a - b) over paired items, its bootstrap CI and a two-sided bootstrap p-value."""
    if len(a) != len(b):
        raise ValueError("paired_bootstrap needs equal-length sequences")
    d = [x - y for x, y in zip(a, b, strict=True)]
    if not d:
        return {"n": 0, "mean_diff": None, "ci_low": None, "ci_high": None, "p_value": None}
    rng = random.Random(seed)
    n = len(d)
    boots = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    md = sum(d) / n
    # Two-sided p: how often the resampled mean falls on the other side of zero (shifted-null form).
    centred = [x - md for x in boots]
    p = (sum(abs(x) >= abs(md) for x in centred) + 1) / (n_boot + 1) if any(d) else 1.0
    return {
        "n": n,
        "mean_diff": md,
        "ci_low": _percentile(boots, alpha / 2),
        "ci_high": _percentile(boots, 1 - alpha / 2),
        "p_value": min(1.0, p),
    }


def mcnemar_exact(a_correct: Sequence[int], b_correct: Sequence[int]) -> dict:
    """Exact (binomial) McNemar test on paired right/wrong outcomes.

    b01 = a wrong & b right, b10 = a right & b wrong. Two-sided p-value over the discordant pairs.
    """
    if len(a_correct) != len(b_correct):
        raise ValueError("mcnemar needs equal-length sequences")
    b10 = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if x and not y)
    b01 = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if y and not x)
    n = b01 + b10
    if n == 0:
        return {"a_only": b10, "b_only": b01, "p_value": 1.0}
    k = min(b01, b10)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n * 2
    return {"a_only": b10, "b_only": b01, "p_value": min(1.0, p)}


def skill_score(score: float | None, reference: float | None) -> float | None:
    """1 - score/reference for a loss (Brier, log loss). >0 beats the reference; 1 is perfect."""
    if score is None or reference is None or reference == 0:
        return None
    return 1 - score / reference


def calibration_bins(confidences: Sequence[float], hits: Sequence[float], n_bins: int = 5) -> list[dict]:
    """Reliability table on top-choice confidence: per bin, mean confidence vs observed hit rate."""
    bins = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        idx = [j for j, c in enumerate(confidences) if (lo <= c < hi) or (i == n_bins - 1 and c == hi)]
        bins.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "lo": lo,
                "hi": hi,
                "n": len(idx),
                "mean_confidence": mean([confidences[j] for j in idx]),
                "hit_rate": mean([hits[j] for j in idx]),
            }
        )
    return bins


def expected_calibration_error(bins: list[dict]) -> float | None:
    total = sum(b["n"] for b in bins)
    if not total:
        return None
    return sum(b["n"] / total * abs(b["mean_confidence"] - b["hit_rate"]) for b in bins if b["n"])


def small_n_warning(n: int) -> str:
    if n >= SMALL_N:
        return ""
    return (
        f"Small sample (n={n} < {SMALL_N}): intervals are wide and one event can swing accuracy by "
        f"{100 / max(n, 1):.0f} points. Treat differences without a significant paired test as noise."
    )
