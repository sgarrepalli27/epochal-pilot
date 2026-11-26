"""Scoring: accuracy, Brier, log loss, baselines, skill scores, calibration and paired tests.

``analyse`` works on plain prediction rows (what ``predictions.csv`` and ``baseline_predictions.csv``
hold), so a saved run can be re-scored and reported later without any API access.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from datetime import date, timedelta
from importlib import resources
from pathlib import Path

from epochal_pilot import stats
from epochal_pilot.scenario import Event


def brier(probs: dict[str, float], outcome: str) -> float:
    """Multi-class Brier score: sum over options of (p - o)^2. 0 = perfect, 2 = confidently wrong."""
    return sum((p - (1.0 if c == outcome else 0.0)) ** 2 for c, p in probs.items())


def models_table() -> dict:
    data = json.loads(resources.files("epochal_pilot").joinpath("models.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def known_cutoffs() -> dict[str, str]:
    return {k: v["cutoff"] for k, v in models_table().items()}


def cutoff_for(model: str, overrides: dict[str, str] | None = None) -> str | None:
    overrides = overrides or {}
    if model in overrides:
        return overrides[model]
    table = known_cutoffs()
    return table.get(model) or table.get(model.split("/", 1)[-1])


def month_end(cutoff: str) -> date:
    y, m = map(int, cutoff.split("-")[:2])
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def is_post_cutoff(event_date: date, cutoff: str) -> bool:
    return event_date > month_end(cutoff)


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _hit(probs: dict[str, float], outcome: str) -> float:
    """Expected accuracy when ties at the top are broken at random."""
    top = max(probs.values())
    winners = [c for c, p in probs.items() if p == top]
    return (outcome in winners) / len(winners)


def _one_hot(choices: list[str], pick: str) -> dict[str, float]:
    return {c: float(c == pick) for c in choices}


# --------------------------------------------------------------------------------------------- baselines


def baseline_forecasts(evs: list[Event], spec: dict, user_rows: list[dict] | None = None) -> list[dict]:
    """Per-event forecasts from reference predictors that need no model.

    spec (scenario.json "baselines"):
      {"constant": {"no-change": "Hold"}, "persistence_column": "previous_outcome",
       "majority": true, "uniform": true}

    ``majority`` uses the outcomes being scored, so it is a HINDSIGHT baseline: shown for context,
    never used as the skill-score reference. ``user_rows`` adds a user-supplied forecaster (for example
    market-implied probabilities you are licensed to use); see ``load_user_baseline``.
    """
    if not evs:
        return []
    out: list[dict] = []

    def add(name: str, probs_for, hindsight: bool = False) -> None:
        for e in evs:
            p = probs_for(e)
            if p is None:
                continue
            out.append(
                {
                    "model": f"baseline:{name}",
                    "event": e.id,
                    "event_date": e.event_date.isoformat(),
                    "outcome": e.outcome,
                    "hindsight": int(hindsight),
                    "probabilities": json.dumps({k: round(v, 6) for k, v in p.items()}),
                }
            )

    for name, pick in (spec.get("constant") or {}).items():
        add(name, lambda e, pick=pick: _one_hot(e.choices, pick) if pick in e.choices else None)
    col = spec.get("persistence_column")
    if col:
        add("persistence", lambda e: _one_hot(e.choices, e.extra[col]) if e.extra.get(col) in e.choices else None)
    if spec.get("majority", True):
        top = Counter(e.outcome for e in evs).most_common(1)[0][0]
        add(
            "majority(hindsight)",
            lambda e: _one_hot(e.choices, top) if top in e.choices else {c: 1 / len(e.choices) for c in e.choices},
            hindsight=True,
        )
    if spec.get("uniform", True):
        add("uniform", lambda e: {c: 1 / len(e.choices) for c in e.choices})
    by_id = {e.id: e for e in evs}
    for r in user_rows or []:
        e = by_id.get(r["event"])
        if e is None:
            continue
        probs = {c: float(r["probs"].get(c, 0.0)) for c in e.choices}
        total = sum(probs.values()) or 1.0
        out.append(
            {
                "model": f"baseline:{r['name']}",
                "event": e.id,
                "event_date": e.event_date.isoformat(),
                "outcome": e.outcome,
                "hindsight": int(bool(r.get("hindsight", False))),
                "probabilities": json.dumps({c: round(p / total, 6) for c, p in probs.items()}),
            }
        )
    return out


def load_user_baseline(path: Path) -> list[dict]:
    """CSV with columns: name, event, then one column per choice holding a probability.

    Example (you supply the numbers, e.g. market-implied odds you are allowed to use)::

        name,event,Hold,Cut 25 bp,Cut 50 bp
        market,fomc_2024_09,0.0,0.4,0.6
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name, event = r.pop("name", "user") or "user", r.pop("event")
            hindsight = str(r.pop("hindsight", "0")).strip() in {"1", "true", "yes"}
            probs = {k: float(v) for k, v in r.items() if k and v not in (None, "")}
            rows.append({"name": name, "event": event, "probs": probs, "hindsight": hindsight})
    return rows


