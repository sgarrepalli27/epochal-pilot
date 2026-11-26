"""Official OpenAI and Gemini Decisions API clients: mocked HTTP only."""

import json

import pytest

from epochal_pilot.cli import main
from epochal_pilot.decisions import (
    GEMINI_DECISIONS_MODEL,
    GEMINI_DECISIONS_URL,
    OPENAI_DECISIONS_MODEL,
    OPENAI_DECISIONS_URL,
    DecisionsClient,
    GeminiDecisionsPredictor,
    OpenAIDecisionsPredictor,
    mock_decisions_transport,
    parse_choice,
)
from epochal_pilot.jev import ATOMIC_NOULS, FORECAST_QUESTION
from epochal_pilot.net import HTTPError
from epochal_pilot.providers import LLM_AS_SYSTEM_ONE
from epochal_pilot.structured import OpenAIPredictor, mock_structured_transport

OPTIONS = [
    "Cut 100+ bp",
    "Cut 75 bp",
    "Cut 50 bp",
    "Cut 25 bp",
    "No change",
    "Hike 25 bp",
    "Hike 50 bp",
    "Hike 75+ bp",
]


def _openai_body(url, body, headers, bucket):
    bucket.append({"url": url, "body": body, "headers": dict(headers)})
    return mock_decisions_transport(url, body, headers)


def test_openai_decisions_request_shape():
    seen = []
    client = DecisionsClient("openai", "test-key", transport=lambda u, b, h: _openai_body(u, b, h, seen))
    state = {"as_of": "2022-02-15", "rule_analysis": {"bucket": "Hike 25 bp"}}
    raw = client.decide(model=OPENAI_DECISIONS_MODEL, state=state, options=OPTIONS)
    assert len(seen) == 1
    req = seen[0]
    assert req["url"] == OPENAI_DECISIONS_URL
    assert "test-key" not in req["url"]
    assert req["headers"]["Authorization"] == "Bearer test-key"
    body = req["body"]
    assert body["model"] == "gpt-6-luna"
    assert isinstance(body["questions"], list)
    forecast = next(q for q in body["questions"] if q["name"] == FORECAST_QUESTION)
    assert forecast["type"] == "choice"
    assert [c["value"] for c in forecast["choices"]] == OPTIONS
    assert all("value" in c and "description" in c for c in forecast["choices"])
    predicates = [q for q in body["questions"] if q["type"] == "predicate"]
    assert {q["name"] for q in predicates} == set(ATOMIC_NOULS)
    # dict state is sent as official input_json
    content = body["input"][0]["content"]
    types = [part["type"] for part in content]
    assert "input_text" in types and "input_json" in types
    data = next(p["data"] for p in content if p["type"] == "input_json")
    assert data["as_of"] == "2022-02-15" and "rule_analysis" in data
    # mock response is official-shaped
    assert isinstance(raw["answers"], list)
    ans = next(a for a in raw["answers"] if a["name"] == FORECAST_QUESTION)
    assert ans["type"] == "choice" and ans["choice"] == "No change"
    assert all("value" in p and "probability" in p for p in ans["probabilities"])

    pred = OpenAIDecisionsPredictor("test-key", "gpt-6-luna", transport=lambda u, b, h: _openai_body(u, b, h, []))
    result = pred.predict(state, "What will the FOMC do?", OPTIONS)
    assert result.answer == "No change"
    assert result.probabilities["No change"] == 1.0
    assert result.metadata["provider"] == "openai-decisions"
    assert result.metadata["calibrated_claim"] is False
    assert "test-key" not in repr(client) and "test-key" not in repr(pred)


