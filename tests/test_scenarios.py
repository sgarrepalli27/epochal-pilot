import itertools
from datetime import date

import pytest

from epochal_pilot.scenario import discover, load_events, load_scenario


def test_ships_two_scenarios(scenarios_dir):
    assert {"fed-rates", "future-leak"} <= set(discover(scenarios_dir))


def test_fed_rates_events(scenarios_dir):
    sc = load_scenario(scenarios_dir, "fed-rates")
    assert len(sc.events) == 23
    assert sc.lead_days == 7 and sc.num_results == 10 and sc.public_context == "public_context"
    by = {e.id: e for e in sc.events}
    assert by["fomc_2024_09"].outcome == "Cut 50 bp"
    assert by["fomc_2024_11"].event_date == date(2024, 11, 7)
    assert by["fomc_2026_09"].outcome == "Hike 25 bp"
    assert sum(e.outcome == "Hold" for e in sc.events) == 16
    for e in sc.events:
        assert e.source_url.startswith("https://www.federalreserve.gov/newsevents/pressreleases/monetary")
        assert e.event_date.strftime("%Y%m%d") in e.source_url
        assert e.extra["previous_outcome"] in e.choices
        assert date(2024, 1, 1) <= e.event_date <= date(2026, 11, 26)


def test_fed_previous_outcome_chain(scenarios_dir):
    evs = load_scenario(scenarios_dir, "fed-rates").events
    for prev, cur in itertools.pairwise(evs):
        assert cur.extra["previous_outcome"] == prev.outcome
        assert cur.extra["range_before"] == prev.extra["range_after"]


def test_future_leak_events(scenarios_dir):
    sc = load_scenario(scenarios_dir, "future-leak")
    assert len(sc.events) == 20
    assert sc.modes == ["gated", "ungated"]
    assert sc.shuffle


def test_as_of(scenarios_dir):
    e = load_scenario(scenarios_dir, "fed-rates").events[0]
    assert e.as_of(7) == "2024-01-24T00:00:00Z"
    assert e.as_of(28) == "2024-01-03T00:00:00Z"


def test_shuffle_is_seeded_and_fixes_position(scenarios_dir):
    evs = load_scenario(scenarios_dir, "future-leak").events
    a = [e.ordered_choices(True, 0) for e in evs]
    assert a == [e.ordered_choices(True, 0) for e in evs]  # reproducible
    assert a != [e.ordered_choices(True, 1) for e in evs]  # seed matters
    assert [e.ordered_choices(False, 0) for e in evs] == [e.choices for e in evs]
    for e, order in zip(evs, a, strict=False):
        assert sorted(order) == sorted(e.choices)
    # The unshuffled file puts the answer at B for 12/20 events; shuffling must break that.
    at_b = sum(o.index(e.outcome) == 1 for e, o in zip(evs, a, strict=False))
    assert at_b < 12


def test_bad_outcome_rejected(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text(
        "id,event_date,question,choices,outcome,source_url\nx,2024-01-01,Q?,A|B,C,https://x\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not among choices"):
        load_events(p)


def test_unknown_scenario(scenarios_dir):
    with pytest.raises(FileNotFoundError, match="available"):
        load_scenario(scenarios_dir, "nope")


def test_models_json_post_cutoff_lists_match(scenarios_dir):
    from epochal_pilot.scoring import is_post_cutoff, models_table

    evs = load_scenario(scenarios_dir, "fed-rates").events
    for model, v in models_table().items():
        want = [e.id for e in evs if is_post_cutoff(e.event_date, v["cutoff"])]
        assert v["fed_rates_post_cutoff"] == want, model


def test_future_leak_questions_are_future_tense(scenarios_dir):
    from epochal_pilot.validate import wording_issues

    for e in load_scenario(scenarios_dir, "future-leak").events:
        assert not wording_issues(e.question, e.outcome, e.choices), e.id
        assert " will " in f" {e.question.lower()} "
