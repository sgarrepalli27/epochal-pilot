"""Epochal Time Portal client: date-gated web search and fetch.

API: POST https://api.epochal.com/v1/time-portal/{search,fetch}
Headers: ``x-api-key`` and ``Accept-Datetime`` (the "as of" moment; nothing later is returned).

Metering (docs.epochal.com, Pricing/FAQ): only 2xx responses count against the free quota. A fetch that
returns 200 with ``status: "error"`` (no archived copy) still counts. Errors (4xx/5xx) do not, so the
local budget refunds them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import resources

from epochal_pilot.cache import Budget, Cache
from epochal_pilot.net import HTTPError, post_json

EPOCHAL_BASE = "https://api.epochal.com/v1/time-portal"
MAX_NUM_RESULTS = 10  # API maximum; 10 results cost the same single request as 1
CATEGORIES = {"news", "finance", "legal", "publications", "companies", "people", "maps"}

Transport = Callable[[str, str, dict], dict]


@dataclass
class SearchOptions:
    """Search filters from scenario.json (all optional). See docs.epochal.com Search reference."""

    num_results: int = MAX_NUM_RESULTS
    categories: list[str] = field(default_factory=list)
    published_after_days: int | None = 30  # window before as-of; None = no lower bound
    domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errs = []
        if not 1 <= self.num_results <= MAX_NUM_RESULTS:
            errs.append(f"num_results must be 1-{MAX_NUM_RESULTS}")
        bad = set(self.categories) - CATEGORIES
        if bad:
            errs.append(f"unknown categories {sorted(bad)} (allowed: {sorted(CATEGORIES)})")
        if set(self.domains) & set(self.exclude_domains):
            errs.append("domains and exclude_domains overlap")
        if len(self.domains) + len(self.exclude_domains) > 200:
            errs.append("domains + exclude_domains: at most 200 entries")
        if self.published_after_days is not None and self.published_after_days < 1:
            errs.append("published_after_days must be >= 1 or null")
        return errs


@dataclass
class Ledger:
    """What this run spent and what Epochal said about it (kept locally in run.json, never shared)."""

    searches_new: int = 0
    fetches_new: int = 0
    cache_hits: int = 0
    errors_refunded: int = 0
    fetch_page_errors: int = 0  # 200 + status=error: counted by Epochal
    request_ids: list[str] = field(default_factory=list)
    copy_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "searches_new": self.searches_new,
            "fetches_new": self.fetches_new,
            "counted_2xx": self.searches_new + self.fetches_new,
            "cache_hits": self.cache_hits,
            "errors_refunded": self.errors_refunded,
            "fetch_page_errors": self.fetch_page_errors,
            "request_ids": self.request_ids,
            "copy_ids": self.copy_ids,
        }


class Epochal:
    def __init__(self, cache: Cache, budget: Budget, api_key: str | None, transport: Transport | None = None):
        self.cache, self.budget, self._key = cache, budget, api_key
        self.transport = transport or self._http
        self.ledger = Ledger()

    def __repr__(self) -> str:  # never show the key
        return f"Epochal(cache={self.cache.root}, key={'set' if self._key else 'not set'})"

    def _http(self, endpoint: str, as_of: str, body: dict) -> dict:
        return post_json(
            f"{EPOCHAL_BASE}/{endpoint}",
            body,
            headers={"x-api-key": self._key or "", "Accept-Datetime": as_of},
            timeout=90,
        )

    @staticmethod
    def request_key(endpoint: str, as_of: str, body: dict) -> str:
        return Cache.key(endpoint, as_of, body)

    def is_cached(self, endpoint: str, as_of: str, body: dict) -> bool:
        return self.cache.has("epochal", self.request_key(endpoint, as_of, body))

    def cached(self, endpoint: str, as_of: str, body: dict) -> dict | None:
        return self.cache.get("epochal", self.request_key(endpoint, as_of, body))

    def call(self, endpoint: str, as_of: str, body: dict) -> dict:
        key = self.request_key(endpoint, as_of, body)
        hit = self.cache.get("epochal", key)
        if hit is not None:
            self.ledger.cache_hits += 1
            return hit
        self.budget.spend()  # reserve before the call so a crash can never overspend
        try:
            resp = self.transport(endpoint, as_of, body)
        except HTTPError:
            self.budget.refund()  # non-2xx responses are not billed by Epochal
            self.ledger.errors_refunded += 1
            raise
        if endpoint == "search":
            self.ledger.searches_new += 1
        else:
            self.ledger.fetches_new += 1
            if resp.get("status") != "success":
                self.ledger.fetch_page_errors += 1
            if resp.get("id"):
                self.ledger.copy_ids.append(str(resp["id"]))
        if resp.get("request_id"):
            self.ledger.request_ids.append(str(resp["request_id"]))
        self.cache.put("epochal", key, resp)
        return resp

    @staticmethod
    def search_body(query: str, as_of: str, opts: SearchOptions | None = None) -> dict:
        opts = opts or SearchOptions()
        body: dict = {"query": query, "num_results": opts.num_results, "include_text": True}
        if opts.categories:
            body["categories"] = list(opts.categories)
        if opts.domains:
            body["domains"] = list(opts.domains)
        if opts.exclude_domains:
            body["exclude_domains"] = list(opts.exclude_domains)
        if opts.published_after_days:
            from epochal_pilot.agent import parse_ts

            gate = parse_ts(as_of)
            if gate is not None:
                after = gate - timedelta(days=opts.published_after_days)
                body["published_after"] = after.strftime("%Y-%m-%dT%H:%M:%SZ")
        return body

    @staticmethod
    def fetch_body(url: str, max_chars: int) -> dict:
        return {"url": url, "max_characters": max_chars}

    def search(self, query: str, as_of: str, opts: SearchOptions | None = None) -> dict:
        return self.call("search", as_of, self.search_body(query, as_of, opts))

    def fetch(self, url: str, as_of: str, max_chars: int = 3000) -> dict:
        return self.call("fetch", as_of, self.fetch_body(url, max_chars))


def fixture_transport(endpoint: str, as_of: str, body: dict) -> dict:
    """Offline stand-in for the API (used by --dry-run and tests)."""
    name = "epochal_search.json" if endpoint == "search" else "epochal_fetch.json"
    data = json.loads(resources.files("epochal_pilot.fixtures").joinpath(name).read_text(encoding="utf-8"))
    data["as_of"] = as_of
    if endpoint == "fetch":
        data["url"] = body.get("url", "")
    return data
