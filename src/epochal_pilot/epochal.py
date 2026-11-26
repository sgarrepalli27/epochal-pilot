"""Epochal Time Portal client: date-gated web search and fetch.

API: POST https://api.epochal.com/v1/time-portal/{search,fetch}
Headers: ``x-api-key`` and ``Accept-Datetime`` (the "as of" moment; nothing later is returned).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources

from epochal_pilot.cache import Budget, Cache
from epochal_pilot.net import post_json

EPOCHAL_BASE = "https://api.epochal.com/v1/time-portal"

Transport = Callable[[str, str, dict], dict]


class Epochal:
    def __init__(self, cache: Cache, budget: Budget, api_key: str | None, transport: Transport | None = None):
        self.cache, self.budget, self._key = cache, budget, api_key
        self.transport = transport or self._http

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

    def call(self, endpoint: str, as_of: str, body: dict) -> dict:
        key = self.request_key(endpoint, as_of, body)
        hit = self.cache.get("epochal", key)
        if hit is not None:
            return hit
        self.budget.spend()  # counted before the call: conservative
        resp = self.transport(endpoint, as_of, body)
        self.cache.put("epochal", key, resp)
        return resp

    @staticmethod
    def search_body(query: str, n: int) -> dict:
        return {"query": query, "num_results": n, "include_text": True}

    @staticmethod
    def fetch_body(url: str, max_chars: int) -> dict:
        return {"url": url, "max_characters": max_chars}

    def search(self, query: str, as_of: str, n: int = 5) -> dict:
        return self.call("search", as_of, self.search_body(query, n))

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
