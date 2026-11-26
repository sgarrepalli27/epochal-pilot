"""SEP-gap derived facts and the --no-context (blind) leak check."""

from datetime import date
from pathlib import Path

from epochal_pilot.cli import main
from epochal_pilot.derived import (
    build_sep_gap,
    count_meetings,
    fmt_bp,
    fmt_rate,
    implied_25bp_moves,
    load_meeting_dates,
    parse_range_midpoint,
    parse_sep_median_funds_rates,
)
from epochal_pilot.public_context import load_public_context
from epochal_pilot.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios" / "fed-quarterly"
MEETINGS = SCEN / "fomc_meetings.csv"


def _pack():
    return load_public_context(SCEN, "public_context")


def _cli(scenarios_dir, tmp_path, *args):
    base = [
        "--scenarios-dir",
        str(scenarios_dir),
        "--cache-dir",
        str(tmp_path / "c"),
        "--env-file",
        str(tmp_path / "x"),
    ]
    return main([*base, *args])


def test_midpoint_and_formatting():
    assert parse_range_midpoint("0.00-0.25%") == 0.125
    assert parse_range_midpoint("3.75-4.00%") == 3.875
    assert parse_range_midpoint("1.50\u20131.75%") == 1.625
    assert fmt_rate(4.1) == "4.1%" and fmt_rate(3.875) == "3.875%" and fmt_rate(0.125) == "0.125%"
    assert fmt_bp(22.5) == "+22.5 bp" and fmt_bp(-2.5) == "-2.5 bp" and fmt_bp(0) == "+0 bp"
    assert implied_25bp_moves(22.5) == 1
    assert implied_25bp_moves(77.5) == 3
    assert implied_25bp_moves(-2.5) == 0
    assert implied_25bp_moves(12.5) == 1
    assert implied_25bp_moves(12.4) == 0
    assert implied_25bp_moves(-37.5) == -2


def test_parse_known_sep_tables():
    pack = _pack()
    by_id = {d.id: d for d in pack.docs if d.kind == "fomc_sep"}
    assert parse_sep_median_funds_rates(by_id["fomc_sep_20211215"].text())[2022] == 0.9
    assert parse_sep_median_funds_rates(by_id["fomc_sep_20260916"].text())[2026] == 4.1
    # Years sit on the Variable row; COVID-era table has three projection years.
    dec19 = parse_sep_median_funds_rates(by_id["fomc_sep_20191211"].text())
    assert dec19[2020] == 1.6 and dec19[2019] == 1.6
    jun20 = parse_sep_median_funds_rates(by_id["fomc_sep_20200610"].text())
    assert jun20[2020] == 0.1 and 2023 not in jun20
    # September 2020 writes tenths as ".1" rather than "0.1".
    sep20 = parse_sep_median_funds_rates(by_id["fomc_sep_20200916"].text())
    assert sep20[2020] == 0.1 and sep20[2023] == 0.1
    # After lift-off the 2022 median jumps — used as a leak-detection fixture below.
    assert parse_sep_median_funds_rates(by_id["fomc_sep_20220316"].text())[2022] == 1.9


def test_every_shipped_sep_parses():
    pack = _pack()
    seps = [d for d in pack.docs if d.kind == "fomc_sep"]
    assert len(seps) >= 40
    for d in seps:
        rates = parse_sep_median_funds_rates(d.text())
        assert rates, f"{d.id}: failed to parse median funds-rate columns"
        assert all(2015 <= y <= 2030 and 0 <= v <= 10 for y, v in rates.items()), (d.id, rates)


def test_sep_gap_2022q1_and_2026q4():
    pack, meetings = _pack(), MEETINGS
    g = build_sep_gap(as_of="2022-02-15", target="2022-03-31", range_at_asof="0.00-0.25%", pack=pack, meetings=meetings)
    assert g.median == 0.9 and g.midpoint == 0.125 and g.gap_bp == 77.5
    assert g.implied_moves == 3 and g.meetings == 1
    assert g.sep_published == date(2021, 12, 15)
    assert g.text == (
        "Derived: SEP median end-2022 0.9% vs current midpoint 0.125% = +77.5 bp, "
        "about 3 x 25 bp hikes over the full year; 1 FOMC meeting before target"
    )

    live = build_sep_gap(
        as_of="2026-11-15", target="2026-12-31", range_at_asof="3.75-4.00%", pack=pack, meetings=meetings
    )
    assert live.median == 4.1 and live.midpoint == 3.875 and live.gap_bp == 22.5
    assert live.implied_moves == 1 and live.meetings == 1
    assert live.sep_published == date(2026, 9, 16)
    assert live.text == (
        "Derived: SEP median end-2026 4.1% vs current midpoint 3.875% = +22.5 bp, "
        "about 1 x 25 bp hike over the full year; 1 FOMC meeting before target"
    )


def test_sep_choice_never_uses_a_document_on_or_after_t():
    pack = _pack()
    # Meeting day: the March 2022 SEP is published that afternoon; midnight T must not see it.
    same_day = build_sep_gap(
        as_of="2022-03-16", target="2022-03-31", range_at_asof="0.00-0.25%", pack=pack, meetings=MEETINGS
    )
    assert same_day.sep_published == date(2021, 12, 15) and same_day.median == 0.9
    after = build_sep_gap(
        as_of="2022-03-17", target="2022-06-30", range_at_asof="0.25-0.50%", pack=pack, meetings=MEETINGS
    )
    assert after.sep_published == date(2022, 3, 16) and after.median == 1.9


