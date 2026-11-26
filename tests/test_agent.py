import json

import pytest

from epochal_pilot.agent import parse_forecast, parse_letter, retrieve
from epochal_pilot.cache import Budget, Cache
from epochal_pilot.epochal import Epochal, SearchOptions

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
    r = retrieve(ep, ["q"], "2024-09-11T00:00:00Z", None, 2)
    assert [d["title"] for d in r.docs] == ["before", "undated"]
    assert r.dropped_future == 1 and r.undated == 1
    assert "RESULT" not in r.context()
    assert len(calls) == 1  # long texts need no fetch


def test_retrieve_fetch_date_check_uses_published_at_not_echoed_as_of(tmp_path):
    results = [{"title": "thin", "url": "https://a", "published_at": None, "snippet": "s"}]
    # The echoed as_of is irrelevant; the copy's own published_at says it is from after the gate.
    fetch = {
        "status": "success",
        "as_of": "2024-09-11T00:00:00Z",
        "published_at": "2030-01-01T00:00:00Z",
        "content": "LEAK",
        "id": "copy1",
        "request_id": "r2",
    }
    ep, calls = _ep(tmp_path, results, fetch=fetch)
    r = retrieve(ep, ["q"], "2024-09-11T00:00:00Z", None, 2)
    assert [c[0] for c in calls] == ["search", "fetch"]
    assert r.dropped_future == 1 and "LEAK" not in r.context()
    # cached on rerun: no new calls
    retrieve(ep, ["q"], "2024-09-11T00:00:00Z", None, 2)
    assert len(calls) == 2
    assert ep.ledger.copy_ids == ["copy1"] and ep.ledger.request_ids == ["r2"]


def test_retrieve_keeps_fetch_when_published_before_gate(tmp_path):
    results = [{"title": "thin", "url": "https://a", "published_at": None, "snippet": "s"}]
    fetch = {
        "status": "success",
        "as_of": "2099-01-01T00:00:00Z",
        "published_at": "2024-09-01T00:00:00Z",
        "content": "FULL TEXT",
    }
    ep, _ = _ep(tmp_path, results, fetch=fetch)
    r = retrieve(ep, ["q"], "2024-09-11T00:00:00Z", None, 2)
    assert r.dropped_future == 0 and "FULL TEXT" in r.context()
    assert r.docs[0]["published_at"] == "2024-09-01T00:00:00Z"


def test_search_body_filters():
    opts = SearchOptions(num_results=10, categories=["news"], published_after_days=30, domains=["reuters.com"])
    body = Epochal.search_body("q", "2024-09-11T00:00:00Z", opts)
    assert body == {
        "query": "q",
        "num_results": 10,
        "include_text": True,
        "categories": ["news"],
        "domains": ["reuters.com"],
        "published_after": "2024-08-12T00:00:00Z",
    }
    assert "published_after" not in Epochal.search_body(
        "q", "2024-09-11T00:00:00Z", SearchOptions(published_after_days=None)
    )
    assert SearchOptions(categories=["blogs"]).validate()
    assert SearchOptions(domains=["a.com"], exclude_domains=["a.com"]).validate()
    assert not SearchOptions().validate()
    assert Epochal.request_key("search", "t", body) == Cache.key("search", "t", body)
    json.dumps(body)
