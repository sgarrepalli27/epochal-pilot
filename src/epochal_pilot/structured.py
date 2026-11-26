"""Schema-locked System One adapters for OpenAI and Gemini (fallback, not calibration-trained).

These are "LLM as System One (not calibration-trained)": the output is pinned to a
JSON schema (answer enum + probabilities), but the model is a general-purpose LLM,
not TypeSafe Jev.

Request shapes were taken from the official docs (fetched 2026-11-26):

* OpenAI Responses: POST https://api.openai.com/v1/responses with
  ``text.format = {type: json_schema, name, strict, schema}``.
  Chat Completions alternative: POST /v1/chat/completions with
  ``response_format = {type: json_schema, json_schema: {name, strict, schema}}``
  and ``logprobs`` / ``top_logprobs`` when requested. Responses can also ask for
  ``include: ["message.output_text.logprobs"]``; some models return them empty
  when json_schema is on, so logprobs are recorded only when present.
  Docs: https://developers.openai.com/api/docs/guides/structured-outputs
* Gemini: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
  with ``generationConfig.responseMimeType = application/json`` and
  ``generationConfig.responseJsonSchema`` (standard JSON Schema; mutually exclusive
  with the older OpenAPI-style ``responseSchema``). Auth is the ``x-goog-api-key``
  header, never a query string (URLs can appear in errors).
  Docs: https://ai.google.dev/api/generate-content
All HTTP is mocked in tests; this module never requires live keys in CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from epochal_pilot.cache import Cache
from epochal_pilot.net import HTTPError, post_json
from epochal_pilot.providers import (
    LLM_AS_SYSTEM_ONE,
    ROLE_SYSTEM_ONE,
    Predictor,
    PredictResult,
    register,
    result_from_choice,
)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

Transport = Callable[[str, dict, dict], dict]

SYSTEM_PROMPT = (
    "You are acting as a System One predictor: return only a schema-locked forecast "
    "(the chosen option and a probability for every option). "
    "You are an LLM used as System One; you are not calibration-trained. "
    "Do not add commentary outside the schema."
)


def forecast_schema(options: list[str]) -> dict:
    """Strict JSON Schema: enum answer + a probability for every option."""
    props = {opt: {"type": "number"} for opt in options}
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "enum": list(options)},
            "probabilities": {
                "type": "object",
                "properties": props,
                "required": list(options),
                "additionalProperties": False,
            },
        },
        "required": ["answer", "probabilities"],
        "additionalProperties": False,
    }


def state_prompt(state: Any, question: str, options: list[str]) -> str:
    body = json.dumps(state, indent=2, default=str) if isinstance(state, (dict, list)) else str(state)
    opts = "\n".join(f"- {o}" for o in options)
    return f"Question: {question}\n\nOptions:\n{opts}\n\nState:\n{body}\n"


def _parse_forecast_json(text: str) -> tuple[str | None, dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        return None, {}
    answer = data.get("answer")
    probs = data.get("probabilities") or {}
    if not isinstance(probs, dict):
        probs = {}
    return (str(answer) if answer is not None else None), {str(k): float(v) for k, v in probs.items() if v is not None}


def extract_logprobs(raw: dict) -> list[dict] | None:
    """Pull a compact token-logprob list out of Chat Completions or Responses payloads, if any."""
    # Chat Completions
    try:
        content = (((raw.get("choices") or [{}])[0]).get("logprobs") or {}).get("content")
        if content:
            out = []
            for tok in content[:32]:
                if isinstance(tok, dict):
                    out.append({"token": tok.get("token"), "logprob": tok.get("logprob")})
            return out or None
    except (TypeError, AttributeError, IndexError):
        pass
    # Responses API: content items may carry logprobs
    found: list[dict] = []
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            lp = part.get("logprobs")
            if isinstance(lp, list):
                for tok in lp[:32]:
                    if isinstance(tok, dict):
                        found.append({"token": tok.get("token"), "logprob": tok.get("logprob")})
            elif isinstance(lp, dict) and isinstance(lp.get("content"), list):
                for tok in lp["content"][:32]:
                    if isinstance(tok, dict):
                        found.append({"token": tok.get("token"), "logprob": tok.get("logprob")})
    return found or None


def _responses_text(raw: dict) -> str:
    if raw.get("output_text"):
        return str(raw["output_text"])
    chunks: list[str] = []
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and part.get("text"):
                chunks.append(str(part["text"]))
            elif part.get("type") == "refusal":
                raise HTTPError(0, f"OpenAI structured output refusal: {part.get('refusal') or 'refused'}")
    return "".join(chunks)


def _chat_text(raw: dict) -> str:
    msg = (raw.get("choices") or [{}])[0].get("message") or {}
    if msg.get("refusal"):
        raise HTTPError(0, f"OpenAI structured output refusal: {msg['refusal']}")
    return msg.get("content") or ""


def _gemini_text(raw: dict) -> str:
    cands = raw.get("candidates") or []
    if not cands:
        feedback = raw.get("promptFeedback") or raw.get("prompt_feedback") or {}
        raise HTTPError(0, f"Gemini returned no candidates ({feedback or 'empty'})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


def _usage(raw: dict) -> dict:
    u = raw.get("usage") or raw.get("usageMetadata") or raw.get("usage_metadata") or {}
    return {
        "input_tokens": int(u.get("input_tokens") or u.get("prompt_tokens") or u.get("promptTokenCount") or 0),
        "output_tokens": int(
            u.get("output_tokens") or u.get("completion_tokens") or u.get("candidatesTokenCount") or 0
        ),
    }


class StructuredPredictor(Predictor):
    """Shared schema-locked adapter. Registered under openai and gemini."""

    role = ROLE_SYSTEM_ONE
    label = LLM_AS_SYSTEM_ONE

    def __init__(
        self,
        provider: str,
        api_key: str | None,
        model: str,
        *,
        cache: Cache | None = None,
        transport: Transport | None = None,
        timeout: float = 120,
        api: str = "responses",
        endpoint: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.provider = provider
        self._key = (api_key or "").strip() or None
        self.model = model
        self.cache = cache
        self.transport = transport
        self.timeout = timeout
        self.api = api  # responses | chat  (openai only)
        self.endpoint = (endpoint or "").rstrip("/") or None
        self.extra_headers = extra_headers or {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def __repr__(self) -> str:
        return (
            f"StructuredPredictor(provider={self.provider!r}, model={self.model!r}, "
            f"api_key={'set' if self._key else 'missing'}, api={self.api!r})"
        )

    def predict(self, state: Any, question: str, options: list[str]) -> PredictResult:
        schema = forecast_schema(options)
        prompt = state_prompt(state, question, options)
        url, body, headers = self._build(prompt, schema)
        raw = self._post(url, body, headers)
        text = self._extract_text(raw)
        try:
            answer, probs = _parse_forecast_json(text)
        except json.JSONDecodeError as e:
            raise HTTPError(0, f"{self.provider} returned non-JSON structured output") from e
        usage = _usage(raw)
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.calls += 1
        logprobs = extract_logprobs(raw)
        meta = {
            "provider": self.provider,
            "role": ROLE_SYSTEM_ONE,
            "label": LLM_AS_SYSTEM_ONE,
            "model": raw.get("model") or self.model,
            "api": self.api if self.provider == "openai" else "generateContent",
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "logprobs": logprobs,
        }
        rationale = f"{LLM_AS_SYSTEM_ONE}; provider={self.provider}; model={meta['model']}"
        return result_from_choice(
            answer,
            probs,
            options,
            parse_mode="structured",
            rationale=rationale,
            raw=text[:2000],
            metadata=meta,
        )

    def _build(self, prompt: str, schema: dict) -> tuple[str, dict, dict]:
        if self.provider == "openai":
            return self._openai_request(prompt, schema)
        if self.provider == "gemini":
            return self._gemini_request(prompt, schema)
        raise HTTPError(0, f"unknown structured provider {self.provider!r}")

    def _openai_headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._key or ''}", "Accept": "application/json"}
        h.update(self.extra_headers)
        return h

    def _openai_request(self, prompt: str, schema: dict) -> tuple[str, dict, dict]:
        if self.api == "chat":
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "forecast", "strict": True, "schema": schema},
                },
                "logprobs": True,
                "top_logprobs": 5,
                "temperature": 0,
            }
            return OPENAI_CHAT_URL, body, self._openai_headers()
        body = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "forecast",
                    "strict": True,
                    "schema": schema,
                }
            },
            "include": ["message.output_text.logprobs"],
            "top_logprobs": 5,
        }
        return OPENAI_RESPONSES_URL, body, self._openai_headers()

    def _gemini_request(self, prompt: str, schema: dict) -> tuple[str, dict, dict]:
        url = GEMINI_URL.format(model=self.model)
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": 0,
            },
        }
        # Key goes in the header only — never in the URL, which can appear in HTTPError messages.
        headers = {"x-goog-api-key": self._key or "", "Accept": "application/json"}
        headers.update(self.extra_headers)
        return url, body, headers

    def _extract_text(self, raw: dict) -> str:
        if self.provider == "gemini":
            return _gemini_text(raw)
        if self.provider == "openai" and self.api != "chat":
            return _responses_text(raw)
        return _chat_text(raw)

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        cache_key = None
        if self.cache is not None:
            cache_key = Cache.key("structured", self.provider, self.model, json.dumps(body, sort_keys=True))
            hit = self.cache.get("structured", cache_key)
            if hit is not None:
                return hit
        if self.transport is not None:
            raw = self.transport(url, body, headers)
        else:
            raw = post_json(url, body, headers=headers, timeout=self.timeout)
        if self.cache is not None and cache_key is not None:
            self.cache.put("structured", cache_key, raw)
        return raw


def mock_structured_transport(url: str, body: dict, _headers: dict) -> dict:
    """Offline fixture: all mass on 'No change' (or the first enum value)."""
    schema = {}
    if "generationConfig" in body:
        schema = (body.get("generationConfig") or {}).get("responseJsonSchema") or {}
    else:
        rf = body.get("response_format") or {}
        schema = (rf.get("json_schema") or {}).get("schema") or {}
        if not schema:
            fmt = (body.get("text") or {}).get("format") or {}
            schema = fmt.get("schema") or {}
    options = ((schema.get("properties") or {}).get("answer") or {}).get("enum") or ["No change"]
    pick = "No change" if "No change" in options else options[0]
    probs = {o: (1.0 if o == pick else 0.0) for o in options}
    payload = json.dumps({"answer": pick, "probabilities": probs})
    logprobs = {"content": [{"token": pick, "logprob": -0.01, "top_logprobs": [{"token": pick, "logprob": -0.01}]}]}
    if "generativelanguage.googleapis.com" in url:
        return {
            "candidates": [{"content": {"parts": [{"text": payload}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8, "totalTokenCount": 18},
        }
    if url.rstrip("/").endswith("/responses"):
        return {
            "model": "structured-mock",
            "output_text": payload,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": payload, "logprobs": logprobs["content"]}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 8},
        }
    return {
        "model": "structured-mock",
        "choices": [{"message": {"content": payload}, "logprobs": logprobs, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8},
    }


@register("openai")
class OpenAIPredictor(StructuredPredictor):
    name = "openai"
    label = LLM_AS_SYSTEM_ONE

    def __init__(self, api_key: str | None, model: str, **kwargs):
        super().__init__("openai", api_key, model, **kwargs)


@register("gemini")
class GeminiPredictor(StructuredPredictor):
    name = "gemini"
    label = LLM_AS_SYSTEM_ONE

    def __init__(self, api_key: str | None, model: str, **kwargs):
        kwargs.pop("api", None)
        kwargs.pop("endpoint", None)
        super().__init__("gemini", api_key, model, **kwargs)