def test_missing_sep_is_graceful():
    # No pack at all (the situation if a March 2020-style cancelled SEP were the only source).
    g = build_sep_gap(
        as_of="2020-02-15",
        target="2020-03-31",
        range_at_asof="1.50-1.75%",
        pack=None,
        meetings=MEETINGS,
    )
    assert g.median is None and g.meetings == 1
    assert g.text == "Derived: no SEP median for end-2020 was available before today; 1 FOMC meeting before target"
    # The shipped pack *does* have the Dec 2019 SEP for T = 15 Feb 2020; that path still works.
    present = build_sep_gap(
        as_of="2020-02-15", target="2020-03-31", range_at_asof="1.50-1.75%", pack=_pack(), meetings=MEETINGS
    )
    assert present.sep_published == date(2019, 12, 11)
    assert present.median == 1.6 and abs(present.gap_bp - (1.6 - 1.625) * 100) < 1e-9
    assert present.implied_moves == 0


def test_meeting_count_uses_only_the_window_after_t():
    dates = load_meeting_dates(MEETINGS)
    assert count_meetings(dates, date(2022, 2, 15), date(2022, 3, 31)) == 1  # 16 Mar
    assert count_meetings(dates, date(2026, 11, 15), date(2026, 12, 31)) == 1  # 9 Dec
    # A meeting on T has already happened; do not count it.
    assert count_meetings(dates, date(2022, 3, 16), date(2022, 3, 31)) == 0
    assert count_meetings([], date(2022, 2, 15), date(2022, 3, 31)) == 0


def test_public_prompt_includes_derived_line(scenarios_dir, tmp_path, monkeypatch):
    from epochal_pilot import runner

    seen = []
    real = runner.mock_transport

    def spy(model, prompt):
        seen.append(prompt)
        return real(model, prompt)

    monkeypatch.setattr(runner, "mock_transport", spy)
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "openrouter",
        "--dry-run",
        "--context",
        "public",
        "--only",
        "fq_2022q1",
        "--out",
        str(tmp_path / "pub"),
    )
    assert rc == 0 and len(seen) == 1
    p = seen[0]
    assert "Derived: SEP median end-2022 0.9% vs current midpoint 0.125% = +77.5 bp" in p
    assert "about 3 x 25 bp hikes over the full year; 1 FOMC meeting before target" in p
    assert "7.5 percent" in p  # CPI document is present
    assert "0.25-0.50" not in p  # outcome range must not leak


def test_no_context_is_blind_and_costs_zero(scenarios_dir, tmp_path, capsys, monkeypatch):
    from epochal_pilot import runner

    seen = []
    real = runner.mock_transport

    def spy(model, prompt):
        seen.append(prompt)
        return real(model, prompt)

    monkeypatch.setattr(runner, "mock_transport", spy)
    out = tmp_path / "blind"
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "openrouter",
        "--dry-run",
        "--no-context",
        "--only",
        "fq_2022q1",
        "--out",
        str(out),
    )
    text = capsys.readouterr().out
    assert rc == 0
    assert "Context: none (blind" in text and "Epochal requests: 0" in text.split("Context:")[1][:200]
    assert len(seen) == 1
    p = seen[0]
    assert "2022-02-15" in p and "0.00-0.25%" in p
    assert "Derived:" not in p
    assert "7.5 percent" not in p and "soon be appropriate" not in p
    assert "this is a blind forecast" in p
    import csv

    rows = list(csv.DictReader((out / "predictions.csv").open(encoding="utf-8")))
    assert rows[0]["context"] == "blind" and rows[0]["n_docs"] == "0" and rows[0]["event"] == "fq_2022q1"


def test_no_context_overrides_epochal_and_needs_no_key(scenarios_dir, tmp_path, capsys):
    # --context epochal would otherwise demand EPOCHAL_API_KEY; --no-context must not.
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--dry-run",
        "--context",
        "epochal",
        "--no-context",
        "--mode",
        "live",
        "--out",
        str(tmp_path / "live-blind"),
    )
    text = capsys.readouterr().out
    assert rc == 0
    assert "blind" in text.lower()
    import csv

    rows = list(csv.DictReader((tmp_path / "live-blind" / "forecast.csv").open(encoding="utf-8")))
    assert rows[0]["event"] == "fq_2026q4" and rows[0]["context"] == "blind" and rows[0]["n_docs"] == "0"


def test_fed_rates_prompt_is_unchanged_by_sep_gap(scenarios_dir, tmp_path, monkeypatch):
    from epochal_pilot import runner

    seen = []
    real = runner.mock_transport

    def spy(model, prompt):
        seen.append(prompt)
        return real(model, prompt)

    monkeypatch.setattr(runner, "mock_transport", spy)
    sc = load_scenario(scenarios_dir, "fed-rates")
    eid = sc.events[0].id
    assert (
        _cli(
            scenarios_dir,
            tmp_path,
            "run",
            "fed-rates",
            "--backend",
            "openrouter",
            "--dry-run",
            "--context",
            "public",
            "--only",
            eid,
            "--out",
            str(tmp_path / "fr"),
        )
        == 0
    )
    assert seen and "Derived:" not in seen[0]
