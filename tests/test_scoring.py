from datetime import date

import pytest

from epochal_pilot.scenario import Event
from epochal_pilot.scoring import baselines, brier, cutoff_for, month_end, summarise

CH = ["Hold", "Cut 25 bp", "Cut 50 bp"]


def ev(i, d, outcome, prev="Hold"):
    return Event(i, date.fromisoformat(d), "q", CH, outcome, "https://x", extra={"previous_outcome": prev})


def test_brier():
    assert brier({"Hold": 1.0, "Cut 25 bp": 0, "Cut 50 bp": 0}, "Hold") == 0
    assert brier({"Hold": 0, "Cut 25 bp": 1.0, "Cut 50 bp": 0}, "Hold") == 2
    assert brier({c: 1 / 3 for c in CH}, "Hold") == pytest.approx(2 / 3, abs=1e-3)


def test_month_end_and_cutoffs():
    assert month_end("2023-12") == date(2023, 12, 31)
    assert month_end("2024-02") == date(2024, 2, 29)
    assert cutoff_for("openai/gpt-4.1-mini") == "2024-06"
    assert cutoff_for("x/unknown") is None
    assert cutoff_for("x/unknown", {"x/unknown": "2025-03"}) == "2025-03"


def test_baselines():
    evs = [
        ev("a", "2024-01-31", "Hold"),
        ev("b", "2024-09-18", "Cut 50 bp"),
        ev("c", "2024-11-07", "Cut 25 bp", "Cut 50 bp"),
    ]
    spec = {"constant": {"no-change": "Hold"}, "persistence_column": "previous_outcome", "majority": True}
    by = {b["model"]: b for b in baselines(evs, spec)}
    assert by["baseline:no-change"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:persistence"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:uniform"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:uniform"]["brier"] == pytest.approx(2 / 3, abs=1e-3)


def _row(model, mode, e, answer, letter, truth_letter, p=1.0):
    probs = {c: (p if c == answer else (1 - p) / 2) for c in CH}
    return {
        "model": model,
        "mode": mode,
        "event": e.id,
        "answer": answer,
        "answer_letter": letter,
        "truth_letter": truth_letter,
        "correct": int(answer == e.outcome),
        "abstained": 0,
        "brier": brier(probs, e.outcome),
        "dropped_future": 1,
        "citations_in_context": 1.0,
    }


def test_summarise_cutoff_split_and_leak():
    a, b = ev("a", "2024-03-20", "Hold"), ev("b", "2024-09-18", "Cut 50 bp")
    events = {"a": a, "b": b}
    rows = [
        _row("m", "gated", a, "Hold", "A", "A"),
        _row("m", "gated", b, "Hold", "A", "C"),
        _row("m", "ungated", a, "Hold", "A", "A"),
        _row("m", "ungated", b, "Cut 50 bp", "C", "C"),
    ]
    s = {(r["model"], r["mode"]): r for r in summarise(rows, events, {"m": "2024-06"})}
    g = s[("m", "gated")]
    assert g["accuracy"] == 0.5 and g["n_pre_cutoff"] == 1 and g["acc_pre_cutoff"] == 1.0
    assert g["acc_post_cutoff"] == 0.0
    assert g["leak_score_upper_bound"] == pytest.approx(0.5 - 1 / 3, abs=1e-3)
    assert g["gap_ungated_minus_gated"] == 0.5
    assert g["fav_letter"] == "A" and g["always_fav_letter_acc"] == 0.5
    assert g["docs_dropped_after_as_of"] == 2
