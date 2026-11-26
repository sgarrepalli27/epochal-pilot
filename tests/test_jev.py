"""TypeSafe Jev backend: request shape, mocked HTTP only, no secrets in output."""

import json
from datetime import date
from pathlib import Path

import pytest

import epochal_pilot.jev as jev_mod
import epochal_pilot.runner as runner_mod
from epochal_pilot.cache import Cache
from epochal_pilot.cli import main
from epochal_pilot.jev import (
    SYSTEMONE_URL,
    JevClient,
    build_state,
    cost_usd,
    forecast_from_choice,
    forecast_questions,
    mock_jev_transport,
)
from epochal_pilot.net import HTTPError
from epochal_pilot.report import html_report, markdown, probability_timeline_svg
from epochal_pilot.rules import RULES_TEXT, RuleSignals

ROOT = Path(__file__).resolve().parents[1]
SECRET = "ts-test-secret-do-not-leak"


def _cli(scenarios_dir, tmp_path, *args):
    return main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            *args,
        ]
    )


def _capture_transport(monkeypatch, inner=mock_jev_transport):
    captured = []

    def wrap(url, body, headers):
        captured.append({"url": url, "body": body, "headers": dict(headers or {})})
        return inner(url, body, headers)

    monkeypatch.setattr(jev_mod, "mock_jev_transport", wrap)
    monkeypatch.setattr(runner_mod, "mock_jev_transport", wrap)
    return captured


