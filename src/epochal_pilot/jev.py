"""TypeSafe Jev client (System One) and the state/question builder for the jev-rules pipeline.

Verified against POST https://api.typesafe.ai/v1/systemone (Authorization: Bearer, model
jev-latest -> jev-1.14.0): a Choice answer has type, choice, confidence, probabilities;
a Noul answer has type, noul; usage has input_tokens and output_tokens (output is free).
Input is billed at $0.042 per million tokens (docs, Jev 1.14). The API key is never logged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from epochal_pilot import __version__
from epochal_pilot.agent import Forecast, normalise_probs
from epochal_pilot.cache import Cache
from epochal_pilot.net import HTTPError, post_json
from epochal_pilot.rules import RULES_TEXT, RuleSignals
from epochal_pilot.scenario import LETTERS

SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
# https://docs.typesafe.ai/models — Jev 1.14 / 1.13, input tokens only; output is free.
PRICE_PER_MTOK = 0.042
FORECAST_QUESTION = "forecast"

# Atomic Noul questions recorded alongside the Choice (not used to invent the forecast).
ATOMIC_NOULS = {
    "statement_hawkish": "Does the latest FOMC statement signal that higher policy rates are likely soon?",
    "inflation_well_above_2": "Is core inflation (PCE preferred, otherwise CPI) well above the 2% goal?",
    "labor_weakening": "Is the labour market weakening (unemployment rising or payrolls soft)?",
    "shock_present": "Is a material shock present (pandemic, bank stress, financial turmoil)?",
}

FORECAST_INSTRUCTIONS = """\
Based only on the state, what will the net change in the FOMC's federal funds target range be
between the as-of date and the forecast (quarter-end) date?

Use the documents, the derived facts, and any rule-analysis outputs in the state. Do not use
knowledge of events after the as-of date, including what the Federal Reserve actually decided later.

