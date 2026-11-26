import pytest

from epochal_pilot.agent import retrieve
from epochal_pilot.cache import Budget, Cache
from epochal_pilot.epochal import Epochal, SearchOptions
from epochal_pilot.net import HTTPError
from epochal_pilot.runner import estimate
from epochal_pilot.scenario import load_scenario


def test_errors_are_refunded_and_page_errors_counted(tmp_path):
    path = tmp_path / "b.json"

    def transport(endpoint, as_of, body):
        if endpoint == "search":
            raise HTTPError(429, "HTTP 429 rate_limit_exceeded")
        return {"status": "error", "page_error": {"http_status_code": None}, "request_id": "r1"}

    ep = Epochal(Cache(tmp_path), Budget(path, per_run=5, max_total=100), "k", transport=transport)
    with pytest.raises(HTTPError):
        ep.search("q", "2024-01-01T00:00:00Z")
    assert ep.budget.this_run == 0 and ep.budget.total == 0 and ep.ledger.errors_refunded == 1
    ep.fetch("https://a", "2024-01-01T00:00:00Z")  # 200 + status=error: Epochal bills it
    assert ep.budget.this_run == 1 and ep.ledger.fetch_page_errors == 1
    assert Budget(path, 5, 100).total == 1


def test_retrieve_survives_search_error(tmp_path):
    def transport(endpoint, as_of, body):
        raise HTTPError(500, "HTTP 500")

    ep = Epochal(Cache(tmp_path), Budget(tmp_path / "b.json", 5, 100), "k", transport=transport)
    r = retrieve(ep, ["q1", "q2"], "2024-01-01T00:00:00Z", None, 2)
    assert r.search_failures == 2 and ep.budget.total == 0


def test_estimate_counts_fetches_exactly_when_search_cached(scenarios_dir, tmp_path):
    sc = load_scenario(scenarios_dir, "fed-rates")
    evs = sc.events[:2]
    opts = SearchOptions()
    ep = Epochal(Cache(tmp_path), Budget(tmp_path / "b.json", 5, 100), "k")
    est = estimate(ep, evs, 7, opts, 2, plan_queries=1, n_models=2)
    assert est["searches_new"] == 2 and est["fetches_new_max"] == 4 and est["planned_queries_max"] == 4
    assert est["total_min"] == 2 and est["total_max"] == 2 + 4 + 4 + 8
    # Cache a search for event 0 with one thin and one long result.
    as_of = evs[0].as_of(7)
    body = Epochal.search_body(evs[0].search_query, as_of, opts)
    ep.cache.put(
        "epochal",
        Epochal.request_key("search", as_of, body),
        {
            "results": [
                {"url": "https://thin", "published_at": None, "snippet": "s"},
                {"url": "https://long", "published_at": None, "text": "x" * 500},
            ]
        },
    )
    est = estimate(ep, evs, 7, opts, 2)
    assert est["searches_cached"] == 1 and est["searches_new"] == 1
    assert est["fetches_new_min"] == 1 and est["fetches_new_max"] == 3
