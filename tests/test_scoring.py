import json
from datetime import date

import pytest

from epochal_pilot.scenario import Event
from epochal_pilot.scoring import analyse, baseline_forecasts, brier, cutoff_for, load_user_baseline, month_end

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


EVS = [
    ev("a", "2024-01-31", "Hold"),
    ev("b", "2024-09-18", "Cut 50 bp"),
    ev("c", "2024-11-07", "Cut 25 bp", "Cut 50 bp"),
]
SPEC = {"constant": {"no-change": "Hold"}, "persistence_column": "previous_outcome", "majority": True}


def test_baselines_and_hindsight_flag():
    base = baseline_forecasts(EVS, SPEC)
    scores, _ = analyse([], base)
    by = {s["model"]: s for s in scores}
    assert by["baseline:no-change"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:persistence"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:uniform"]["accuracy"] == pytest.approx(1 / 3, abs=1e-3)
    assert by["baseline:uniform"]["brier"] == pytest.approx(2 / 3, abs=1e-3)
    assert by["baseline:majority(hindsight)"]["hindsight"] == 1
    assert by["baseline:no-change"]["hindsight"] == 0
    assert by["baseline:uniform"]["accuracy_ci_low"] == by["baseline:uniform"]["accuracy_ci_high"]


def _row(model, mode, e, answer, letter, truth_letter, p=1.0):
    probs = {c: (p if c == answer else (1 - p) / 2) for c in CH}
    return {
        "model": model,
        "mode": mode,
        "event": e.id,
        "event_date": e.event_date.isoformat(),
        "outcome": e.outcome,
        "answer": answer,
        "answer_letter": letter,
        "truth_letter": truth_letter,
        "correct": int(answer == e.outcome),
        "abstained": 0,
        "probabilities": json.dumps(probs),
        "dropped_future": 1,
        "citations_in_context": 1.0,
    }


def test_analyse_cutoff_split_leak_and_pairs():
    a, b = ev("a", "2024-03-20", "Hold"), ev("b", "2024-09-18", "Cut 50 bp")
    rows = [
        _row("m", "gated", a, "Hold", "A", "A"),
        _row("m", "gated", b, "Hold", "A", "C"),
        _row("m", "ungated", a, "Hold", "A", "A"),
        _row("m", "ungated", b, "Cut 50 bp", "C", "C"),
    ]
    scores, detail = analyse(rows, baseline_forecasts([a, b], SPEC), {"m": "2024-06"})
    s = {(r["model"], r["mode"]): r for r in scores}
    g = s[("m", "gated")]
    assert g["context"] == "epochal" and s[("m", "ungated")]["context"] == "none"
    assert g["accuracy"] == 0.5 and g["n_pre_cutoff"] == 1 and g["acc_pre_cutoff"] == 1.0
    assert g["acc_post_cutoff"] == 0.0
    assert g["leak_score_upper_bound"] == pytest.approx(0.5 - 1 / 3, abs=1e-3)
    assert g["gap_ungated_minus_gated"] == 0.5
    assert g["fav_letter"] == "A" and g["always_fav_letter_acc"] == 0.5
    assert g["docs_dropped_after_as_of"] == 2
    assert g["small_n"] == 1
    # Reference excludes the hindsight majority baseline.
    assert g["reference"] in {"baseline:no-change", "baseline:persistence", "baseline:uniform"}
    assert "hindsight" not in g["reference"]
    assert g["brier_skill"] is not None and g["brier_skill_ci_low"] <= g["brier_skill_ci_high"]
    assert detail["pairs"] and detail["pairs"][0]["n"] == 2
    assert "mcnemar" in detail["pairs"][0]
    assert len(detail["groups"]["m|gated|epochal"]["calibration"]) == 5
    assert "Small sample" in detail["groups"]["m|gated|epochal"]["warning"]


def test_analyse_is_seeded():
    rows = [_row("m", "gated", e, "Hold", "A", "A", p=0.7) for e in EVS]
    s1, _ = analyse(rows, baseline_forecasts(EVS, SPEC), seed=3)
    s2, _ = analyse(rows, baseline_forecasts(EVS, SPEC), seed=3)
    assert s1 == s2


def test_user_baseline_csv(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("name,event,Hold,Cut 25 bp,Cut 50 bp\nmarket,a,0.9,0.1,0\nmarket,b,0.2,0.3,0.5\n", encoding="utf-8")
    base = baseline_forecasts(EVS, {"uniform": True, "majority": False}, load_user_baseline(p))
    scores, _ = analyse([], base)
    m = {s["model"]: s for s in scores}["baseline:market"]
    assert m["n"] == 2 and m["accuracy"] == 1.0 and m["hindsight"] == 0