The forecast is the NET change over every decision between the as-of date and quarter-end.
An SEP year-end median is a calendar-year projection, not the quarter-end outcome.
"""

CHOICE_CRITERIA = {
    "Cut 100+ bp": "The target range at quarter-end is 100 basis points or more lower than on the as-of date (net).",
    "Cut 75 bp": "The range is 75 basis points lower at quarter-end (net).",
    "Cut 50 bp": "The range is 50 basis points lower at quarter-end (net).",
    "Cut 25 bp": "The range is 25 basis points lower at quarter-end (net).",
    "No change": "The range at quarter-end is the same as on the as-of date (net 0 bp).",
    "Hike 25 bp": "The range is 25 basis points higher at quarter-end (net).",
    "Hike 50 bp": "The range is 50 basis points higher at quarter-end (net).",
    "Hike 75+ bp": "The range is 75 basis points or more higher at quarter-end (net).",
}


Transport = Callable[[str, dict, dict], dict]


def cost_usd(input_tokens: int) -> float:
    return input_tokens / 1_000_000 * PRICE_PER_MTOK


def forecast_questions(choices: list[str], *, atomics: bool = True) -> dict[str, dict]:
    criteria = {c: CHOICE_CRITERIA.get(c, c) for c in choices}
    questions: dict[str, dict] = {
        FORECAST_QUESTION: {
            "type": "choice",
            "instructions": FORECAST_INSTRUCTIONS,
            "criteria": criteria,
        }
    }
    if atomics:
        for name, inst in ATOMIC_NOULS.items():
            questions[name] = {"type": "noul", "instructions": inst}
    return questions


def build_state(
    *,
    as_of: str,
    target: str,
    range_at_asof: str,
    question: str,
    meetings: str,
    derived_facts: str,
    documents: list[dict],
    signals: RuleSignals | None,
    include_docs: bool,
    include_rules: bool,
    max_chars_per_doc: int = 4500,
) -> dict[str, Any]:
    """Assemble the System One ``state`` object (step 4). Blind runs omit docs, derived facts, and rules."""
    state: dict[str, Any] = {
        "as_of": as_of[:10],
        "forecast_date": target,
        "policy_range": range_at_asof,
        "question": question,
        "scheduled_meetings_before_target": meetings,
    }
    if include_docs:
        if derived_facts:
            state["derived_facts"] = derived_facts
        if documents:
            state["documents"] = [
                {
                    "title": d.get("title") or "",
                    "published": d.get("published_at") or "",
                    "url": d.get("url") or "",
                    "text": (d.get("text") or "")[:max_chars_per_doc],
                }
                for d in documents
            ]
    if include_rules and signals is not None:
        state["rule_analysis"] = signals.as_state()
        state["business_rules"] = RULES_TEXT
    return state


@dataclass
class JevResponse:
    model: str
    answers: dict[str, dict]
    input_tokens: int
    output_tokens: int
    raw: dict = field(default_factory=dict)


class JevClient:
    """POST /v1/systemone. The API key is stored but never written to logs, caches, or exceptions."""

    def __init__(
        self,
        api_key: str | None,
        cache: Cache | None = None,
        transport: Transport | None = None,
        timeout: float = 60,
    ):
        self._key = (api_key or "").strip() or None
        self.cache = cache
        self.transport = transport
        self.timeout = timeout
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def __repr__(self) -> str:
        return f"JevClient(api_key={'set' if self._key else 'missing'}, calls={self.calls})"

    def system_one(self, *, model: str, state: Any, questions: dict[str, dict]) -> JevResponse:
        body = {"model": model, "state": state, "questions": questions}
        cache_key = None
        if self.cache is not None:
            cache_key = Cache.key(
                "systemone", model, json.dumps(state, sort_keys=True), json.dumps(questions, sort_keys=True)
            )
            hit = self.cache.get("jev", cache_key)
            if hit is not None:
                parsed = _parse_response(hit)
                # Cached hits are not billed again; still expose the original usage counts.
                return parsed
        raw = self._post(body)
        parsed = _parse_response(raw)
        self.input_tokens += parsed.input_tokens
        self.output_tokens += parsed.output_tokens
        self.calls += 1
        if self.cache is not None and cache_key is not None:
            self.cache.put("jev", cache_key, raw)
        return parsed

    def _post(self, body: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self._key or ''}",
            "Accept": "application/json",
        }
        delay = 1.0
        last: Exception | None = None
        for _attempt in range(4):
            try:
                if self.transport is not None:
                    return self.transport(SYSTEMONE_URL, body, headers)
                return post_json(SYSTEMONE_URL, body, headers=headers, timeout=self.timeout)
            except HTTPError as e:
                last = e
                if e.status == 429 and _attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise last or HTTPError(0, f"cannot reach {SYSTEMONE_URL}")


def _parse_response(raw: dict) -> JevResponse:
    usage = raw.get("usage") or {}
    answers = raw.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}
    return JevResponse(
        model=str(raw.get("model") or ""),
        answers={k: v for k, v in answers.items() if isinstance(v, dict)},
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        raw=raw,
    )


def forecast_from_choice(resp: JevResponse, choices: list[str], question: str = FORECAST_QUESTION) -> Forecast:
    """Turn a Choice answer into a Forecast. Probabilities are taken as-is and renormalised."""
    ans = resp.answers.get(question) or {}
    if ans.get("type") and ans.get("type") != "choice":
        raise HTTPError(0, f"expected a choice answer for {question!r}, got type={ans.get('type')!r}")
    letter = None
    picked = str(ans.get("choice") or "").strip()
    if picked in choices:
        letter = LETTERS[choices.index(picked)]
    probs = normalise_probs(ans.get("probabilities") or {}, choices, letter)
    answer = None
    if letter:
        answer = choices[LETTERS.index(letter)]
    elif any(probs.values()):
        answer = max(probs, key=probs.get)
    conf = ans.get("confidence")
    if conf is not None:
        rationale = f"Jev Choice ({resp.model or 'jev'}); confidence={conf}"
    else:
        rationale = f"Jev Choice ({resp.model or 'jev'})"
    return Forecast(
        answer=answer,
        probabilities=probs,
        citations=[],
        rationale=rationale[:500],
        raw=json.dumps(ans, sort_keys=True),
        parse_mode="jev",
    )


def atomic_judgments(resp: JevResponse) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in ATOMIC_NOULS:
        ans = resp.answers.get(name) or {}
        if ans.get("type") == "noul" and ans.get("noul") is not None:
            try:
                out[name] = float(ans["noul"])
            except (TypeError, ValueError):
                continue
    return out


def mock_jev_transport(_url: str, body: dict, _headers: dict) -> dict:
    """Offline fixture: all mass on 'No change' (or the first criterion); Nouls at 0.0."""
    questions = body.get("questions") or {}
    answers: dict[str, dict] = {}
    for name, q in questions.items():
        qtype = (q or {}).get("type")
        if qtype == "choice":
            criteria = list((q or {}).get("criteria") or {})
            pick = "No change" if "No change" in criteria else (criteria[0] if criteria else "No change")
            probs = {c: (1.0 if c == pick else 0.0) for c in criteria} or {pick: 1.0}
            answers[name] = {"type": "choice", "choice": pick, "confidence": 1.0, "probabilities": probs}
        elif qtype == "noul":
            answers[name] = {"type": "noul", "noul": 0.0}
        elif qtype == "score":
            answers[name] = {"type": "score", "score": 0.0, "confidence": 1.0, "probabilities": {"0": 1.0}}
    return {"model": "jev-mock", "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 2}}


def usage_summary(client: JevClient) -> dict:
    return {
        "calls": client.calls,
        "input_tokens": client.input_tokens,
        "output_tokens": client.output_tokens,
        "approx_cost_usd": round(cost_usd(client.input_tokens), 6),
        "price_per_mtok_input": PRICE_PER_MTOK,
        "output_tokens_billed": False,
        "user_agent": f"epochal-pilot/{__version__}",
    }