def test_system_one_request_shape_and_choice_parse(tmp_path, monkeypatch):
    captured = []

    def transport(url, body, headers):
        captured.append({"url": url, "body": body, "headers": dict(headers)})
        return {
            "model": "jev-1.14.0",
            "answers": {
                "forecast": {
                    "type": "choice",
                    "choice": "Hike 25 bp",
                    "confidence": 0.62,
                    "probabilities": {
                        "Cut 100+ bp": 0.0,
                        "Cut 75 bp": 0.0,
                        "Cut 50 bp": 0.0,
                        "Cut 25 bp": 0.05,
                        "No change": 0.2,
                        "Hike 25 bp": 0.6,
                        "Hike 50 bp": 0.15,
                        "Hike 75+ bp": 0.0,
                    },
                },
                "statement_hawkish": {"type": "noul", "noul": 0.8},
            },
            "usage": {"input_tokens": 407, "output_tokens": 73},
        }

    client = JevClient(SECRET, cache=Cache(tmp_path), transport=transport)
    choices = [
        "Cut 100+ bp",
        "Cut 75 bp",
        "Cut 50 bp",
        "Cut 25 bp",
        "No change",
        "Hike 25 bp",
        "Hike 50 bp",
        "Hike 75+ bp",
    ]
    state = {"as_of": "2022-02-15", "question": "net change?"}
    resp = client.system_one(model="jev-latest", state=state, questions=forecast_questions(choices))
    assert captured[0]["url"] == SYSTEMONE_URL
    assert captured[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = captured[0]["body"]
    assert body["model"] == "jev-latest"
    assert body["state"] == state
    assert body["questions"]["forecast"]["type"] == "choice"
    assert "Hike 25 bp" in body["questions"]["forecast"]["criteria"]
    fc = forecast_from_choice(resp, choices)
    assert fc.answer == "Hike 25 bp" and fc.parse_mode == "jev"
    assert fc.probabilities["Hike 25 bp"] == pytest.approx(0.6)
    assert SECRET not in repr(client)
    assert SECRET not in fc.raw and SECRET not in fc.rationale
    # cache stores the response, never the key
    cached = list((tmp_path).rglob("*"))
    for path in cached:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert SECRET not in text


def test_429_retries_then_succeeds(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(jev_mod.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def transport(url, body, headers):
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError(429, "HTTP 429 from https://api.typesafe.ai/v1/systemone")
        return mock_jev_transport(url, body, headers)

    client = JevClient("k", cache=Cache(tmp_path), transport=transport)
    resp = client.system_one(
        model="jev-latest", state={"as_of": "2022-02-15"}, questions=forecast_questions(["No change"])
    )
    assert resp.answers["forecast"]["choice"] == "No change"
    assert calls["n"] == 3 and sleeps == [1.0, 2.0]


def test_cost_and_repr_hide_key():
    assert cost_usd(1_000_000) == pytest.approx(0.042)
    assert "ts-secret" not in repr(JevClient("ts-secret"))
    assert "missing" in repr(JevClient(None))


def test_build_state_omits_docs_and_rules_when_asked():
    docs = [{"title": "Statement", "published_at": "2022-01-26", "url": "https://example", "text": "hike soon"}]
    sig = RuleSignals(as_of=date(2022, 2, 15), target=date(2022, 3, 31))
    sig.notes = ["demo"]
    full = build_state(
        as_of="2022-02-15",
        target="2022-03-31",
        range_at_asof="0-0.25%",
        question="q",
        meetings="one meeting",
        derived_facts="SEP gap +77.5 bp",
        documents=docs,
        signals=sig,
        include_docs=True,
        include_rules=True,
    )
    assert "documents" in full and "derived_facts" in full
    assert full["rule_analysis"] and full["business_rules"] == RULES_TEXT
    blind = build_state(
        as_of="2022-02-15",
        target="2022-03-31",
        range_at_asof="0-0.25%",
        question="q",
        meetings="one meeting",
        derived_facts="SEP gap +77.5 bp",
        documents=docs,
        signals=sig,
        include_docs=False,
        include_rules=False,
    )
    assert "documents" not in blind and "derived_facts" not in blind
    assert "rule_analysis" not in blind and "business_rules" not in blind
    assert blind["as_of"] == "2022-02-15" and blind["policy_range"] == "0-0.25%"


def test_missing_typesafe_key(scenarios_dir, tmp_path, capsys):
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "jev",
        "--only",
        "fq_2022q1",
    )
    err = capsys.readouterr().err
    assert rc == 2 and "TYPESAFE_API_KEY is not set" in err
    assert SECRET not in err
    assert "EPOCHAL_API_KEY is not set" not in err  # jev defaults to public context


def test_dry_run_jev_rules_includes_analysis_jev_does_not(scenarios_dir, tmp_path, monkeypatch, capsys):
    captured = _capture_transport(monkeypatch)
    out = tmp_path / "jev-rules"
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "jev-rules",
        "--only",
        "fq_2022q1",
        "--dry-run",
        "--out",
        str(out),
    )
    text = capsys.readouterr().out
    assert rc == 0 and "context=public" in text
    state = captured[-1]["body"]["state"]
    assert "rule_analysis" in state and "business_rules" in state
    assert "documents" in state
    assert state["rule_analysis"]["rules_point_estimate"]["nearest_bucket"] == "Hike 25 bp"
    # no typessafe key used in dry-run
    assert "Bearer " in str(captured[-1]["headers"].get("Authorization", "Bearer "))
    assert SECRET not in json.dumps(captured[-1])
    manifest = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert manifest["pipeline"] == "jev-rules"
    blob = (out / "run.json").read_text(encoding="utf-8") + (out / "predictions.csv").read_text(encoding="utf-8")
    assert SECRET not in blob and "TYPESAFE_API_KEY=" not in blob

    captured.clear()
    out2 = tmp_path / "jev-only"
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "jev",
        "--only",
        "fq_2022q1",
        "--dry-run",
        "--out",
        str(out2),
    )
    assert rc == 0
    state = captured[-1]["body"]["state"]
    assert "rule_analysis" not in state and "business_rules" not in state
    assert "documents" in state
    assert capsys.readouterr().out  # drain


