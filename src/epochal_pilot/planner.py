"""``epochal-pilot plan``: spend a fixed number of Epochal requests where they carry the most signal.

Signal per event, highest first:
  * after the model's training cutoff (the model cannot have memorised the outcome),
  * contested (the outcome broke from the previous one, or the scenario marks it contested),
  * a longer lead time (harder; tests forecasting, not reading the news of the day).
Cached searches cost nothing, so they are always included.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from epochal_pilot import config
from epochal_pilot.agent import fetch_candidates
from epochal_pilot.cache import Budget, Cache
from epochal_pilot.epochal import Epochal
from epochal_pilot.runner import DEFAULT_MODELS, search_options, select_events
from epochal_pilot.scenario import Event, Scenario, load_scenario
from epochal_pilot.scoring import cutoff_for, is_post_cutoff


def contested(ev: Event, sc: Scenario) -> bool:
    flag = str(ev.extra.get("contested", "")).strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    col = sc.baselines.get("persistence_column")
    return bool(col and ev.extra.get(col) and ev.extra[col] != ev.outcome)


def candidates(
    sc: Scenario,
    models: list[str],
    leads: list[int],
    ep: Epochal,
    fetches_per_event: float,
    events: list[Event] | None = None,
) -> list[dict]:
    opts = search_options(sc)
    today = date.today()
    out = []
    for ev in sc.events if events is None else events:
        if ev.event_date >= today or not ev.resolved:
            continue
        cuts = [cutoff_for(m) for m in models]
        post = [c is not None and is_post_cutoff(ev.event_date, c) for c in cuts]
        post_frac = sum(post) / len(post) if post else 0.0
        for lead in leads[:1] if ev.fixed_as_of else leads:  # a fixed as-of date has one lead only
            as_of = ev.as_of(lead)
            hit = ep.cached("search", as_of, Epochal.search_body(ev.search_query, as_of, opts))
            if hit is not None:
                todo = [
                    u
                    for u in fetch_candidates(hit.get("results", []), as_of, sc.max_fetches)
                    if not ep.is_cached("fetch", as_of, Epochal.fetch_body(u, 3000))
                ]
                cost = float(len(todo))
            else:
                cost = 1.0 + min(fetches_per_event, sc.max_fetches)
            score = 1.0 + 3.0 * post_frac + 2.0 * contested(ev, sc) + (0.5 if lead > sc.lead_days else 0.0)
            out.append(
                {
                    "event": ev.id,
                    "date": ev.event_date.isoformat(),
                    "lead": lead,
                    "post_cutoff": post_frac,
                    "contested": contested(ev, sc),
                    "cached": hit is not None,
                    "cost": cost,
                    "score": score,
                }
            )
    return out


def choose(cands: list[dict], requests: int) -> tuple[list[dict], float]:
    free = [c for c in cands if c["cost"] == 0]
    # Best signal per request first; on ties prefer later events (further past any cutoff).
    paid = sorted((c for c in cands if c["cost"] > 0), key=lambda c: c["date"], reverse=True)
    paid.sort(key=lambda c: (-c["score"] / c["cost"], -c["score"]))
    chosen, spent = list(free), 0.0
    for c in paid:
        if spent + c["cost"] <= requests:
            chosen.append(c)
            spent += c["cost"]
    chosen.sort(key=lambda c: (c["date"], c["lead"]))
    return chosen, spent


def cmd_plan(args) -> int:
    config.load_dotenv(args.env_file)
    sc = load_scenario(Path(args.scenarios_dir), args.scenario)
    models = args.model or [DEFAULT_MODELS["openrouter"]]
    leads = [int(x) for x in str(args.lead_days or sc.lead_days).split(",") if x.strip()]
    cache = Cache(Path(args.cache_dir) / "live")
    budget = Budget(Path(args.cache_dir) / "live" / "budget.json", per_run=0, max_total=0, verbose=False)
    ep = Epochal(cache, budget, None)
    evs = select_events(sc, args.only, None) if getattr(args, "only", None) else None
    cands = candidates(sc, models, leads, ep, args.fetches_per_event, evs)
    chosen, spent = choose(cands, args.requests)
    unknown = [m for m in models if not cutoff_for(m)]
    print(
        f"Plan for {sc.name}: {args.requests} Epochal request(s); models {', '.join(models)}; lead days "
        f"{','.join(map(str, leads))}; ~{args.fetches_per_event:g} fetch(es) per uncached event"
    )
    if unknown:
        print(f"  (no known cutoff for {', '.join(unknown)}: pass it with `run --cutoff MODEL=YYYY-MM`)")
    print(f"\n{'event':22} {'date':10} {'lead':>4} {'post-cutoff':>11} {'contested':>9} {'cost':>5} {'score':>5}")
    for c in chosen:
        print(
            f"{c['event']:22} {c['date']:10} {c['lead']:>4} {c['post_cutoff']:>11.0%} "
            f"{'yes' if c['contested'] else '':>9} {'cached' if c['cached'] and c['cost'] == 0 else c['cost']:>5} "
            f"{c['score']:>5.1f}"
        )
    n_post = sum(c["post_cutoff"] > 0 for c in chosen)
    n_cont = sum(c["contested"] for c in chosen)
    print(
        f"\n{len(chosen)} event-runs, expected cost ~{spent:g} requests (worst case "
        f"{sum(1 + sc.max_fetches for c in chosen if not c['cached'])}); "
        f"{n_post} after a model cutoff, {n_cont} contested."
    )
    if n_cont and n_cont < len(chosen):
        print("Contested events are over-sampled on purpose: report scores per stratum, not as a base rate.")
    fixed = all(e.fixed_as_of for e in sc.events)
    for lead in leads[:1] if fixed else leads:
        ids = [c["event"] for c in chosen if c["lead"] == lead]
        if ids:
            mflags = " ".join(f"--model {m}" for m in models)
            lflag = "" if fixed else f" --lead-days {lead}"
            print(f"\n  epochal-pilot run {sc.name} --only {','.join(ids)}{lflag} {mflags} --budget {args.requests}")
    return 0
