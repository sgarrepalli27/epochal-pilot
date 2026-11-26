"""System One predictor plugin interface: a stdlib registry of name -> predictor class.

A predictor implements ``predict(state, question, options)`` and returns a distribution
plus metadata. Built-in System One predictors: TypeSafe Jev (``jev`` / ``jev-rules``),
the no-model ``rules`` checklist, and official Decisions APIs (``openai-decisions``, ``gemini-decisions``),
and a labeled structured-output fallback (``openai``, ``gemini``).

The structured adapters are clearly labeled "LLM as System One (not calibration-trained)":
they lock the output to a JSON schema, but they are not TypeSafe Jev and are not
trained as a calibration-native judge.

System Two backends (openrouter, anthropic, claude-cli, ollama) stay available as
``--backend`` values for compatibility and as optional ``--analyst`` helpers; they
are not the recommended default predictor.

To add a third-party predictor, see docs/adding-a-provider.md and set
``EPOCHAL_PILOT_PROVIDER_MODULE`` to a module that calls ``register``.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar

from epochal_pilot.agent import Forecast, normalise_probs
from epochal_pilot.scenario import LETTERS

ROLE_SYSTEM_ONE = "system-one"
ROLE_SYSTEM_TWO = "system-two"
LLM_AS_SYSTEM_ONE = "LLM as System One (not calibration-trained)"

# Backward-compatible --backend values. System Two names stay valid as an explicit choice.
SYSTEM_ONE_BACKENDS = frozenset(
    {
        "jev",
        "jev-rules",
        "rules",
        "openai-decisions",
        "gemini-decisions",
        "openai",
        "gemini",
    }
)
SYSTEM_TWO_BACKENDS = frozenset({"openrouter", "anthropic", "ollama", "claude-cli"})
ALL_BACKENDS = tuple(
    [
        "jev-rules",
        "jev",
        "rules",
        "openai-decisions",
        "gemini-decisions",
        "openai",
        "gemini",
        "openrouter",
        "anthropic",
        "ollama",
        "claude-cli",
    ]
)

_REGISTRY: dict[str, type[Predictor]] = {}


@dataclass
class PredictResult:
    """Distribution over ``options`` plus provider metadata (never includes secrets)."""

    probabilities: dict[str, float]
    answer: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    raw: str = ""
    parse_mode: str = ""

    def as_forecast(self) -> Forecast:
        return Forecast(
            answer=self.answer,
            probabilities=self.probabilities,
            citations=[],
            rationale=(self.rationale or "")[:500],
            raw=self.raw,
            parse_mode=self.parse_mode or self.metadata.get("provider") or "system-one",
        )


class Predictor:
    """Provider plugin. Subclasses set ``name`` and implement ``predict``."""

    name: ClassVar[str] = ""
    role: ClassVar[str] = ROLE_SYSTEM_ONE
    label: ClassVar[str] = ""

    def predict(self, state: Any, question: str, options: list[str]) -> PredictResult:
        raise NotImplementedError


def register(name: str, cls: type[Predictor] | None = None):
    """Register ``cls`` under ``name``. Usable as ``@register("myco")`` or ``register("myco", Cls)``."""

    def _put(klass: type[Predictor]) -> type[Predictor]:
        if not getattr(klass, "name", None):
            klass.name = name
        _REGISTRY[name] = klass
        return klass

    if cls is not None:
        return _put(cls)
    return _put


def get_predictor_class(name: str) -> type[Predictor]:
    load_extra_providers()
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY) or ALL_BACKENDS)
        raise KeyError(f"unknown System One provider {name!r}. Known: {known}")
    return _REGISTRY[name]


def load_extra_providers() -> str | None:
    """Import ``EPOCHAL_PILOT_PROVIDER_MODULE`` once so it can call ``register``."""
    mod = (os.environ.get("EPOCHAL_PILOT_PROVIDER_MODULE") or "").strip()
    if not mod or getattr(load_extra_providers, "_loaded", None) == mod:
        return mod or None
    importlib.import_module(mod)
    load_extra_providers._loaded = mod  # type: ignore[attr-defined]
    return mod


def resolve_backend(explicit: str | None) -> tuple[str, str | None]:
    """Pick the predictor. Explicit ``--backend`` always wins (backward compatible).

    Otherwise: TypeSafe Jev-rules if ``TYPESAFE_API_KEY`` is set, else the ``rules``
    checklist plus a message on how to enable Jev and the other System One options.
    """
    from epochal_pilot import config

    if explicit:
        return explicit, None
    if (os.environ.get(config.TYPESAFE_KEY_ENV) or "").strip():
        return "jev-rules", "Default predictor: jev-rules (TypeSafe System One)."
    notice = (
        "No TYPESAFE_API_KEY set; using --backend rules (deterministic checklist, no model).\n"
        "To enable TypeSafe Jev, the recommended System One predictor: add TYPESAFE_API_KEY to .env\n"
        "  (see https://docs.typesafe.ai) and re-run, or pass --backend jev-rules.\n"
        "Other official System One Decisions APIs: --backend openai-decisions or --backend gemini-decisions.\n"
        "Schema-locked LLM fallback (not calibration-trained): --backend openai or --backend gemini."
    )
    return "rules", notice


def uses_rules_analysis(backend: str, with_rules: bool) -> bool:
    """Official pipeline step 3: run the in-code checklist and include it in state."""
    return backend == "jev-rules" or backend == "rules" or bool(with_rules)


def system_two_notice(backend: str) -> str | None:
    if backend not in SYSTEM_TWO_BACKENDS:
        return None
    return (
        f"Note: {backend} is a System Two (LLM) backend, intended for analysis and rationales.\n"
        "For predictions, prefer a System One predictor: --backend jev-rules (recommended),\n"
        "  --backend openai-decisions, or --backend gemini-decisions."
    )


class RulesPredictor(Predictor):
    """No-model checklist. The forecast is already computed as ``state['_rules_forecast']`` or passed in."""

    name = "rules"
    role = ROLE_SYSTEM_ONE
    label = "deterministic rules checklist (hindsight-documented, no model)"

    def __init__(self, forecast: Forecast | None = None):
        self._forecast = forecast

    def predict(self, state: Any, question: str, options: list[str]) -> PredictResult:
        fc = self._forecast
        if fc is None:
            raise RuntimeError("RulesPredictor needs a precomputed Forecast (see runner)")
        return PredictResult(
            probabilities=dict(fc.probabilities),
            answer=fc.answer,
            metadata={"provider": "rules", "role": ROLE_SYSTEM_ONE},
            rationale=fc.rationale,
            raw=fc.raw,
            parse_mode="rules",
        )


class JevPredictor(Predictor):
    """TypeSafe System One Choice over ``options``."""

    name = "jev"
    role = ROLE_SYSTEM_ONE
    label = "TypeSafe Jev (System One Choice)"

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def __repr__(self) -> str:
        return f"JevPredictor(model={self.model!r}, client={self.client!r})"

    def predict(self, state: Any, question: str, options: list[str]) -> PredictResult:
        from epochal_pilot.jev import atomic_judgments, forecast_from_choice, forecast_questions

        resp = self.client.system_one(model=self.model, state=state, questions=forecast_questions(options))
        fc = forecast_from_choice(resp, options)
        meta = {
            "provider": "jev",
            "role": ROLE_SYSTEM_ONE,
            "jev_model": resp.model,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "confidence": (resp.answers.get("forecast") or {}).get("confidence"),
            "judgments": atomic_judgments(resp),
        }
        return PredictResult(
            probabilities=dict(fc.probabilities),
            answer=fc.answer,
            metadata=meta,
            rationale=fc.rationale,
            raw=fc.raw,
            parse_mode="jev",
        )


register("rules", RulesPredictor)
register("jev", JevPredictor)
register("jev-rules", JevPredictor)


def result_from_choice(
    answer: str | None,
    probs: dict,
    options: list[str],
    *,
    parse_mode: str,
    rationale: str = "",
    raw: str = "",
    metadata: dict | None = None,
) -> PredictResult:
    letter = None
    if answer in options:
        letter = LETTERS[options.index(answer)]
    normalised = normalise_probs(probs or {}, options, letter)
    if not answer and any(normalised.values()):
        answer = max(normalised, key=normalised.get)
    return PredictResult(
        probabilities=normalised,
        answer=answer,
        metadata=metadata or {},
        rationale=rationale,
        raw=raw,
        parse_mode=parse_mode,
    )


# Side-effect import: registers decisions APIs and structured-output fallbacks.
from epochal_pilot import decisions as _decisions  # noqa: E402,F401
from epochal_pilot import structured as _structured  # noqa: E402,F401
