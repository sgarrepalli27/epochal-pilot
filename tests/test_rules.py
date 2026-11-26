"""Deterministic rule analysis on first-release pack documents (no model, no network)."""

from pathlib import Path

from epochal_pilot.public_context import load_public_context
from epochal_pilot.rules import (
    analyse_event,
    distribution_from_bp,
    parse_cpi,
    parse_jobs,
    parse_pce,
    parse_two_year,
    rules_forecast,
)
from epochal_pilot.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios" / "fed-quarterly"


def _pack_event():
    pack = load_public_context(SCEN, "public_context")
    sc = load_scenario(ROOT / "scenarios", "fed-quarterly")
    return pack, {e.id: e for e in sc.events}, SCEN / "fomc_meetings.csv"


def test_parsers_on_2022_and_2026_releases():
    pack, _, _ = _pack_event()
    docs = {d.kind: d.text() for d in pack.select("2022-02-15")}
    assert parse_cpi(docs["bls_cpi"]) == (7.5, 6.0)
    assert parse_pce(docs["bea_pce"]) == (5.8, 4.9)
    assert parse_jobs(docs["bls_jobs"]) == (4.0, "unchanged", 467)
    assert parse_two_year(docs["treasury_yields"]) == 1.58

    docs6 = {d.kind: d.text() for d in pack.select("2026-11-15")}
    assert parse_cpi(docs6["bls_cpi"]) == (None, None)  # no YoY in that release excerpt
    assert parse_pce(docs6["bea_pce"]) == (3.6, 3.1)
    assert parse_jobs(docs6["bls_jobs"]) == (4.1, "unchanged", 165)
    assert parse_two_year(docs6["treasury_yields"]) == 4.88


def test_analyse_2022q1_is_hike_25_and_2020q1_hold_is_honest():
    pack, by, meetings = _pack_event()
    sig = analyse_event(ev=by["fq_2022q1"], as_of="2022-02-15", pack=pack, meetings_path=meetings)
    assert sig.core_inflation() == 4.9
    assert sig.inflation_gap_pp == 2.9
    assert sig.unemployment == 4.0 and sig.payrolls == 467
    assert sig.two_year == 1.58
    assert sig.statement_tone == "hawkish"
    assert sig.shock_present is False
    assert sig.bucket == "Hike 25 bp"
    assert sig.expected_bp == 25.0

    miss = analyse_event(ev=by["fq_2020q1"], as_of="2020-02-15", pack=pack, meetings_path=meetings)
    # January 2020 statement has no emergency language; the checklist does not invent the COVID cut.
    assert miss.shock_present is False
    assert miss.bucket == "No change"


def test_missing_fields_do_not_crash():
    pack, by, meetings = _pack_event()
    # Empty parsers
    assert parse_cpi("") == (None, None)
    assert parse_pce("not a release") == (None, None)
    assert parse_jobs("") == (None, None, None)
    assert parse_two_year("") is None
    sig = analyse_event(ev=by["fq_2022q1"], as_of="2016-02-15", pack=pack, meetings_path=meetings)
    # Very early as-of may have a thin pack; still returns a bucket.
    assert sig.bucket in {
        "Cut 100+ bp",
        "Cut 75 bp",
        "Cut 50 bp",
        "Cut 25 bp",
        "No change",
        "Hike 25 bp",
        "Hike 50 bp",
        "Hike 75+ bp",
    }


def test_distribution_and_forecast_are_deterministic():
    pack, by, meetings = _pack_event()
    shown = list(by["fq_2022q1"].choices)
    sig = analyse_event(ev=by["fq_2022q1"], as_of="2022-02-15", pack=pack, meetings_path=meetings, choices=shown)
    first = rules_forecast(sig, shown)
    second = rules_forecast(sig, shown)
    assert first.answer == "Hike 25 bp" and first.parse_mode == "rules"
    assert first.probabilities == second.probabilities
    assert abs(sum(first.probabilities.values()) - 1.0) < 1e-9
    dist = distribution_from_bp(25, shown)
    assert max(dist, key=dist.get) == "Hike 25 bp"
