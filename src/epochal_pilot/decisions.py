"""Official Decisions APIs used as System One predictors (alongside TypeSafe Jev).

Request and response shapes were taken from the official docs (fetched 2026-11-26):

* OpenAI Decisions (GA): POST https://api.openai.com/v1/decisions
  Authorization: Bearer $OPENAI_API_KEY. Only model: gpt-6-luna.
  Body: {model, input, questions[]} where questions is an array of
  predicate | choice | score objects (each may have a name).
  A choice question uses choices: [{value, description}].
  Answers is an array (same order / echoed name). Choice probabilities are
  [{value, probability}, ...]. Predicate returns probability (0-1).
  Usage: input_tokens, output_tokens (not charged), input_tokens_details.cached_tokens.
  Uncached input $0.10 / 1M tokens. Docs do not call the probabilities "calibrated";
  they describe an "estimate" plus a separate confidence on choice/score.
  https://developers.openai.com/api/docs/guides/decisions
  https://developers.openai.com/api/reference/resources/decisions/methods/create

* Gemini Decisions API (v1beta): POST https://generativelanguage.googleapis.com/v1beta/decisions
  Auth: x-goog-api-key header (never a query string — URLs can appear in errors).
  Model: gemini-decision-1. Body: {model, input, questions} where questions is a
  map of name -> {type: noul|choice|score, instructions, criteria}.
  Choice criteria is a map of option id -> description. Answers is a map.
  Choice probabilities are a map; noul is P(yes) in [0.01, 0.99].
  Docs explicitly return "calibrated probabilities". Output tokens are always 0.
  Paid input $0.10 / 1M tokens; output not applicable. Free tier is free of charge.
  https://aistudio.google.com/docs/decisions-api
  https://aistudio.google.com/api-reference/decisions/create-decision

Tests use mocked HTTP only; this module never requires live keys in CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from epochal_pilot.cache import Cache
from epochal_pilot.jev import ATOMIC_NOULS, CHOICE_CRITERIA, FORECAST_INSTRUCTIONS, FORECAST_QUESTION
from epochal_pilot.net import HTTPError, post_json
from epochal_pilot.providers import ROLE_SYSTEM_ONE, Predictor, PredictResult, register, result_from_choice

OPENAI_DECISIONS_URL = "https://api.openai.com/v1/decisions"
GEMINI_DECISIONS_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
OPENAI_DECISIONS_MODEL = "gpt-6-luna"
GEMINI_DECISIONS_MODEL = "gemini-decision-1"
# Official paid input price for OpenAI and Gemini Decisions APIs (output is free / N/A).
PRICE_PER_MTOK = 0.10

Transport = Callable[[str, dict, dict], dict]


def cost_usd(input_tokens: int, *, cached_tokens: int = 0, price: float = PRICE_PER_MTOK) -> float:
    # Official list price is $0.10 / 1M uncached input; cache discounts are recorded in metadata, not applied here.
    billed = max(input_tokens - cached_tokens, 0)
    return billed / 1_000_000 * price


def openai_questions(options: list[str], *, atomics: bool = True) -> list[dict]:
    """OpenAI Decisions: questions is an array; yes/no is ``predicate``."""
    qs: list[dict] = [
        {
            "type": "choice",
            "name": FORECAST_QUESTION,
            "instructions": FORECAST_INSTRUCTIONS,
            "choices": [{"value": c, "description": CHOICE_CRITERIA.get(c, c)} for c in options],
        }
    ]
    if atomics:
        for name, inst in ATOMIC_NOULS.items():
            qs.append({"type": "predicate", "name": name, "instructions": inst})
    return qs


def gemini_questions(options: list[str], *, atomics: bool = True) -> dict[str, dict]:
    """Gemini Decisions: questions is a name -> question map; yes/no is ``noul``."""
    qs: dict[str, dict] = {
        FORECAST_QUESTION: {
            "type": "choice",
            "instructions": FORECAST_INSTRUCTIONS,
            "criteria": {c: CHOICE_CRITERIA.get(c, c) for c in options},
        }
    }
    if atomics:
        for name, inst in ATOMIC_NOULS.items():
            qs[name] = {"type": "noul", "instructions": inst}
    return qs


def openai_input(state: Any) -> Any:
    """Shared evidence. Dict state is sent as an official ``input_json`` part."""
    if isinstance(state, dict):
        return [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Shared as-of evidence for the forecast questions follows as JSON."},
                    {"type": "input_json", "data": state},
                ],
            }
        ]
    return str(state) if state is not None else ""


def gemini_input(state: Any) -> str:
    """Gemini accepts a string, Content, or Content[]. We send compact JSON or text."""
    if isinstance(state, (dict, list)):
        return json.dumps(state, default=str)
    return str(state) if state is not None else ""


def _probs_from_openai_choice(ans: dict) -> tuple[str | None, dict[str, float]]:
    picked = ans.get("choice")
    probs: dict[str, float] = {}
    raw = ans.get("probabilities") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("value") is not None:
                try:
                    probs[str(item["value"])] = float(item.get("probability") or 0)
                except (TypeError, ValueError):
                    continue
    elif isinstance(raw, dict):
        for k, v in raw.items():
            try:
                probs[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    return (str(picked) if picked is not None else None), probs


def _probs_from_gemini_choice(ans: dict) -> tuple[str | None, dict[str, float]]:
    picked = ans.get("choice")
    raw = ans.get("probabilities") or {}
    probs: dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                probs[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    return (str(picked) if picked is not None else None), probs


def _openai_answers_by_name(raw: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i, ans in enumerate(raw.get("answers") or []):
        if not isinstance(ans, dict):
            continue
        out[str(ans.get("name") or f"q{i}")] = ans
    return out


def _usage(raw: dict, *, flavor: str) -> dict[str, int]:
    u = raw.get("usage") or {}
    if flavor == "gemini":
        return {
            "input_tokens": int(u.get("total_input_tokens") or u.get("input_tokens") or 0),
            "output_tokens": int(u.get("total_output_tokens") or u.get("output_tokens") or 0),
            "cached_tokens": 0,
        }
    details = u.get("input_tokens_details") or {}
    return {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
    }


def atomic_from_answers(by_name: dict[str, dict]) -> dict[str, float]:
    """Record yes/no atomics as a common {name: P(yes)} map (predicate.probability or noul)."""
    out: dict[str, float] = {}
    for name in ATOMIC_NOULS:
        ans = by_name.get(name) or {}
        typ = ans.get("type")
        try:
            if typ == "predicate" and ans.get("probability") is not None:
                out[name] = float(ans["probability"])
            elif typ == "noul" and ans.get("noul") is not None:
                out[name] = float(ans["noul"])
        except (TypeError, ValueError):
            continue
    return out


class DecisionsClient:
    """POST the official Decisions endpoint for one flavor. The key is never logged."""

    def __init__(
        self,
        flavor: str,
        api_key: str | None,
        *,
        cache: Cache | None = None,
        transport: Transport | None = None,
        timeout: float = 60,
        endpoint: str | None = None,
    ):
        if flavor not in {"openai", "gemini"}:
            raise ValueError(f"unknown decisions flavor {flavor!r}")
        self.flavor = flavor
        self._key = (api_key or "").strip() or None
        self.cache = cache
        self.transport = transport
        self.timeout = timeout
        self.endpoint = (endpoint or "").rstrip("/") or None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.calls = 0

    def __repr__(self) -> str:
        return f"DecisionsClient(flavor={self.flavor!r}, api_key={'set' if self._key else 'missing'})"

    def url(self) -> str:
        if self.flavor == "gemini":
            return GEMINI_DECISIONS_URL
        return OPENAI_DECISIONS_URL

    def headers(self) -> dict[str, str]:
        if self.flavor == "gemini":
            return {"x-goog-api-key": self._key or "", "Accept": "application/json"}
        return {"Authorization": f"Bearer {self._key or ''}", "Accept": "application/json"}

    def decide(self, *, model: str, state: Any, options: list[str], atomics: bool = True) -> dict:
        if self.flavor == "gemini":
            body = {
                "model": model,
                "input": gemini_input(state),
                "questions": gemini_questions(options, atomics=atomics),
            }
        else:
            body = {
                "model": model,
                "input": openai_input(state),
                "questions": openai_questions(options, atomics=atomics),
            }
        cache_key = None
        if self.cache is not None:
            cache_key = Cache.key("decisions", self.flavor, model, json.dumps(body, sort_keys=True))
            hit = self.cache.get("decisions", cache_key)
            if hit is not None:
                return hit
        raw = self._post(self.url(), body, self.headers())
        usage = _usage(raw, flavor="gemini" if self.flavor == "gemini" else "openai")
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cached_tokens += usage["cached_tokens"]
        self.calls += 1
        if self.cache is not None and cache_key is not None:
            self.cache.put("decisions", cache_key, raw)
        return raw

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        if self.transport is not None:
            return self.transport(url, body, headers)
        return post_json(url, body, headers=headers, timeout=self.timeout)


def parse_choice(raw: dict, options: list[str], *, flavor: str) -> tuple[str | None, dict[str, float], dict]:
    """Return (answer, probabilities, extras) for the forecast question. Raises on refusal."""
    if flavor == "gemini":
        feedback = raw.get("prompt_feedback") or raw.get("promptFeedback") or {}
        if feedback.get("block_reason") or feedback.get("blockReason"):
            raise HTTPError(0, "Gemini Decisions request blocked (prompt_feedback)")
        by_name = {k: v for k, v in (raw.get("answers") or {}).items() if isinstance(v, dict)}
        ans = by_name.get(FORECAST_QUESTION) or {}
        if ans.get("type") == "refusal":
            raise HTTPError(0, "Gemini Decisions refused the forecast question")
        answer, probs = _probs_from_gemini_choice(ans)
        extras = {
            "confidence": ans.get("confidence"),
            "judgments": atomic_from_answers(by_name),
            "model": raw.get("model"),
        }
        return answer, probs, extras

    by_name = _openai_answers_by_name(raw)
    ans = by_name.get(FORECAST_QUESTION) or {}
    if ans.get("type") == "refusal":
        raise HTTPError(0, "Decisions API refused the forecast question")
    if ans.get("type") and ans.get("type") != "choice":
        raise HTTPError(0, f"expected a choice answer for {FORECAST_QUESTION!r}, got type={ans.get('type')!r}")
    answer, probs = _probs_from_openai_choice(ans)
    extras = {
        "confidence": ans.get("confidence"),
        "judgments": atomic_from_answers(by_name),
        "model": raw.get("model"),
    }
    return answer, probs, extras


class DecisionsPredictor(Predictor):
    """System One predictor backed by an official Decisions API (not the structured-output fallback)."""

    role = ROLE_SYSTEM_ONE
    label = "official Decisions API (System One)"

    def __init__(self, client: DecisionsClient, model: str):
        self.client = client
        self.model = model

    def __repr__(self) -> str:
        return f"DecisionsPredictor(flavor={self.client.flavor!r}, model={self.model!r}, client={self.client!r})"

    def predict(self, state: Any, question: str, options: list[str]) -> PredictResult:
        raw = self.client.decide(model=self.model, state=state, options=options)
        flavor = self.client.flavor
        answer, probs, extras = parse_choice(raw, options, flavor=flavor)
        usage = _usage(raw, flavor="gemini" if flavor == "gemini" else "openai")
        provider = {"openai": "openai-decisions", "gemini": "gemini-decisions"}[flavor]
        calibrated = flavor == "gemini"  # only Gemini's docs claim calibrated probabilities
        meta = {
            "provider": provider,
            "role": ROLE_SYSTEM_ONE,
            "label": self.label,
            "flavor": flavor,
            "model": extras.get("model") or self.model,
            "confidence": extras.get("confidence"),
            "judgments": extras.get("judgments") or {},
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cached_tokens": usage["cached_tokens"],
            "calibrated_claim": calibrated,
            "approx_cost_usd": round(cost_usd(usage["input_tokens"], cached_tokens=usage["cached_tokens"]), 6),
        }
        claim = (
            "calibrated Decisions API"
            if calibrated
            else "Decisions API (probability estimates; calibration not claimed in the official guide)"
        )
        rationale = f"{claim}; provider={provider}; model={meta['model']}; confidence={extras.get('confidence')}"
        return result_from_choice(
            answer,
            probs,
            options,
            parse_mode="decisions",
            rationale=rationale[:500],
            raw=json.dumps(
                {
                    FORECAST_QUESTION: (
                        raw.get("answers") if flavor != "gemini" else (raw.get("answers") or {}).get(FORECAST_QUESTION)
                    )
                },
                default=str,
            )[:2000],
            metadata=meta,
        )


def mock_decisions_transport(url: str, body: dict, _headers: dict) -> dict:
    """Offline fixture: all mass on 'No change' (or the first option). Provider-shaped payload."""
    pick = "No change"
    options: list[str] = []
    questions = body.get("questions")
    if isinstance(questions, dict):
        crit = (questions.get(FORECAST_QUESTION) or {}).get("criteria") or {}
        options = list(crit)
    elif isinstance(questions, list):
        for q in questions:
            if (q or {}).get("name") == FORECAST_QUESTION or (q or {}).get("type") == "choice":
                options = [
                    str(c.get("value"))
                    for c in (q.get("choices") or [])
                    if isinstance(c, dict) and c.get("value") is not None
                ]
                break
    if "No change" not in options:
        pick = options[0] if options else "No change"
    if "generativelanguage.googleapis.com" in url:
        probs = {o: (1.0 if o == pick else 0.0) for o in options} or {pick: 1.0}
        answers: dict[str, dict] = {
            FORECAST_QUESTION: {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 1.0}
        }
        if isinstance(questions, dict):
            for name, q in questions.items():
                if name == FORECAST_QUESTION:
                    continue
                if (q or {}).get("type") == "noul":
                    answers[name] = {"type": "noul", "noul": 0.01, "confidence": 0.99}
        return {
            "model": GEMINI_DECISIONS_MODEL,
            "answers": answers,
            "usage": {"total_input_tokens": 20, "total_output_tokens": 0, "total_tokens": 20},
        }
    # OpenAI Decisions: answers array, probabilities as [{value, probability}]
    qs = questions if isinstance(questions, list) else []
    opts = options or [pick]
    probs = [{"value": o, "probability": (1.0 if o == pick else 0.0)} for o in opts]
    answers_l: list[dict] = [
        {"type": "choice", "name": FORECAST_QUESTION, "choice": pick, "probabilities": probs, "confidence": 1.0}
    ]
    for q in qs:
        if (q or {}).get("type") == "predicate" and (q or {}).get("name"):
            answers_l.append({"type": "predicate", "name": q["name"], "probability": 0.0})
    return {
        "model": body.get("model") or OPENAI_DECISIONS_MODEL,
        "answers": answers_l,
        "usage": {
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 20,
        },
    }


@register("openai-decisions")
class OpenAIDecisionsPredictor(DecisionsPredictor):
    name = "openai-decisions"
    label = "OpenAI Decisions API (gpt-6-luna)"

    def __init__(self, api_key: str | None, model: str, **kwargs):
        endpoint = kwargs.pop("endpoint", None)
        super().__init__(DecisionsClient("openai", api_key, endpoint=endpoint, **kwargs), model)


@register("gemini-decisions")
class GeminiDecisionsPredictor(DecisionsPredictor):
    name = "gemini-decisions"
    label = "Gemini Decisions API (gemini-decision-1)"

    def __init__(self, api_key: str | None, model: str, **kwargs):
        kwargs.pop("endpoint", None)
        kwargs.pop("api", None)
        super().__init__(DecisionsClient("gemini", api_key, **kwargs), model)
