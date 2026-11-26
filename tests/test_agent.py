import json

import pytest

from epochal_pilot.agent import parse_forecast, parse_letter, retrieve
from epochal_pilot.cache import Budget, Cache
from epochal_pilot.epochal import Epochal

CH = ["Hold", "Cut 25 bp", "Cut 50 bp", "Hike 25 bp"]


@pytest.mark.parametrize(
    "text,want",
    [
        ("ANSWER: B", "B"),
        ("answer - (c)", "C"),
        ("Reasoning...\nANSWER: A", "A"),
        ("C", "C"),
        ("no idea", None),
        ("", None),
        ("I expect Cut 50 bp.", "C"),
        ("Hold or Cut 25 bp", None),
    ],
)
def test_parse_letter(text, want):
    assert parse_letter(text, CH) == want


def test_parse_forecast_json():
    reply = (
        "Sure!\n```json\n"
        '{"answer": "C", "probabilities": {"A": 0.1, "B": 0.3, "C": 0.6, "D": 0}, '
        '"citations": ["https://a.example"], "rationale": "x"}\n```'
    )
    fc = parse_forecast(reply, CH)
    assert fc.answer == "Cut 50 bp"
    assert fc.probabilities["Cut 50 bp"] == pytest.approx(0.6)
    assert sum(fc.probabilities.values()) == pytest.approx(1)
    assert fc.citations == ["https://a.example"]


def test_parse_forecast_percentages_and_names():
    fc = parse_forecast('{"answer": "A", "probabilities": {"Hold": 80, "Cut 25 bp": 20}}', CH)
    assert fc.probabilities["Hold"] == pytest.approx(0.8)


def test_parse_forecast_falls_back_to_one_hot():
    fc = parse_forecast("ANSWER: B", CH)
    assert fc.answer == "Cut 25 bp" and fc.probabilities["Cut 25 bp"] == 1.0 and fc.parse_mode == "text"


def test_parse_forecast_abstain_is_uniform():
    fc = parse_forecast("I cannot predict the future.", CH)
    assert fc.answer is None and fc.parse_mode == "abstain"
    assert all(p == pytest.approx(0.25) for p in fc.probabilities.values())


def _ep(tmp_path, results, fetch=None, per_run=10):
    calls = []

    def transport(endpoint, as_of, body):
        calls.append((endpoint, as_of, body))
        if endpoint == "search":
            return {"results": results}
        return fetch or {"status": "error"}

    ep = Epochal(Cache(tmp_path), Budget(tmp_path / "b.json", per_run, 100), "k", transport=transport)
    return ep, calls


def test_retrieve_drops_documents_after_as_of(tmp_path):
    long = "x" * 400
    results = [
        {"title": "before", "url": "https://a", "published_at": "2024-09-10T15:00:00Z", "text": long},
        {"title": "after", "url": "https://b", "published_at": "2024-09-19T00:00:00Z", "text": "RESULT"},
        {"title": "undated", "url": "https://c", "published_at": None, "text": long},
    ]
    ep, calls = _ep(tmp_path, results)
    r = retrieve(ep, ["q"], "2024-09-11T00:00:00Z", 5, 2)
    assert [d["title"] for d in r.docs] == ["before", "undated"]
    assert r.dropped_future == 1 and r.undated == 1
    assert "RESULT" not in r.context()
    assert len(calls) == 1  # long texts need no fetch


def test_retrieve_fetches_thin_results_and_checks_snapshot_date(tmp_path):
    results = [{"title": "thin", "url": "https://a", "published_at": None, "snippet": "s"}]
    ep, calls = _ep(tmp_path, results, fetch={"status": "success", "as_of": "2030-01-01T00:00:00Z", "content": "LEAK"})
    r = retrieve(ep, ["q"], "2024-09-11T00:00:00Z", 5, 2)
    assert [c[0] for c in calls] == ["search", "fetch"]
    assert r.dropped_future == 1 and "LEAK" not in r.context()
    # cached on rerun: no new calls
    retrieve(ep, ["q"], "2024-09-11T00:00:00Z", 5, 2)
    assert len(calls) == 2


def test_cache_key_matches_original_probe_format(tmp_path):
    """Searches cached by the original future-leak probe stay reusable (same key scheme)."""
    body = Epochal.search_body("q", 5)
    assert body == {"query": "q", "num_results": 5, "include_text": True}
    assert Epochal.request_key("search", "t", body) == Cache.key("search", "t", body)
    json.dumps(body)
