"""Run a scenario: for each model x event x mode, build the as-of context, ask, parse, score."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from epochal_pilot import config
from epochal_pilot.agent import (
    RESPONSE_FORMAT,
    Retrieval,
    force_choice_prompt,
    format_options,
    parse_forecast,
    retrieve,
)
from epochal_pilot.cache import Budget, BudgetExceeded, Cache
from epochal_pilot.epochal import Epochal, fixture_transport
from epochal_pilot.llm import LLM, mock_transport
from epochal_pilot.scenario import LETTERS, Event, Scenario, load_scenario
from epochal_pilot.scoring import baselines, brier, summarise

DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4.1-mini",
    "anthropic": "claude-haiku-5-5",
    "ollama": "llama3.2:3b",
}

PLAN_PROMPT = """Today's date is $today. You are researching this question using a web search engine
that only returns pages published on or before $today.

QUESTION: $question

Write $k short web search queries (one per line, no numbering, no quotes) that would find the most
useful evidence available on $today."""


def select_events(sc: Scenario, only: str | None, limit: int | None) -> list[Event]:
    evs = sc.events
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        unknown = set(want) - {e.id for e in evs}
        if unknown:
            raise SystemExit(f"unknown event id(s): {', '.join(sorted(unknown))}")
        evs = [e for e in evs if e.id in want]
    return evs[:limit] if limit else evs


def estimate(ep: Epochal, sc: Scenario, evs: list[Event], lead_days: int, num_results: int) -> tuple[int, int]:
    """(uncached searches, cached searches) for the seed queries. Fetches/planned queries come on top."""
    todo = sum(
        not ep.is_cached("search", e.as_of(lead_days), Epochal.search_body(e.search_query, num_results)) for e in evs
    )
    return todo, len(evs) - todo


def plan_queries(llm: LLM, model: str, ev: Event, today: str, k: int) -> list[str]:
    from string import Template

    reply = llm.ask(model, Template(PLAN_PROMPT).substitute(today=today, question=ev.question, k=k))
    qs = [ln.strip(" -*\t\"'") for ln in reply.splitlines() if 3 < len(ln.strip()) < 200]
    return [q for q in qs if not q.lower().startswith(("here", "sure"))][:k]


def run(args) -> int:
    config.load_dotenv(args.env_file)
    sc = load_scenario(Path(args.scenarios_dir), args.scenario)
    evs = select_events(sc, args.only, args.limit)
    lead = args.lead_days if args.lead_days is not None else sc.lead_days
    modes = args.modes.split(",") if args.modes else sc.modes
    for m in modes:
        if m not in sc.templates:
            raise SystemExit(f"scenario '{sc.name}' has no prompt for mode '{m}' (has: {', '.join(sc.templates)})")
    shuffle = sc.shuffle and not args.no_shuffle
    num_results = args.num_results or sc.num_results
    max_fetches = sc.max_fetches if args.max_fetches is None else args.max_fetches
    cutoffs = dict(c.split("=", 1) for c in (args.cutoff or []))

    cache_root = Path(args.cache_dir) / ("dryrun" if args.dry_run else "live")
    cache = Cache(cache_root)
    if args.dry_run:  # fixtures are free: the counter still runs, but never blocks
        budget = Budget(cache_root / "budget.json", per_run=10**9, max_total=10**9, verbose=False)
    else:
        budget = Budget(cache_root / "budget.json", per_run=args.budget, max_total=args.max_total)
    if args.dry_run:
        ep = Epochal(cache, budget, None, transport=fixture_transport)
        llm = LLM(cache, backend="mock", transport=mock_transport)
        models = args.model or ["mock-model"]
    else:
        need_epochal = "gated" in modes and not args.estimate_only
        ekey = config.require_key(config.EPOCHAL_KEY_ENV, "Get one at https://epochal.com.") if need_epochal else None
        okey = None
        if args.backend == "openrouter" and not args.estimate_only:
            okey = config.require_key(config.OPENROUTER_KEY_ENV, "Get one at https://openrouter.ai/keys.")
        elif args.backend == "anthropic" and not args.estimate_only:
            okey = config.require_key(config.ANTHROPIC_KEY_ENV, "Get one at https://console.anthropic.com/.")
        ep = Epochal(cache, budget, ekey)
        llm = LLM(cache, backend=args.backend, api_key=okey)
        models = args.model or [DEFAULT_MODELS[args.backend]]

    todo, cached = estimate(ep, sc, evs, lead, num_results)
    print(
        f"Scenario {sc.name}: {len(evs)} events x {len(models)} model(s) x modes {','.join(modes)}; "
        f"as-of = event - {lead} days; shuffle={'on' if shuffle else 'off'} (seed {args.seed})"
    )
    if "gated" in modes:
        print(
            f"Epochal: {cached} searches cached, {todo} new needed (+ up to {max_fetches} fetch(es) per event"
            f"{', + planned queries' if args.plan_queries else ''}). Budget: {budget.remaining} available "
            f"(--budget {args.budget}, lifetime {budget.total}/{args.max_total})."
        )
        if todo > budget.remaining:
            print(
                "  Not enough budget for every event; the run stops cleanly when it runs out. "
                "Use --limit/--only or raise --budget.",
                file=sys.stderr,
            )
    if args.estimate_only:
        return 0

    rows: list[dict] = []
    retrievals: dict[tuple, Retrieval] = {}
    stopped = None
    for model in models:
        if stopped:
            break
        for ev in evs:
            as_of = ev.as_of(lead)
            today = as_of[:10]
            shown = ev.ordered_choices(shuffle, args.seed)
            letters = "/".join(LETTERS[: len(shown)])
            for mode in modes:
                ret = Retrieval()
                if mode == "gated":
                    queries = [ev.search_query]
                    if args.plan_queries:
                        queries += plan_queries(llm, model, ev, today, args.plan_queries)
                    rkey = (ev.id, tuple(queries))
                    try:
                        if rkey not in retrievals:
                            retrievals[rkey] = retrieve(ep, queries, as_of, num_results, max_fetches)
                    except BudgetExceeded as e:
                        stopped = str(e)
                        break
                    ret = retrievals[rkey]
                prompt = sc.template(mode).safe_substitute(
                    today=today,
                    as_of=as_of,
                    question=ev.question,
                    options=format_options(shown),
                    letters=letters,
                    context=ret.context() or "(no documents found)",
                    response_format=RESPONSE_FORMAT.replace("$letters", letters),
                    **{k: v for k, v in ev.extra.items() if v},
                )
                reply = llm.ask(model, prompt)
                fc = parse_forecast(reply, shown)
                if fc.answer is None:
                    reply2 = llm.ask(model, force_choice_prompt(prompt, reply, shown))
                    fc = parse_forecast(reply2, shown)
                    fc.retried = True
                ctx_urls = set(ret.urls)
                cited_ok = (sum(c in ctx_urls for c in fc.citations) / len(fc.citations)) if fc.citations else None
                rows.append(
                    {
                        "model": model,
                        "mode": mode,
                        "event": ev.id,
                        "event_date": ev.event_date.isoformat(),
                        "as_of": as_of if mode == "gated" else "",
                        "outcome": ev.outcome,
                        "answer": fc.answer or "",
                        "answer_letter": LETTERS[shown.index(fc.answer)] if fc.answer else "",
                        "truth_letter": LETTERS[shown.index(ev.outcome)],
                        "correct": int(fc.answer == ev.outcome),
                        "p_outcome": round(fc.probabilities.get(ev.outcome, 0.0), 4),
                        "brier": round(brier(fc.probabilities, ev.outcome), 4),
                        "abstained": int(fc.answer is None),
                        "retried": int(fc.retried),
                        "parse_mode": fc.parse_mode,
                        "n_docs": len(ret.docs),
                        "dropped_future": ret.dropped_future,
                        "citations_in_context": None if cited_ok is None else round(cited_ok, 4),
                        "probabilities": json.dumps({k: round(v, 4) for k, v in fc.probabilities.items()}),
                        "citations": " ".join(fc.citations),
                        "rationale": fc.rationale,
                    }
                )
                mark = "ok " if fc.answer == ev.outcome else ("-- " if fc.answer else "?? ")
                print(
                    f"  {mark}{model:32} {mode:8} {ev.id:22} -> {fc.answer or '(abstain)'}"
                    f"  p(true)={rows[-1]['p_outcome']:.2f}"
                )
            if stopped:
                break
    if stopped:
        print(f"\nStopped early: {stopped}. Partial results are scored below.", file=sys.stderr)

    by_id = {e.id: e for e in sc.events}
    scored_events = [by_id[i] for i in dict.fromkeys(r["event"] for r in rows)]
    scores = summarise(rows, by_id, cutoffs) + baselines(scored_events, sc.baselines)
    out_dir = Path(args.out) if args.out else Path("runs") / f"{sc.name}-{datetime.now():%Y%m%d-%H%M%S}"
    write_outputs(out_dir, sc, rows, scores, args, lead, budget, models, modes, shuffle)
    print_table(scores)
    if args.dry_run:
        print(f"\nDry run: {budget.this_run} fixture requests (no network, nothing spent)")
    else:
        print(f"\nEpochal requests: {budget.this_run} this run, {budget.total} lifetime (cache: {cache_root})")
    print(f"Wrote {out_dir / 'predictions.csv'}, scores.csv, summary.md")
    return 0 if rows else 1


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_outputs(out_dir: Path, sc: Scenario, rows, scores, args, lead, budget, models, modes, shuffle) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "predictions.csv", rows)
    write_csv(out_dir / "scores.csv", scores)
    meta = {
        "scenario": sc.name,
        "models": models,
        "backend": "mock" if args.dry_run else args.backend,
        "modes": modes,
        "lead_days": lead,
        "shuffle": shuffle,
        "seed": args.seed,
        "events": len({r["event"] for r in rows}),
        "epochal_requests_this_run": budget.this_run,
        "dry_run": bool(args.dry_run),
        "created": datetime.now().isoformat(timespec="seconds"),
    }  # no keys or secrets, ever
    (out_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    lines = [
        f"# {sc.name} backtest",
        "",
        sc.description,
        "",
        "```json",
        json.dumps(meta, indent=2),
        "```",
        "",
        "| model | mode | n | accuracy | brier | chance | leak (upper bound) |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s['model']} | {s['mode']} | {s['n']} | {_fmt(s.get('accuracy'))} | {_fmt(s.get('brier'))} "
            f"| {_fmt(s.get('chance'))} | {_fmt(s.get('leak_score_upper_bound'))} |"
        )
    lines += [
        "",
        "Lower Brier is better (0 = perfect, 2 = confidently wrong). The leak score is gated accuracy "
        "above chance: an upper bound on leakage, not proof of it. Not investment advice.",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(x) -> str:
    return "" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def print_table(scores: list[dict]) -> None:
    print(f"\n{'model':40} {'mode':8} {'n':>3} {'acc':>6} {'brier':>6} {'chance':>6} {'pre':>6} {'post':>6}")
    for s in scores:
        print(
            f"{s['model'][:40]:40} {s['mode']:8} {s['n']:>3} {_fmt(s.get('accuracy')):>6} "
            f"{_fmt(s.get('brier')):>6} {_fmt(s.get('chance')):>6} {_fmt(s.get('acc_pre_cutoff')):>6} "
            f"{_fmt(s.get('acc_post_cutoff')):>6}"
        )
