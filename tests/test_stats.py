import math

import pytest

from epochal_pilot import stats


def test_bootstrap_ci_seeded_and_contains_mean():
    xs = [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]
    lo, hi = stats.bootstrap_ci(xs, seed=1)
    assert (lo, hi) == stats.bootstrap_ci(xs, seed=1)
    assert lo < 0.7 < hi and lo >= 0 and hi <= 1
    assert stats.bootstrap_ci([]) == (None, None)
    assert stats.bootstrap_ci([0.4]) == (0.4, 0.4)


def test_log_loss():
    assert stats.log_loss({"A": 1.0, "B": 0.0}, "A") == pytest.approx(0)
    assert stats.log_loss({"A": 0.5, "B": 0.5}, "A") == pytest.approx(math.log(2))
    assert stats.log_loss({"A": 0.0, "B": 1.0}, "A") == pytest.approx(-math.log(stats.EPS))


def test_skill_score():
    assert stats.skill_score(0.25, 0.5) == pytest.approx(0.5)
    assert stats.skill_score(0.5, 0.5) == 0
    assert stats.skill_score(None, 0.5) is None and stats.skill_score(0.1, 0) is None


def test_mcnemar_exact():
    # 6 discordant pairs all favouring A: p = 2 * 0.5^6
    r = stats.mcnemar_exact([1] * 6 + [1, 0], [0] * 6 + [1, 0])
    assert r["a_only"] == 6 and r["b_only"] == 0
    assert r["p_value"] == pytest.approx(2 / 64)
    assert stats.mcnemar_exact([1, 0], [1, 0])["p_value"] == 1.0


def test_paired_bootstrap():
    a = [0.1] * 20
    b = [0.6] * 20
    r = stats.paired_bootstrap(a, b, seed=0)
    assert r["mean_diff"] == pytest.approx(-0.5) and r["ci_high"] < 0 and r["p_value"] < 0.01
    same = stats.paired_bootstrap([0.3, 0.4], [0.3, 0.4])
    assert same["mean_diff"] == 0 and same["p_value"] == 1.0
    with pytest.raises(ValueError):
        stats.paired_bootstrap([1], [1, 2])


def test_calibration_bins_and_ece():
    conf = [0.95, 0.9, 0.3, 0.25, 1.0]
    hits = [1, 1, 0, 1, 1]
    bins = stats.calibration_bins(conf, hits, n_bins=5)
    assert sum(b["n"] for b in bins) == 5
    top = bins[-1]
    assert top["n"] == 3 and top["hit_rate"] == 1.0
    assert stats.expected_calibration_error(bins) >= 0


def test_small_n_warning():
    assert "n=12" in stats.small_n_warning(12)
    assert stats.small_n_warning(40) == ""
