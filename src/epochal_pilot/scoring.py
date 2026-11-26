"""Scoring: accuracy, multi-class Brier score, baselines and the pre/post-cutoff leak split."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from importlib import resources

from epochal_pilot.scenario import Event


def brier(probs: dict[str, float], outcome: str) -> float:
    """Multi-class Brier score: sum over options of (p - o)^2. 0 = perfect, 2 = confidently wrong."""
    return sum((p - (1.0 if c == outcome else 0.0)) ** 2 for c, p in probs.items())


def known_cutoffs() -> dict[str, str]:
    data = json.loads(resources.files("epochal_pilot").joinpath("models.json").read_text(encoding="utf-8"))
    return {k: v["cutoff"] for k, v in data.items() if not k.startswith("_")}


def cutoff_for(model: str, overrides: dict[str, str] | None = None) -> str | None:
    overrides = overrides or {}
    if model in overrides:
        return overrides[model]
    table = known_cutoffs()
    return table.get(model) or table.get(model.split("/", 1)[-1])


def month_end(cutoff: str) -> date:
    y, m = map(int, cutoff.split("-")[:2])
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def summarise(rows: list[dict], events: dict[str, Event], cutoffs: dict[str, str] | None = None) -> list[dict]:
    """One score row per (model, mode)."""
    out = []
    for model, mode in sorted({(r["model"], r["mode"]) for r in rows}):
        rs = [r for r in rows if r["model"] == model and r["mode"] == mode]
        base = _mean([1 / len(events[r["event"]].choices) for r in rs])
        acc = _mean([float(r["correct"]) for r in rs])
        row = {
            "model": model,
            "mode": mode,
            "n": len(rs),
            "abstain": sum(r["abstained"] for r in rs),
            "accuracy": acc,
            "brier": _mean([r["brier"] for r in rs]),
            "chance": base,
            "acc_above_chance": round(acc - base, 4) if acc is not None else None,
        }
        # Position-bias check: accuracy of always picking this model's favourite shown letter.
        picks = [r["answer_letter"] for r in rs if r["answer_letter"]]
        fav = Counter(picks).most_common(1)[0][0] if picks else None
        row["fav_letter"] = fav or ""
        row["always_fav_letter_acc"] = _mean([float(r["truth_letter"] == fav) for r in rs]) if fav else None
        cut = cutoff_for(model, cutoffs)
        row["cutoff"] = cut or ""
        if cut:
            end = month_end(cut)
            pre = [r for r in rs if events[r["event"]].event_date <= end]
            post = [r for r in rs if events[r["event"]].event_date > end]
            row |= {
                "n_pre_cutoff": len(pre),
                "acc_pre_cutoff": _mean([float(r["correct"]) for r in pre]),
                "brier_pre_cutoff": _mean([r["brier"] for r in pre]),
                "n_post_cutoff": len(post),
                "acc_post_cutoff": _mean([float(r["correct"]) for r in post]),
                "brier_post_cutoff": _mean([r["brier"] for r in post]),
            }
        if mode == "gated":
            row["docs_dropped_after_as_of"] = sum(r.get("dropped_future", 0) for r in rs)
            cited = [r["citations_in_context"] for r in rs if r.get("citations_in_context") is not None]
            row["citations_in_context"] = _mean(cited)
        out.append(row)
    # Leak score (upper bound): gated accuracy above chance; and ungated-minus-gated gap when both ran.
    by = {(s["model"], s["mode"]): s for s in out}
    for s in out:
        if s["mode"] == "gated":
            s["leak_score_upper_bound"] = s["acc_above_chance"]
            u = by.get((s["model"], "ungated"))
            if u and u["accuracy"] is not None and s["accuracy"] is not None:
                s["gap_ungated_minus_gated"] = round(u["accuracy"] - s["accuracy"], 4)
    return out


def _hit(probs: dict[str, float], outcome: str) -> float:
    """Expected accuracy when ties at the top are broken at random."""
    top = max(probs.values())
    winners = [c for c, p in probs.items() if p == top]
    return (outcome in winners) / len(winners)


def _one_hot(choices: list[str], pick: str) -> dict[str, float]:
    return {c: float(c == pick) for c in choices}


def baselines(evs: list[Event], spec: dict) -> list[dict]:
    """Reference forecasters that need no model.

    spec (from scenario.json "baselines"):
      {"constant": {"no-change": "Hold"}, "persistence_column": "previous_outcome",
       "majority": true, "uniform": true}
    """
    if not evs:
        return []
    out = []

    def score(name: str, probs_for) -> None:
        ps = [probs_for(e) for e in evs]
        out.append(
            {
                "model": f"baseline:{name}",
                "mode": "gated",
                "n": len(evs),
                "accuracy": _mean([_hit(p, e.outcome) for p, e in zip(ps, evs, strict=True)]),
                "brier": _mean([brier(p, e.outcome) for p, e in zip(ps, evs, strict=True)]),
            }
        )

    for name, pick in (spec.get("constant") or {}).items():
        score(name, lambda e, pick=pick: _one_hot(e.choices, pick))
    col = spec.get("persistence_column")
    if col and all(e.extra.get(col) in e.choices for e in evs):
        score("persistence", lambda e: _one_hot(e.choices, e.extra[col]))
    if spec.get("majority", True):
        top = Counter(e.outcome for e in evs).most_common(1)[0][0]
        score(
            f"majority({top}, hindsight)",
            lambda e: _one_hot(e.choices, top) if top in e.choices else {c: 1 / len(e.choices) for c in e.choices},
        )
    if spec.get("uniform", True):
        score("uniform", lambda e: {c: 1 / len(e.choices) for c in e.choices})
    return out
