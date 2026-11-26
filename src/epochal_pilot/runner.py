"""Run a scenario: for each model x event x mode, build the as-of context, ask, parse, score."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from epochal_pilot import config, manifest
from epochal_pilot.agent import (
    RESPONSE_FORMAT,
    Retrieval,
    fetch_candidates,
    force_choice_prompt,
    format_options,
    parse_forecast,
    retrieve,
)
from epochal_pilot.cache import Budget, BudgetExceeded, Cache
from epochal_pilot.epochal import Epochal, SearchOptions, fixture_transport
from epochal_pilot.llm import LLM, ClaudeCLIError, find_claude, mock_transport
from epochal_pilot.public_context import load_public_context
from epochal_pilot.scenario import LETTERS, Event, Scenario, load_scenario
from epochal_pilot.scoring import analyse, baseline_forecasts, brier, load_user_baseline

DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4.1-mini",
    "anthropic": "claude-haiku-5-5",
    "ollama": "llama3.2:3b",
    "claude-cli": "default",
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


def search_options(sc: Scenario, num_results: int | None = None) -> SearchOptions:
    opts = SearchOptions(
        num_results=num_results or sc.num_results,
        categories=sc.categories,
        published_after_days=sc.published_after_days,
        domains=sc.domains,
        exclude_domains=sc.exclude_domains,
    )
    errs = opts.validate()
    if errs:
        raise SystemExit(f"scenario '{sc.name}' search settings: {'; '.join(errs)}")
    return opts


def estimate(
    ep: Epochal,
    evs: list[Event],
    lead_days: int,
    opts: SearchOptions,
    max_fetches: int,
    plan_queries: int = 0,
    n_models: int = 1,
) -> dict:
    """New Epochal requests a run needs: seed searches, fetches and model-planned queries.

    Fetches are exact when the seed search is cached (we can see which results are thin), otherwise
    0..max_fetches per event. Planned queries depend on the model's reply, so they count as new.
    """
    est = {"events": len(evs), "searches_new": 0, "searches_cached": 0, "fetches_new_min": 0, "fetches_new_max": 0}
    for e in evs:
        as_of = e.as_of(lead_days)
        body = Epochal.search_body(e.search_query, as_of, opts)
        hit = ep.cached("search", as_of, body)
        if hit is None:
            est["searches_new"] += 1
            est["fetches_new_max"] += max_fetches
            continue
        est["searches_cached"] += 1
        for url in fetch_candidates(hit.get("results", []), as_of, max_fetches):
            if not ep.is_cached("fetch", as_of, Epochal.fetch_body(url, 3000)):
                est["fetches_new_min"] += 1
                est["fetches_new_max"] += 1
    est["planned_queries_max"] = plan_queries * len(evs) * n_models
    est["planned_fetches_max"] = est["planned_queries_max"] * max_fetches
    est["total_min"] = est["searches_new"] + est["fetches_new_min"]
    est["total_max"] = (
        est["searches_new"] + est["fetches_new_max"] + est["planned_queries_max"] + est["planned_fetches_max"]
    )
    return est


def plan_queries(llm: LLM, model: str, ev: Event, today: str, k: int) -> list[str]:
    from string import Template

    reply = llm.ask(model, Template(PLAN_PROMPT).substitute(today=today, question=ev.question, k=k))
    qs = [ln.strip(" -*\t\"'") for ln in reply.splitlines() if 3 < len(ln.strip()) < 200]
    return [q for q in qs if not q.lower().startswith(("here", "sure"))][:k]


def _keys_for(args, need_epochal: bool) -> tuple[str | None, str | None]:
    ekey = config.require_key(config.EPOCHAL_KEY_ENV, "Get one at https://epochal.com.") if need_epochal else None
    okey = None
    if args.estimate_only:
        return ekey, None
    if args.backend == "openrouter":
        okey = config.require_key(config.OPENROUTER_KEY_ENV, "Get one at https://openrouter.ai/keys.")
    elif args.backend == "anthropic":
        okey = config.require_key(config.ANTHROPIC_KEY_ENV, "Get one at https://console.anthropic.com/.")
    elif args.backend == "claude-cli" and not find_claude():
        raise config.MissingKeyError(
            "claude (Claude Code) is not on PATH. Install it from https://code.claude.com, run `claude` once "
            "to log in, then retry. (No API key is needed for this backend.)"
        )
    return ekey, okey


def run(args) -> int:
    config.load_dotenv(args.env_file)
    sc = load_scenario(Path(args.scenarios_dir), args.scenario)
    evs = select_events(sc, args.only, args.limit)
    lead = args.lead_days if args.lead_days is not None else sc.lead_days
    modes = args.modes.split(",") if args.modes else sc.modes
    for m in modes:
        if m not in sc.templates:
            raise SystemExit(f"scenario '{sc.name}' has no prompt for mode '{m}' (has: {', '.join(sc.templates)})")
    context = getattr(args, "context", None) or "epochal"
    pack = None
    if context == "public":
        pack = load_public_context(sc.path, sc.public_context)
        if pack is None:
            raise SystemExit(f"scenario '{sc.name}' ships no public context pack (scenario.json 'public_context')")
    shuffle = sc.shuffle and not args.no_shuffle
    opts = search_options(sc, args.num_results)
    max_fetches = sc.max_fetches if args.max_fetches is None else args.max_fetches
    cutoffs = dict(c.split("=", 1) for c in (args.cutoff or []))
    uses_epochal = "gated" in modes and context == "epochal"

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
        ekey, okey = _keys_for(args, uses_epochal and not args.estimate_only)
        ep = Epochal(cache, budget, ekey)
        llm = LLM(cache, backend=args.backend, api_key=okey)
        models = args.model or [DEFAULT_MODELS[args.backend]]

    print(
        f"Scenario {sc.name}: {len(evs)} events x {len(models)} model(s) x modes {','.join(modes)}; "
        f"context={context}; as-of = event - {lead} days; shuffle={'on' if shuffle else 'off'} (seed {args.seed})"
    )
    est = None
    if uses_epochal:
        est = estimate(ep, evs, lead, opts, max_fetches, args.plan_queries, len(models))
        rng = (
            f"{est['total_min']}" if est["total_min"] == est["total_max"] else f"{est['total_min']}-{est['total_max']}"
        )
        print(
            f"Epochal: {est['searches_new']} new + {est['searches_cached']} cached searches; fetches "
            f"{est['fetches_new_min']}-{est['fetches_new_max']}"
            f"{'; planned queries up to ' + str(est['planned_queries_max']) if args.plan_queries else ''}. "
            f"New requests needed: {rng}. Budget: {budget.remaining} available "
            f"(--budget {args.budget}, lifetime {budget.total}/{args.max_total})."
        )
        print(
            f"  search: num_results={opts.num_results}"
            f"{', categories=' + ','.join(opts.categories) if opts.categories else ''}"
            f"{', published_after=as_of-' + str(opts.published_after_days) + 'd' if opts.published_after_days else ''}"
            f"{', domains=' + str(len(opts.domains)) if opts.domains else ''}"
            f"{', exclude_domains=' + str(len(opts.exclude_domains)) if opts.exclude_domains else ''}"
        )
        if est["total_max"] > budget.remaining and not args.dry_run:
            print(
                "  The worst case is above the budget; the run stops cleanly when it runs out. "
                "Use --limit/--only, `epochal-pilot plan`, or raise --budget.",
                file=sys.stderr,
            )
    elif "gated" in modes:
        print(f"Context: public-domain pack '{sc.public_context}' ({len(pack.docs)} docs). Epochal requests: 0.")
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
                if mode == "gated" and context == "public":
                    ret = pack.retrieval(as_of)
                elif mode == "gated":
                    queries = [ev.search_query]
                    if args.plan_queries:
                        queries += plan_queries(llm, model, ev, today, args.plan_queries)
                    rkey = (ev.id, tuple(queries))
                    try:
                        if rkey not in retrievals:
                            retrievals[rkey] = retrieve(ep, queries, as_of, opts, max_fetches)
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
                try:
                    reply = llm.ask(model, prompt)
                    fc = parse_forecast(reply, shown)
                    if fc.answer is None:
                        reply2 = llm.ask(model, force_choice_prompt(prompt, reply, shown))
                        fc = parse_forecast(reply2, shown)
                        fc.retried = True
                except ClaudeCLIError as e:
                    stopped = f"claude-cli: {e}"
                    break
                ctx_urls = set(ret.urls)
                cited_ok = (sum(c in ctx_urls for c in fc.citations) / len(fc.citations)) if fc.citations else None
                rows.append(
                    {
                        "model": model,
                        "mode": mode,
                        "context": context if mode == "gated" else "none",
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
    user = load_user_baseline(Path(args.baseline_csv)) if getattr(args, "baseline_csv", None) else None
    base_rows = baseline_forecasts(scored_events, sc.baselines, user)
    scores, detail = analyse(rows, base_rows, cutoffs, seed=args.seed)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir = Path(args.out) if args.out else Path("runs") / f"{sc.name}-{context}-{stamp}"
    meta = build_manifest(sc, args, lead, budget, models, modes, shuffle, context, opts, max_fetches, llm, ep, rows)
    meta["estimate"] = est
    write_outputs(out_dir, sc, rows, base_rows, scores, detail, meta)
    print_table(scores, detail)
    if args.dry_run:
        print(f"\nDry run: {budget.this_run} fixture requests (no network, nothing spent)")
    elif uses_epochal:
        print(f"\nEpochal requests: {budget.this_run} this run, {budget.total} lifetime (cache: {cache_root})")
    else:
        print("\nEpochal requests: 0 (public context)")
    print(f"Wrote {out_dir / 'report.html'}, predictions.csv, scores.csv, stats.json, summary.md")
    return 0 if rows else 1


def build_manifest(sc, args, lead, budget, models, modes, shuffle, context, opts, max_fetches, llm, ep, rows) -> dict:
    """run.json: what ran, on which code and data, with which settings. Never any key."""
    return {
        "scenario": sc.name,
        "models": models,
        "backend": "mock" if args.dry_run else args.backend,
        "model_params": llm.params(),
        "modes": modes,
        "context": context,
        "lead_days": lead,
        "shuffle": shuffle,
        "seed": args.seed,
        "events": len({r["event"] for r in rows}),
        "event_ids": list(dict.fromkeys(r["event"] for r in rows)),
        "search": {
            "num_results": opts.num_results,
            "categories": opts.categories,
            "published_after_days": opts.published_after_days,
            "domains": opts.domains,
            "exclude_domains": opts.exclude_domains,
            "max_fetches": max_fetches,
            "plan_queries": args.plan_queries,
        }
        if context == "epochal"
        else None,
        "public_context": sc.public_context if context == "public" else None,
        "cutoff_overrides": dict(c.split("=", 1) for c in (args.cutoff or [])),
        "epochal_requests_this_run": budget.this_run if context == "epochal" else 0,
        "epochal_lifetime_used": budget.total if context == "epochal" and not args.dry_run else None,
        "epochal_ledger": ep.ledger.as_dict() if context == "epochal" else None,
        "dry_run": bool(args.dry_run),
        "hashes": {
            "events_sha256": manifest.sha256_file(sc.events_file),
            "scenario_sha256": manifest.sha256_file(sc.path / "scenario.json"),
            **{f"prompt_{m}_sha256": manifest.sha256_file(p) for m, p in sorted(sc.prompt_files.items())},
            "public_context_index_sha256": manifest.sha256_file(sc.path / sc.public_context / "index.json")
            if context == "public" and sc.public_context
            else None,
        },
        "git": manifest.git_info(),
        "environment": manifest.environment(),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_outputs(out_dir: Path, sc: Scenario, rows, base_rows, scores, detail, meta) -> None:
    from epochal_pilot import report

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "predictions.csv", rows)
    write_csv(out_dir / "baseline_predictions.csv", base_rows)
    write_csv(out_dir / "scores.csv", scores)
    (out_dir / "stats.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
    (out_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(report.markdown(meta, scores, detail, rows), encoding="utf-8")
    (out_dir / "report.html").write_text(report.html_report(meta, scores, detail, rows), encoding="utf-8")


def _fmt(x) -> str:
    return "" if x is None or x == "" else f"{x:.3f}" if isinstance(x, float) else str(x)


def _ci(s: dict, k: str) -> str:
    lo, hi = s.get(f"{k}_ci_low"), s.get(f"{k}_ci_high")
    return "" if lo is None else f"[{lo:.2f},{hi:.2f}]"


def print_table(scores: list[dict], detail: dict | None = None) -> None:
    print(
        f"\n{'model':34} {'mode':8} {'ctx':7} {'n':>3} {'acc':>6} {'95% CI':>12} {'brier':>6} {'95% CI':>12} "
        f"{'BSS':>6} {'pre':>6} {'post':>6}"
    )
    for s in scores:
        name = s["model"] + (" (hindsight)" if s.get("hindsight") and "hindsight" not in s["model"] else "")
        print(
            f"{name[:34]:34} {s['mode']:8} {s.get('context', ''):7} {s['n']:>3} {_fmt(s.get('accuracy')):>6} "
            f"{_ci(s, 'accuracy'):>12} {_fmt(s.get('brier')):>6} {_ci(s, 'brier'):>12} "
            f"{_fmt(s.get('brier_skill')):>6} {_fmt(s.get('acc_pre_cutoff')):>6} {_fmt(s.get('acc_post_cutoff')):>6}"
        )
    refs = {s["reference"] for s in scores if s.get("reference")}
    if refs:
        print(f"BSS = Brier skill vs the best non-hindsight baseline ({', '.join(sorted(refs))}); >0 beats it.")
    n_min = min((s["n"] for s in scores if s["mode"] != "baseline"), default=0)
    if detail and any(g.get("warning") for g in detail.get("groups", {}).values()):
        from epochal_pilot.stats import small_n_warning

        print("WARNING: " + small_n_warning(n_min))