# ---------------------------------------------------------------------------------------------- analysis


def _norm(r: dict) -> dict:
    """Prediction row from memory or CSV -> typed fields used by the analysis."""
    probs = r["probabilities"]
    if isinstance(probs, str):
        probs = json.loads(probs) if probs else {}
    out = dict(r)
    out["probs"] = {k: float(v) for k, v in probs.items()}
    outcome = r["outcome"]
    out["p_true"] = out["probs"].get(outcome, 0.0)
    out["brier"] = brier(out["probs"], outcome) if out["probs"] else 2.0
    out["log_loss"] = stats.log_loss(out["probs"], outcome)
    if "correct" in r and r["correct"] not in ("", None):
        out["correct"] = float(r["correct"])
    else:
        out["correct"] = _hit(out["probs"], outcome) if out["probs"] else 0.0
    out["confidence"] = max(out["probs"].values()) if out["probs"] else 0.0
    out["hindsight"] = int(str(r.get("hindsight", "0")) in {"1", "True", "true"})
    mode = r.get("mode") or "baseline"
    out["mode"] = mode
    ctx = r.get("context")
    out["context"] = ctx if ctx else ("epochal" if mode == "gated" else "none")
    out["event_date_d"] = date.fromisoformat(str(r["event_date"])[:10])
    return out


def _boot_skill(m: list[float], ref: list[float], seed: int) -> tuple[float | None, float | None]:
    if not m:
        return None, None
    rng = random.Random(seed)
    n, vals = len(m), []
    for _ in range(stats.N_BOOT):
        idx = [rng.randrange(n) for _ in range(n)]
        rm = sum(ref[i] for i in idx) / n
        if rm > 0:
            vals.append(1 - (sum(m[i] for i in idx) / n) / rm)
    vals.sort()
    if not vals:
        return None, None
    return stats._percentile(vals, 0.025), stats._percentile(vals, 0.975)


def _summary_block(rs: list[dict], seed: int) -> dict:
    acc = [r["correct"] for r in rs]
    br = [r["brier"] for r in rs]
    ll = [r["log_loss"] for r in rs]
    a_lo, a_hi = stats.bootstrap_ci(acc, seed=seed)
    b_lo, b_hi = stats.bootstrap_ci(br, seed=seed)
    l_lo, l_hi = stats.bootstrap_ci(ll, seed=seed)
    return {
        "n": len(rs),
        "accuracy": _r(stats.mean(acc)),
        "accuracy_ci_low": _r(a_lo),
        "accuracy_ci_high": _r(a_hi),
        "brier": _r(stats.mean(br)),
        "brier_ci_low": _r(b_lo),
        "brier_ci_high": _r(b_hi),
        "log_loss": _r(stats.mean(ll)),
        "log_loss_ci_low": _r(l_lo),
        "log_loss_ci_high": _r(l_hi),
    }


def group_key(r: dict) -> tuple[str, str, str]:
    return (r["model"], r["mode"], r["context"])


