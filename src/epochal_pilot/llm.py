"""Chat backends: OpenRouter (default), Anthropic Claude API, and Ollama (local). Responses are cached."""

from __future__ import annotations

import os
from collections.abc import Callable

from epochal_pilot.cache import Cache
from epochal_pilot.net import post_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

Transport = Callable[[str, str], str]


class LLM:
    def __init__(
        self,
        cache: Cache,
        backend: str = "openrouter",
        api_key: str | None = None,
        transport: Transport | None = None,
        max_tokens: int = 600,
    ):
        self.cache, self.backend, self._key, self.max_tokens = cache, backend, api_key, max_tokens
        self.ollama_url = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
        backends = {"ollama": self._ollama, "anthropic": self._anthropic, "openrouter": self._openrouter}
        self.transport = transport or backends.get(backend, self._openrouter)

    def __repr__(self) -> str:
        return f"LLM(backend={self.backend}, key={'set' if self._key else 'not set'})"

    def _openrouter(self, model: str, prompt: str) -> str:
        r = post_json(
            OPENROUTER_URL,
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self.max_tokens,
            },
            headers={
                "Authorization": f"Bearer {self._key}",
                "X-Title": "epochal-pilot agent backtest harness",
            },
            timeout=180,
        )
        return (r.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    def _anthropic(self, model: str, prompt: str) -> str:
        # Current Claude models think adaptively; thinking uses output tokens, so allow headroom.
        r = post_json(
            ANTHROPIC_URL,
            {
                "model": model,
                "max_tokens": max(self.max_tokens, 4000),
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={"x-api-key": self._key or "", "anthropic-version": ANTHROPIC_VERSION},
            timeout=300,
        )
        blocks = r.get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")

    def _ollama(self, model: str, prompt: str) -> str:
        r = post_json(
            f"{self.ollama_url}/api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": self.max_tokens},
            },
            timeout=900,
        )
        return r.get("message", {}).get("content") or ""

    def ask(self, model: str, prompt: str) -> str:
        key = Cache.key(self.backend, model, prompt, self.max_tokens)
        hit = self.cache.get("llm", key)
        if hit is not None:
            return hit["text"]
        text = self.transport(model, prompt)
        if text.strip():  # never cache an empty reply
            self.cache.put("llm", key, {"model": model, "text": text})
        return text


def mock_transport(model: str, prompt: str) -> str:
    """Deterministic offline model for --dry-run: picks option A with 60% confidence."""
    import re

    letters = re.findall(r"^([A-H])\. ", prompt, re.MULTILINE)
    letters = letters or ["A", "B"]
    rest = (1 - 0.6) / max(1, len(letters) - 1)
    probs = ", ".join(f'"{x}": {0.6 if x == letters[0] else round(rest, 4)}' for x in letters)
    url = re.search(r"\) (https?://\S+)", prompt)
    cite = f'"{url.group(1)}"' if url else ""
    return f'{{"answer": "{letters[0]}", "probabilities": {{{probs}}}, "citations": [{cite}], "rationale": "mock"}}'