def test_blind_omits_docs_derived_and_rules(scenarios_dir, tmp_path, monkeypatch, capsys):
    captured = _capture_transport(monkeypatch)
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "jev-rules",
        "--only",
        "fq_2022q1",
        "--no-context",
        "--dry-run",
        "--out",
        str(tmp_path / "blind"),
    )
    text = capsys.readouterr().out
    assert rc == 0 and "context=blind" in text
    state = captured[-1]["body"]["state"]
    assert "documents" not in state
    assert "derived_facts" not in state
    assert "rule_analysis" not in state
    assert "business_rules" not in state
    assert state["as_of"].startswith("2022-02-15")


def test_mask_dates_rewrites_years_in_state(scenarios_dir, tmp_path, monkeypatch, capsys):
    captured = _capture_transport(monkeypatch)
    rc = _cli(
        scenarios_dir,
        tmp_path,
        "run",
        "fed-quarterly",
        "--backend",
        "jev",
        "--only",
        "fq_2022q1",
        "--mask-dates",
        "--dry-run",
        "--out",
        str(tmp_path / "mask"),
    )
    assert rc == 0
    capsys.readouterr()
    state = captured[-1]["body"]["state"]
    blob = json.dumps(state)
    assert "2022" not in blob and "2021" not in blob
    assert "Year 0" in blob


def test_rules_public_dry_run_is_deterministic(scenarios_dir, tmp_path, capsys):
    outs = []
    for i in range(2):
        dest = tmp_path / f"rules-{i}"
        rc = _cli(
            scenarios_dir,
            tmp_path,
            "run",
            "fed-quarterly",
            "--backend",
            "rules",
            "--only",
            "fq_2022q1",
            "--dry-run",
            "--out",
            str(dest),
        )
        assert rc == 0
        capsys.readouterr()
        rows = (dest / "predictions.csv").read_text(encoding="utf-8")
        outs.append(rows)
        assert "Hike 25 bp" in rows
    assert outs[0] == outs[1]
    html = (tmp_path / "rules-0" / "report.html").read_text(encoding="utf-8")
    assert "Predicted probability of the actual outcome over time" in html
    assert "<svg" in html and "Calibration" in html and "<script" not in html


def test_report_embeds_timeline_and_calibration_svg():
    rows = [
        {
            "event": "fq_2022q1",
            "event_date": "2022-03-31",
            "as_of": "2022-02-15",
            "model": "jev-latest",
            "mode": "gated",
            "p_outcome": "0.6",
            "correct": "1",
            "outcome": "Hike 25 bp",
            "answer": "Hike 25 bp",
            "probabilities": '{"Hike 25 bp": 0.6, "No change": 0.4}',
        },
        {
            "event": "fq_2022q2",
            "event_date": "2022-06-30",
            "as_of": "2022-05-15",
            "model": "jev-latest",
            "mode": "gated",
            "p_outcome": "0.2",
            "correct": "0",
            "outcome": "Hike 75+ bp",
            "answer": "Hike 25 bp",
            "probabilities": '{"Hike 25 bp": 0.8, "Hike 75+ bp": 0.2}',
        },
    ]
    svg = probability_timeline_svg(rows)
    assert svg.startswith("<svg") and "16a34a" in svg and "dc2626" in svg
    scores = [{"model": "jev-latest", "mode": "gated", "context": "public", "n": 2, "accuracy": 0.5, "brier": 0.2}]
    html = html_report({"scenario": "fed-quarterly"}, scores, {"groups": {}}, rows, title="demo")
    assert "Predicted probability of the actual outcome over time" in html
    assert "<svg" in html and "Calibration" in html
    assert "<script" not in html
    md = markdown({"scenario": "fed-quarterly"}, scores, {"groups": {}}, rows)
    assert "See `report.html` for the SVG" in md


def test_check_never_prints_key_value(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    rc = _cli(scenarios_dir, tmp_path, "check")
    out = capsys.readouterr().out
    assert rc == 0 and "TYPESAFE_API_KEY" in out and "set" in out
    assert SECRET not in out