def test_gemini_decisions_request_shape():
    seen = []
    client = DecisionsClient("gemini", "test-key", transport=lambda u, b, h: _openai_body(u, b, h, seen))
    state = {"as_of": "2022-02-15"}
    raw = client.decide(model=GEMINI_DECISIONS_MODEL, state=state, options=OPTIONS)
    req = seen[0]
    assert req["url"] == GEMINI_DECISIONS_URL
    assert "test-key" not in req["url"]
    assert "?" not in req["url"]
    assert req["headers"]["x-goog-api-key"] == "test-key"
    assert "Authorization" not in req["headers"]
    body = req["body"]
    assert body["model"] == "gemini-decision-1"
    assert isinstance(body["questions"], dict)
    forecast = body["questions"][FORECAST_QUESTION]
    assert forecast["type"] == "choice"
    assert set(forecast["criteria"]) == set(OPTIONS)
    nouls = {n for n, q in body["questions"].items() if q["type"] == "noul"}
    assert nouls == set(ATOMIC_NOULS)
    assert json.loads(body["input"]) == state
    assert raw["answers"][FORECAST_QUESTION]["choice"] == "No change"
    assert raw["usage"]["total_output_tokens"] == 0

    pred = GeminiDecisionsPredictor("test-key", "gemini-decision-1", transport=mock_decisions_transport)
    result = pred.predict(state, "q", OPTIONS)
    assert result.answer == "No change"
    assert result.metadata["provider"] == "gemini-decisions"
    assert result.metadata["calibrated_claim"] is True
    assert "test-key" not in repr(client)


def test_openai_refusal_raises():
    raw = {"answers": [{"type": "refusal", "name": FORECAST_QUESTION, "refusal": "can't"}]}
    with pytest.raises(HTTPError, match="refused"):
        parse_choice(raw, OPTIONS, flavor="openai")


def test_gemini_refusal_raises():
    raw = {"answers": {FORECAST_QUESTION: {"type": "refusal"}}}
    with pytest.raises(HTTPError, match="refused"):
        parse_choice(raw, OPTIONS, flavor="gemini")


def test_unknown_flavor_rejected():
    with pytest.raises(ValueError, match="unknown decisions flavor"):
        DecisionsClient("not-a-flavor", "test-key")


def test_structured_openai_is_labeled_fallback():
    pred = OpenAIPredictor("test-key", "gpt-4o-mini", transport=mock_structured_transport)
    result = pred.predict({"as_of": "2022-02-15"}, "q", OPTIONS)
    assert result.answer
    label = (result.metadata.get("label") or "") + (result.rationale or "") + json.dumps(result.metadata)
    assert "not calibration-trained" in label or LLM_AS_SYSTEM_ONE in label
    assert result.metadata.get("provider") == "openai"


def test_dry_run_openai_decisions_with_rules_includes_analysis(scenarios_dir, tmp_path, capsys):
    out = tmp_path / "od"
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-quarterly",
            "--backend",
            "openai-decisions",
            "--with-rules",
            "--dry-run",
            "--only",
            "fq_2022q1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    text = capsys.readouterr().out + capsys.readouterr().err
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["pipeline"] == "openai-decisions"
    assert meta["with_rules"] is True
    assert meta["model_params"]["endpoint"] == OPENAI_DECISIONS_URL
    assert "rule_signals" in (out / "predictions.csv").read_text(encoding="utf-8")
    blob = (out / "run.json").read_text(encoding="utf-8") + text
    assert "OPENAI_API_KEY" not in blob or "is not set" in text  # name of the var is ok
    # never a secret value (none was provided in dry-run)
    assert "test-key" not in blob
    assert "azure" not in blob.lower()


def test_dry_run_gemini_decisions_and_jev_rules_still_includes_analysis(scenarios_dir, tmp_path, capsys):
    out = tmp_path / "gd"
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-quarterly",
            "--backend",
            "gemini-decisions",
            "--dry-run",
            "--only",
            "fq_2022q1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["pipeline"] == "gemini-decisions"
    assert meta["model_params"]["model"] == "gemini-decision-1"
    assert meta["model_params"]["calibrated_claim"] is True
    capsys.readouterr()

    out2 = tmp_path / "jr"
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-quarterly",
            "--backend",
            "jev-rules",
            "--dry-run",
            "--only",
            "fq_2022q1",
            "--out",
            str(out2),
        ]
    )
    assert rc == 0
    pred = (out2 / "predictions.csv").read_text(encoding="utf-8")
    assert "rule_signals" in pred
    meta = json.loads((out2 / "run.json").read_text(encoding="utf-8"))
    assert meta["with_rules"] is True and meta["pipeline"] == "jev-rules"


def test_missing_openai_key_exits_cleanly(scenarios_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-quarterly",
            "--backend",
            "openai-decisions",
            "--only",
            "fq_2022q1",
            "--out",
            str(tmp_path / "x"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2 and "OPENAI_API_KEY is not set" in err
    assert "test-key" not in err