def analyse(
    pred_rows: list[dict],
    base_rows: list[dict] | None = None,
    cutoffs: dict[str, str] | None = None,
    seed: int = 0,
) -> tuple[list[dict], dict]:
    """Score rows -> (scores table, stats dict with calibration, references and paired tests)."""
    preds = [_norm(r) for r in pred_rows]
    bases = [_norm({**r, "mode": "baseline", "context": "none"}) for r in (base_rows or [])]
    base_by: dict[str, dict[str, dict]] = {}
    for b in bases:
        base_by.setdefault(b["model"], {})[b["event"]] = b

    scores: list[dict] = []
    detail: dict = {"seed": seed, "n_boot": stats.N_BOOT, "groups": {}, "pairs": []}

    for key in sorted({group_key(r) for r in preds}):
        model, mode, ctx = key
        rs = [r for r in preds if group_key(r) == key]
        events = [r["event"] for r in rs]
        row = {"model": model, "mode": mode, "context": ctx, **_summary_block(rs, seed)}
        row["abstain"] = sum(int(float(r.get("abstained") or 0)) for r in rs)
        n_choices = [len(r["probs"]) or 1 for r in rs]
        row["chance"] = _r(stats.mean([1 / k for k in n_choices]))
        row["acc_above_chance"] = _r(row["accuracy"] - row["chance"]) if row["accuracy"] is not None else None
        picks = [r.get("answer_letter") for r in rs if r.get("answer_letter")]
        fav = Counter(picks).most_common(1)[0][0] if picks else None
        row["fav_letter"] = fav or ""
        row["always_fav_letter_acc"] = (
            _r(stats.mean([float(r.get("truth_letter") == fav) for r in rs])) if fav else None
        )
        cut = cutoff_for(model, cutoffs)
        row["cutoff"] = cut or ""
        if cut:
            pre = [r for r in rs if not is_post_cutoff(r["event_date_d"], cut)]
            post = [r for r in rs if is_post_cutoff(r["event_date_d"], cut)]
            for tag, sub in (("pre", pre), ("post", post)):
                row[f"n_{tag}_cutoff"] = len(sub)
                row[f"acc_{tag}_cutoff"] = _r(stats.mean([r["correct"] for r in sub]))
                row[f"brier_{tag}_cutoff"] = _r(stats.mean([r["brier"] for r in sub]))
        # Reference = best NON-hindsight baseline on exactly these events.
        ref_name, ref_brier = None, None
        for bname, by_ev in base_by.items():
            if any(by_ev[e]["hindsight"] for e in events if e in by_ev):
                continue
            if not all(e in by_ev for e in events):
                continue
            bb = stats.mean([by_ev[e]["brier"] for e in events])
            if bb is not None and (ref_brier is None or bb < ref_brier):
                ref_name, ref_brier = bname, bb
        row["reference"] = ref_name or ""
        row["reference_brier"] = _r(ref_brier)
        g: dict = {"key": list(key)}
        if ref_name:
            m_b = [r["brier"] for r in rs]
            r_b = [base_by[ref_name][e]["brier"] for e in events]
            row["brier_skill"] = _r(stats.skill_score(stats.mean(m_b), ref_brier))
            lo, hi = _boot_skill(m_b, r_b, seed)
            row["brier_skill_ci_low"], row["brier_skill_ci_high"] = _r(lo), _r(hi)
            pb = stats.paired_bootstrap(m_b, r_b, seed=seed)
            row["p_vs_reference"] = _r(pb["p_value"])
            g["vs_reference"] = {"reference": ref_name, "brier_diff": _round_dict(pb)}
            ref_hits = [base_by[ref_name][e]["correct"] for e in events]
            if all(h in (0.0, 1.0) for h in ref_hits):
                g["vs_reference"]["mcnemar"] = stats.mcnemar_exact(
                    [int(r["correct"]) for r in rs], [int(h) for h in ref_hits]
                )
        bins = stats.calibration_bins([r["confidence"] for r in rs], [r["correct"] for r in rs])
        g["calibration"] = _round_bins(bins)
        row["ece"] = _r(stats.expected_calibration_error(bins))
        g["warning"] = stats.small_n_warning(len(rs))
        row["small_n"] = int(bool(g["warning"]))
        if mode == "gated":
            row["docs_dropped_after_as_of"] = sum(int(float(r.get("dropped_future") or 0)) for r in rs)
            cited = [float(r["citations_in_context"]) for r in rs if r.get("citations_in_context") not in (None, "")]
            row["citations_in_context"] = _r(stats.mean(cited))
        detail["groups"]["|".join(key)] = g
        scores.append(row)

    # Paired: gated vs ungated for the same model, on shared events.
    for model in sorted({r["model"] for r in preds}):
        gated = {(r["context"], r["event"]): r for r in preds if r["model"] == model and r["mode"] == "gated"}
        ung = {r["event"]: r for r in preds if r["model"] == model and r["mode"] == "ungated"}
        for ctx in sorted({c for c, _ in gated}):
            common = sorted(e for (c, e) in gated if c == ctx and e in ung)
            if not common:
                continue
            a = [gated[(ctx, e)] for e in common]
            b = [ung[e] for e in common]
            detail["pairs"].append(paired_compare(f"{model} gated[{ctx}]", a, f"{model} ungated", b, seed))
    by = {(s["model"], s["mode"], s["context"]): s for s in scores}
    for s in scores:
        if s["mode"] == "gated":
            s["leak_score_upper_bound"] = s["acc_above_chance"]
            u = by.get((s["model"], "ungated", "none"))
            if u and u["accuracy"] is not None and s["accuracy"] is not None:
                s["gap_ungated_minus_gated"] = _r(u["accuracy"] - s["accuracy"])

    # Baseline rows (scored on every event they cover).
    for bname, by_ev in sorted(base_by.items()):
        rs = list(by_ev.values())
        row = {"model": bname, "mode": "baseline", "context": "none", **_summary_block(rs, seed)}
        row["hindsight"] = int(any(r["hindsight"] for r in rs))
        scores.append(row)
    return scores, detail


