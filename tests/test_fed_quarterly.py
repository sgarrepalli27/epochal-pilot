"""fed-quarterly: answer key, rolling as-of calendar, no-leak public pack, live mode, report fixes."""

import csv
import importlib.util
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

from epochal_pilot import runner
from epochal_pilot.cli import main
from epochal_pilot.llm import ClaudeCLIError
from epochal_pilot.public_context import load_public_context
from epochal_pilot.report import latest_run, markdown
from epochal_pilot.runner import select_events
from epochal_pilot.scenario import load_scenario
from epochal_pilot.scoring import direction_summary
from epochal_pilot.validate import validate_scenario

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios" / "fed-quarterly"


def _tool():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("build_fed_quarterly", ROOT / "tools" / "build_fed_quarterly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _built(today: str) -> dict[str, dict]:
    t = _tool()
    rows = t.build_events(
        t.load_changes(SCEN / "fomc_changes.csv"),
        t.load_meetings(SCEN / "fomc_meetings.csv"),
        date.fromisoformat(today),
    )
    return {r["id"]: r for r in rows}


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------------------------------- answer key


def test_bucket_edges():
    t = _tool()
    assert t.bucket(0) == "No change"
    assert t.bucket(-25) == "Cut 25 bp" and t.bucket(50) == "Hike 50 bp"
    assert t.bucket(-150) == "Cut 100+ bp" and t.bucket(-100) == "Cut 100+ bp"
    assert t.bucket(75) == "Hike 75+ bp" and t.bucket(150) == "Hike 75+ bp"
    with pytest.raises(ValueError):
        t.bucket(10)


def test_truth_derivation_known_quarters():
    ev = _built("2026-11-26")
    assert len(ev) == 44
    # Mar 2020: two emergency cuts (-50 on 3 Mar, -100 on 15 Mar) net -150 bp.
    assert ev["fq_2020q1"]["outcome"] == "Cut 100+ bp" and ev["fq_2020q1"]["change_bp"] == "-150"
    assert ev["fq_2020q1"]["range_at_asof"] == "1.50-1.75%" and ev["fq_2020q1"]["range_at_quarter_end"] == "0.00-0.25%"
    # The range at T already includes the 31 Jul 2019 cut; the quarter adds the 18 Sep cut.
    assert ev["fq_2019q3"]["range_at_asof"] == "2.00-2.25%" and ev["fq_2019q3"]["outcome"] == "Cut 25 bp"
    assert ev["fq_2022q4"]["outcome"] == "Hike 50 bp" and ev["fq_2022q2"]["outcome"] == "Hike 75+ bp"
    assert ev["fq_2024q3"]["outcome"] == "Cut 50 bp"
    assert ev["fq_2026q3"]["outcome"] == "Hike 25 bp" and ev["fq_2026q3"]["direction"] == "up"
    assert ev["fq_2023q2"]["outcome"] == "No change" and ev["fq_2023q2"]["previous_outcome"] == "Hike 25 bp"
    # Only scheduled meetings are shown (the calendar is public a year ahead), never the emergency ones.
    assert ev["fq_2020q1"]["meetings_in_window"] == "18 March 2020"
    assert ev["fq_2026q4"]["meetings_in_window"] == "9 December 2026"


def test_live_quarter_resolves_after_its_end():
    live = _built("2026-11-26")["fq_2026q4"]
    assert live["status"] == "live" and live["outcome"] == ""
    later = _built("2027-01-05")["fq_2026q4"]
    assert later["status"] == "backtest" and later["outcome"] == "No change"  # no change recorded after Oct 2026


def test_committed_events_match_the_answer_key():
    built = _built("2100-01-01")
    for r in _read(SCEN / "events.csv"):
        assert r["as_of"] == built[r["id"]]["as_of"]
        if r["outcome"]:
            assert r["outcome"] == built[r["id"]]["outcome"], r["id"]
            assert r["range_at_quarter_end"] == built[r["id"]]["range_at_quarter_end"]


def test_alfred_summary_is_first_release_math():
    t = _tool()
    obs = [(date(2023, m, 1), 100.0 + m) for m in range(1, 13)] + [(date(2024, 1, 1), 105.06)]
    assert t.alfred_summary(obs, "yoy").startswith("4.0% y/y (Jan 2024)")
    assert t.alfred_summary([(date(2024, 1, 1), 3.7)], "level") == "3.7 (Jan 2024)"
    assert t.alfred_summary([], "level") == "n/a"


# ------------------------------------------------------------------------------------------- scenario + leak


def test_scenario_loads_with_fixed_as_of_and_tags(scenarios_dir):
    sc = load_scenario(scenarios_dir, "fed-quarterly")
    by = {e.id: e for e in sc.events}
    e = by["fq_2016q1"]
    assert e.fixed_as_of == date(2016, 2, 15)
    assert e.as_of(7) == e.as_of(45) == "2016-02-15T00:00:00Z"  # lead days don't move a fixed as-of
    assert len(select_events(sc, None, None)) == 43
    assert [x.id for x in select_events(sc, None, None, "live")] == ["fq_2026q4"]
    tp = select_events(sc, "turning-point", None)
    assert len(tp) == 10 and all("turning-point" in x.tags for x in tp)
    assert [x.id for x in select_events(sc, "fq_2016q1,turning-point", 2)] == ["fq_2016q1", "fq_2016q4"]
    with pytest.raises(SystemExit, match="unknown"):
        select_events(sc, "no-such-tag", None)


def test_public_pack_never_leaks(scenarios_dir):
    sc = load_scenario(scenarios_dir, "fed-quarterly")
    pack = load_public_context(sc.path, sc.public_context)
    assert pack is not None
    assert not pack.leak_check({e.id: e.as_of(sc.lead_days) for e in sc.events})
    for e in sc.events:
        gate = e.fixed_as_of
        docs = pack.select(e.as_of(sc.lead_days))
        assert {d.kind for d in docs} == set(pack.kinds)
        assert all(d.published < gate for d in docs), e.id
        # The decision being forecast is never in its own context.
        for d in docs:
            if d.kind == "fomc_statement":
                assert d.published < gate <= e.event_date
    # T = 15 Feb 2020: the latest statement is 29 Jan 2020, not the March emergency cuts.
    feb20 = {d.kind: d for d in pack.select("2020-02-15T00:00:00Z")}
    assert feb20["fomc_statement"].published == date(2020, 1, 29)
    assert feb20["treasury_yields"].published == date(2020, 2, 14)


def test_validate_fed_quarterly(scenarios_dir):
    errors, _warnings, info = validate_scenario(scenarios_dir, "fed-quarterly", today=date(2026, 11, 26))
    assert errors == []
    assert info["events"] == 44 and info["live"] == 1 and "turning-point" in info["tags"]


def test_direction_summary_counts_ties_fairly():
    dirs = {"Cut 25 bp": "down", "No change": "hold", "Hike 25 bp": "up"}
    bp = {"Cut 25 bp": -25, "No change": 0, "Hike 25 bp": 25}
    rows = [
        {"model": "m", "mode": "gated", "context": "public", "outcome": "Hike 25 bp", "answer": "Hike 25 bp"},
        {"model": "m", "mode": "gated", "context": "public", "outcome": "Cut 25 bp", "answer": "No change"},
        {"model": "baseline:uniform", "outcome": "No change", "probabilities": json.dumps({k: 1 / 3 for k in dirs})},
    ]
    d = direction_summary(rows, dirs, bp)
    assert d["m gated[public]"] == {"n": 2, "direction_accuracy": 0.5, "mae_bp": 12.5}
    assert d["baseline:uniform"]["direction_accuracy"] == pytest.approx(1 / 3, abs=1e-4)


# ------------------------------------------------------------------------------------------- runs


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


def test_dry_run_public_backtest(scenarios_dir, tmp_path, capsys):
    out = tmp_path / "runs" / "bt"
    rc = _cli(scenarios_dir, tmp_path, "run", "fed-quarterly", "--dry-run", "--context", "public", "--out", str(out))
    text = capsys.readouterr().out
    assert rc == 0 and "Epochal requests: 0 (public context)" not in text and "Dry run:" in text
    rows = _read(out / "predictions.csv")
    assert len(rows) == 43 and all(r["as_of"].endswith("-15T00:00:00Z") for r in rows)
    assert all(int(r["n_docs"]) == 7 for r in rows)
    assert "direction acc" in text and "Direction and size" in (out / "summary.md").read_text(encoding="utf-8")


def test_turning_points_estimate_is_ten_requests(scenarios_dir, tmp_path, capsys):
    rc = _cli(scenarios_dir, tmp_path, "run", "fed-quarterly", "--only", "turning-point", "--estimate")
    out = capsys.readouterr().out
    assert rc == 0 and "New requests needed: 10." in out


def test_live_forecast_then_score(scenarios_dir, tmp_path, capsys):
    runs = tmp_path / "runs"
    out = runs / "live1"
    rc = _cli(
        scenarios_dir, tmp_path, "run", "fed-quarterly", "--mode", "live", "--dry-run", "--context", "public",
        "--out", str(out),
    )  # fmt: skip
    assert rc == 0
    rows = _read(out / "forecast.csv")
    assert [r["event"] for r in rows] == ["fq_2026q4"]
    assert rows[0]["status"] == "pending" and rows[0]["outcome"] == "" and rows[0]["forecast_created"]
    assert not (out / "predictions.csv").exists()
    capsys.readouterr()
    # Still unresolved: score says so and writes nothing.
    assert _cli(scenarios_dir, tmp_path, "score", "--latest", "--runs-dir", str(runs)) == 1
    assert "still pending" in capsys.readouterr().out
    # Resolve it in a copy of the scenario (as a maintainer would after 31 Dec), then score.
    sdir = tmp_path / "scen"
    shutil.copytree(SCEN, sdir / "fed-quarterly")
    ev_path = sdir / "fed-quarterly" / "events.csv"
    evs = _read(ev_path)
    for r in evs:
        if r["id"] == "fq_2026q4":
            r["outcome"] = "No change"
    with open(ev_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(evs[0]))
        w.writeheader()
        w.writerows(evs)
    assert _cli(sdir, tmp_path, "score", str(out)) == 0
    assert _read(out / "forecast.csv")[0]["status"] == "resolved"
    pred = _read(out / "predictions.csv")
    assert pred[0]["outcome"] == "No change" and pred[0]["brier"] != ""
    # Nothing left to forecast in live mode once resolved.
    assert _cli(sdir, tmp_path, "run", "fed-quarterly", "--mode", "live", "--dry-run", "--context", "public") == 1
    assert "Nothing to forecast" in capsys.readouterr().err


def test_report_latest_picks_newest(scenarios_dir, tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    for name in ("a", "b"):
        _cli(scenarios_dir, tmp_path, "run", "fed-rates", "--dry-run", "--context", "public", "--limit", "1",
             "--out", str(runs / name))  # fmt: skip
    newest = runs / "b"
    import os

    os.utime(runs / "a" / "run.json", (1, 1))
    assert latest_run(runs) == newest
    capsys.readouterr()
    assert _cli(scenarios_dir, tmp_path, "report", "--latest", "--runs-dir", str(runs)) == 0
    assert f"Latest run: {newest}" in capsys.readouterr().out
    assert (newest / "report.html").is_file()
    assert _cli(scenarios_dir, tmp_path, "report", "--latest", "--runs-dir", str(tmp_path / "empty")) == 2
    assert _cli(scenarios_dir, tmp_path, "report", str(runs / "11_26_2026")) == 2
    assert "report --latest" in capsys.readouterr().err


def test_no_results_message_instead_of_empty_table(scenarios_dir, tmp_path, capsys, monkeypatch):
    def broken(model, prompt):
        raise ClaudeCLIError("OAuth session expired")

    monkeypatch.setattr(runner, "mock_transport", broken)
    out = tmp_path / "runs" / "empty"
    rc = _cli(scenarios_dir, tmp_path, "run", "fed-quarterly", "--dry-run", "--context", "public", "--out", str(out))
    text = capsys.readouterr().out
    assert rc == 1
    assert "No results: nothing was scored (claude-cli: OAuth session expired)" in text
    assert "acc" not in text.split("No results")[0].splitlines()[-1]  # no table header printed
    assert "No results" in (out / "summary.md").read_text(encoding="utf-8")
    assert "No results" in markdown({"scenario": "x"}, [], {}, [])


def test_prompt_shows_no_answer_columns(scenarios_dir, tmp_path, monkeypatch):
    seen = []
    real = runner.mock_transport

    def spy(model, prompt):
        seen.append(prompt)
        return real(model, prompt)

    monkeypatch.setattr(runner, "mock_transport", spy)
    rc = _cli(
        scenarios_dir, tmp_path, "run", "fed-quarterly", "--dry-run", "--context", "public", "--only", "fq_2020q1",
        "--modes", "gated,ungated", "--out", str(tmp_path / "o"),
    )  # fmt: skip
    assert rc == 0 and len(seen) == 2
    for p in seen:
        assert "1.50-1.75%" in p and "18 March 2020" in p  # range at T and the scheduled meeting
        assert "0.00-0.25" not in p and "-150" not in p  # the answer key never reaches the model
        assert "2020-03-03" not in p and "2020-03-15" not in p and "unscheduled:" not in p
        assert not re.search(r"\$[a-z_]+", p)  # every placeholder was filled