def paired_compare(name_a: str, a: list[dict], name_b: str, b: list[dict], seed: int = 0) -> dict:
    """Paired tests on rows for the same events (same order): Brier bootstrap + exact McNemar."""
    pb = stats.paired_bootstrap([r["brier"] for r in a], [r["brier"] for r in b], seed=seed)
    out = {
        "a": name_a,
        "b": name_b,
        "n": len(a),
        "acc_a": _r(stats.mean([r["correct"] for r in a])),
        "acc_b": _r(stats.mean([r["correct"] for r in b])),
        "brier_a": _r(stats.mean([r["brier"] for r in a])),
        "brier_b": _r(stats.mean([r["brier"] for r in b])),
        "brier_diff": _round_dict(pb),
    }
    ca, cb = [r["correct"] for r in a], [r["correct"] for r in b]
    if all(x in (0.0, 1.0) for x in ca + cb):
        out["mcnemar"] = stats.mcnemar_exact([int(x) for x in ca], [int(x) for x in cb])
    return out


def _round_dict(d: dict) -> dict:
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in d.items()}


def _round_bins(bins: list[dict]) -> list[dict]:
    return [_round_dict(b) for b in bins]


def norm_rows(rows: list[dict]) -> list[dict]:
    return [_norm(r) for r in rows]


def direction_summary(rows: list[dict], directions: dict[str, str], choice_bp: dict[str, int] | None = None) -> dict:
    """Per forecaster: share of events where the top choice's direction (down/hold/up) matches the truth,
    and the mean absolute error in bp of the top choice. Works on prediction and baseline rows alike."""
    out: dict[str, dict] = {}
    groups: dict[str, list[tuple[list[str], str]]] = {}
    for r in rows:
        if not r.get("outcome"):
            continue
        probs = json.loads(r.get("probabilities") or "{}")
        if r.get("answer"):
            picks = [r["answer"]]
        elif probs:
            top = max(probs.values())
            picks = [c for c, p in probs.items() if p == top]  # ties (e.g. uniform): average over them
        else:
            continue
        mode = r.get("mode", "")
        name = r["model"] if r["model"].startswith("baseline:") else f"{r['model']} {mode}[{r.get('context', '')}]"
        groups.setdefault(name, []).append((picks, r["outcome"]))
    for name, pairs in groups.items():
        hits = [sum(directions.get(p) == directions.get(o) for p in ps) / len(ps) for ps, o in pairs]
        errs = [
            sum(abs(choice_bp[p] - choice_bp[o]) for p in ps) / len(ps)
            for ps, o in pairs
            if choice_bp and o in choice_bp and all(p in choice_bp for p in ps)
        ]
        out[name] = {
            "n": len(pairs),
            "direction_accuracy": round(sum(hits) / len(hits), 4),
            "mae_bp": round(sum(errs) / len(errs), 2) if errs else None,
        }
    return out
