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
from epochal_pilot.derived import build_sep_gap
from epochal_pilot.epochal import Epochal, SearchOptions, fixture_transport
from epochal_pilot.jev import DEFAULT_MODEL as JEV_DEFAULT_MODEL
from epochal_pilot.jev import JevClient, build_state, mock_jev_transport, usage_summary
from epochal_pilot.llm import LLM, ClaudeCLIError, find_claude, mock_transport
from epochal_pilot.masking import mask_value
from epochal_pilot.net import HTTPError
from epochal_pilot.providers import (
    SYSTEM_ONE_BACKENDS,
    JevPredictor,
    get_predictor_class,
    resolve_backend,
    system_two_notice,
    uses_rules_analysis,
)
from epochal_pilot.public_context import load_public_context
from epochal_pilot.rules import analyse_event, rules_forecast
from epochal_pilot.scenario import LETTERS, Event, Scenario, load_scenario
from epochal_pilot.scoring import analyse, baseline_forecasts, brier, direction_summary, load_user_baseline

DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4.1-mini",
    "anthropic": "claude-haiku-5-5",
    "ollama": "llama3.2:3b",
    "claude-cli": "default",
    "jev": JEV_DEFAULT_MODEL,
    "jev-rules": JEV_DEFAULT_MODEL,
    "rules": "rules",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "openai-decisions": "gpt-6-luna",
    "gemini-decisions": "gemini-decision-1",
}

# System One predictors default to public context (0 Epochal requests).
JUDGMENT_BACKENDS = SYSTEM_ONE_BACKENDS

PLAN_PROMPT = """Today's date is $today. You are researching this question using a web search engine
that only returns pages published on or before $today.

QUESTION: $question

Write $k short web search queries (one per line, no numbering, no quotes) that would find the most
useful evidence available on $today."""


def select_events(sc: Scenario, only: str | None, limit: int | None, mode: str = "backtest") -> list[Event]:
    """Events to run. ``only`` takes event ids and/or tags (e.g. ``turning-point``); ``mode`` picks
    resolved events (backtest) or unresolved ones (live)."""
    evs = [e for e in sc.events if e.resolved == (mode != "live")]
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        all_ids = {e.id for e in sc.events}
        all_tags = {t for e in sc.events for t in e.tags}
        unknown = set(want) - all_ids - all_tags
        if unknown:
            hint = f" (tags: {', '.join(sorted(all_tags))})" if all_tags else ""
            raise SystemExit(f"unknown event id(s) or tag(s): {', '.join(sorted(unknown))}{hint}")
        evs = [e for e in evs if e.id in want or set(e.tags) & set(want)]
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


def outcome_fields(probs: dict[str, float], answer: str | None, outcome: str, shown: list[str]) -> dict:
    """Scoring columns of a prediction row; blank while the outcome is unknown (live forecasts)."""
    if not outcome:
        return {"outcome": "", "truth_letter": "", "correct": "", "p_outcome": "", "brier": ""}
    return {
        "outcome": outcome,
        "truth_letter": LETTERS[shown.index(outcome)] if outcome in shown else "",
        "correct": int(answer == outcome),
        "p_outcome": round(probs.get(outcome, 0.0), 4),
        "brier": round(brier(probs, outcome), 4),
    }


NO_RESULTS = (
    "No results: nothing was scored{why}. Nothing to compare, so no table. Check the messages above "
    "(a stopped backend, an expired login, a used-up budget or a filter that matched no events), fix it and re-run."
)


def _default_model(backend: str) -> str:
    return DEFAULT_MODELS.get(backend) or "mock-model"


def _keys_for(args, need_epochal: bool) -> tuple[str | None, str | None]:
    ekey = config.require_key(config.EPOCHAL_KEY_ENV, "Get one at https://epochal.com.") if need_epochal else None
    okey = None
    if args.estimate_only:
        return ekey, None
    b = args.backend
    if b == "openrouter":
        okey = config.require_key(config.OPENROUTER_KEY_ENV, "Get one at https://openrouter.ai/keys.")
    elif b == "anthropic":
        okey = config.require_key(config.ANTHROPIC_KEY_ENV, "Get one at https://console.anthropic.com/.")
    elif b in {"jev", "jev-rules"}:
        okey = config.require_key(
            config.TYPESAFE_KEY_ENV,
            "Get one at https://typesafe.ai (TypeSafe / Jev). See https://docs.typesafe.ai",
        )
    elif b in {"openai", "openai-decisions"}:
        okey = config.require_key(config.OPENAI_KEY_ENV, "Get one at https://platform.openai.com/api-keys.")
    elif b in {"gemini", "gemini-decisions"}:
        okey = config.gemini_key()
        if not okey:
            raise config.MissingKeyError(
                "GEMINI_API_KEY is not set. Get one at https://aistudio.google.com "
                "(GOOGLE_API_KEY is also accepted). Or run with --dry-run."
            )
    elif b == "claude-cli" and not find_claude():
        raise config.MissingKeyError(
            "claude (Claude Code) is not on PATH. Install it from https://code.claude.com, run `claude` once "
            "to log in, then retry. (No API key is needed for this backend.)"
        )
    return ekey, okey


def _analyst_key(args) -> str | None:
    name = getattr(args, "analyst", None)
    if not name:
        return None
    if name == "openrouter":
        return config.require_key(config.OPENROUTER_KEY_ENV, "Get one at https://openrouter.ai/keys.")
    if name == "anthropic":
        return config.require_key(config.ANTHROPIC_KEY_ENV, "Get one at https://console.anthropic.com/.")
    if name == "claude-cli" and not find_claude():
        raise config.MissingKeyError("claude (Claude Code) is not on PATH (needed for --analyst claude-cli).")
    return None


def _make_predictor(args, model: str, cache, api_key: str | None, *, dry_run: bool):
    """Build a System One predictor. ``rules`` is handled in ``_judgment_forecast``."""
    b = args.backend
    if b in {"jev", "jev-rules"}:
        client = JevClient(
            None if dry_run else api_key,
            cache=cache,
            transport=mock_jev_transport if dry_run else None,
        )
        return JevPredictor(client, model)
    if b == "rules":
        return None
    cls = get_predictor_class(b)
    kwargs: dict = {"api_key": None if dry_run else api_key, "model": model, "cache": cache}
    if b in {"openai-decisions", "gemini-decisions"}:
        from epochal_pilot.decisions import mock_decisions_transport

        if dry_run:
            kwargs["transport"] = mock_decisions_transport
    else:
        from epochal_pilot.structured import mock_structured_transport

        if dry_run:
            kwargs["transport"] = mock_structured_transport
        if b == "openai":
            kwargs["api"] = getattr(args, "structured_api", "responses")
    return cls(**kwargs)


def run(args) -> int:
    config.load_dotenv(args.env_file)
    chosen, notice = resolve_backend(getattr(args, "backend", None))
    args.backend = chosen
    if notice:
        print(notice)
    two = system_two_notice(args.backend)
    if two:
        print(two)
    sc = load_scenario(Path(args.scenarios_dir), args.scenario)
    run_mode = getattr(args, "mode", None) or "backtest"
    evs = select_events(sc, args.only, args.limit, run_mode)
    if not evs:
        if run_mode == "live":
            print(
                f"Nothing to forecast: scenario '{sc.name}' has no unresolved (live) events"
                f"{' matching --only' if args.only else ''}. If an earlier live forecast has resolved, "
                "score it with `epochal-pilot score --latest`.",
                file=sys.stderr,
            )
        else:
            print(f"No events selected for '{sc.name}' (check --only / --limit).", file=sys.stderr)
        return 1
    lead = args.lead_days if args.lead_days is not None else sc.lead_days
    modes = args.modes.split(",") if args.modes else sc.modes
    for m in modes:
        if m not in sc.templates:
            raise SystemExit(f"scenario '{sc.name}' has no prompt for mode '{m}' (has: {', '.join(sc.templates)})")
    judgment = args.backend in JUDGMENT_BACKENDS
    if getattr(args, "no_context", False):
        context = "blind"
    elif getattr(args, "context", None):
        context = args.context
    else:
        context = "public" if judgment else "epochal"
    pack = None
    sep_pack = None
    if context == "public":
        pack = load_public_context(sc.path, sc.public_context)
        if pack is None:
            raise SystemExit(f"scenario '{sc.name}' ships no public context pack (scenario.json 'public_context')")
        sep_pack = pack
    elif context != "blind":
        sep_pack = load_public_context(sc.path, sc.public_context)
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
    jev_client = None
    predictor_key = None
    if args.dry_run:
        ep = Epochal(cache, budget, None, transport=fixture_transport)
        if judgment:
            models = args.model or [_default_model(args.backend)]
            llm = LLM(cache, backend="mock", transport=mock_transport)
        else:
            llm = LLM(cache, backend="mock", transport=mock_transport)
            models = args.model or ["mock-model"]
    else:
        ekey, okey = _keys_for(args, uses_epochal and not args.estimate_only)
        predictor_key = okey
        ep = Epochal(cache, budget, ekey)
        if judgment:
            llm = LLM(cache, backend="mock")  # unused unless --analyst
            models = args.model or [_default_model(args.backend)]
        else:
            llm = LLM(cache, backend=args.backend, api_key=okey)
            models = args.model or [DEFAULT_MODELS.get(args.backend) or "mock-model"]
    if (not args.dry_run) and getattr(args, "analyst", None):
        akey = _analyst_key(args)
        llm = LLM(
            cache,
            backend=args.analyst if not args.dry_run else "mock",
            api_key=akey,
            transport=mock_transport if args.dry_run else None,
        )
    elif args.dry_run and getattr(args, "analyst", None):
        llm = LLM(cache, backend="mock", transport=mock_transport)

    asof_txt = "fixed per event (as_of column)" if all(e.fixed_as_of for e in evs) else f"event - {lead} days"
    print(
        f"Scenario {sc.name} [{run_mode}]: {len(evs)} events x {len(models)} model(s) x modes {','.join(modes)}; "
        f"context={context}; as-of = {asof_txt}; shuffle={'on' if shuffle else 'off'} (seed {args.seed})"
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
    elif context == "public":
        print(f"Context: public-domain pack '{sc.public_context}' ({len(pack.docs)} docs). Epochal requests: 0.")
    elif context == "blind":
        print(
            "Context: none (blind: as-of date and current range only; "
            "no documents, derived facts, or rule analysis). Epochal requests: 0."
        )
    if judgment:
        mask = "on" if getattr(args, "mask_dates", False) else "off"
        print(
            f"Pipeline: {args.backend}; --mask-dates {mask}. "
            "System One predictor; jev-rules / --with-rules = analyse first-release data in code, "
            "then a Decisions/Jev Choice. rules = the same analysis with no model. "
            "openai/gemini without -decisions = schema-locked LLM fallback "
            "(not calibration-trained)."
        )
    if args.estimate_only:
        return 0

    rows: list[dict] = []
    retrievals: dict[tuple, Retrieval] = {}
    stopped = None
    for model in models:
        if stopped:
            break
        pred = None
        if judgment:
            pred = _make_predictor(args, model, cache, predictor_key, dry_run=args.dry_run)
            if args.backend in {"jev", "jev-rules"} and pred is not None:
                jev_client = pred.client  # type: ignore[attr-defined]
        for ev in evs:
            as_of = ev.as_of(lead)
            today = as_of[:10]
            shown = ev.ordered_choices(shuffle, args.seed)
            letters = "/".join(LETTERS[: len(shown)])
            for mode in modes:
                ret = Retrieval()
                if mode == "gated" and context == "public":
                    ret = pack.retrieval(as_of)
                elif mode == "gated" and context == "epochal":
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
                if mode == "gated" and context == "blind":
                    ctx_text = (
                        "(no documents; this is a blind forecast — use only the as-of date "
                        "and the policy range stated above)"
                    )
                else:
                    ctx_text = ret.context(sc.context_chars_per_doc) or "(no documents found)"
                fields = {k: v for k, v in ev.extra.items() if v}  # event columns; core fields win
                fields.update(
                    today=today,
                    as_of=as_of,
                    question=ev.question,
                    options=format_options(shown),
                    letters=letters,
                    context=ctx_text,
                    derived_facts=_derived_facts(mode, context, ev, as_of, sep_pack, sc.path),
                    response_format=RESPONSE_FORMAT.replace("$letters", letters),
                )
                prompt = sc.template(mode).safe_substitute(fields)
                extra_row: dict = {}
                try:
                    if judgment and mode == "gated":
                        fc, extra_row = _judgment_forecast(
                            args, ev, as_of, shown, fields, ret, pack or sep_pack, sc, pred, model, context, llm
                        )
                    else:
                        reply = llm.ask(model, prompt)
                        fc = parse_forecast(reply, shown)
                        if fc.answer is None:
                            reply2 = llm.ask(model, force_choice_prompt(prompt, reply, shown))
                            fc = parse_forecast(reply2, shown)
                            fc.retried = True
                except ClaudeCLIError as e:
                    stopped = f"claude-cli: {e}"
                    break
                except HTTPError as e:
                    stopped = f"{args.backend}: {e}"
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
                        "answer": fc.answer or "",
                        "answer_letter": LETTERS[shown.index(fc.answer)] if fc.answer else "",
                        **outcome_fields(fc.probabilities, fc.answer, ev.outcome, shown),
                        "abstained": int(fc.answer is None),
                        "retried": int(fc.retried),
                        "parse_mode": fc.parse_mode,
                        "n_docs": len(ret.docs),
                        "dropped_future": ret.dropped_future,
                        "citations_in_context": None if cited_ok is None else round(cited_ok, 4),
                        "probabilities": json.dumps({k: round(v, 4) for k, v in fc.probabilities.items()}),
                        "citations": " ".join(fc.citations),
                        "rationale": fc.rationale,
                        **extra_row,
                    }
                )
                if not ev.outcome:
                    mark = "-> "
                    tail = f"  p={fc.probabilities.get(fc.answer, 0.0):.2f} (live: resolves after {ev.event_date})"
                else:
                    mark = "ok " if fc.answer == ev.outcome else ("-- " if fc.answer else "?? ")
                    tail = f"  p(true)={rows[-1]['p_outcome']:.2f}"
                print(f"  {mark}{model:32} {mode:8} {ev.id:22} -> {fc.answer or '(abstain)'}{tail}")
            if stopped:
                break
    if stopped:
        print(f"\nStopped early: {stopped}. Partial results are kept below.", file=sys.stderr)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    tag = f"{sc.name}-live" if run_mode == "live" else sc.name
    out_dir = Path(args.out) if args.out else Path("runs") / f"{tag}-{context}-{stamp}"
    meta = build_manifest(sc, args, lead, budget, models, modes, shuffle, context, opts, max_fetches, llm, ep, rows)
    meta["estimate"] = est
    meta["run_mode"] = run_mode
    meta["stopped"] = stopped
    meta["mask_dates"] = bool(getattr(args, "mask_dates", False))
    meta["pipeline"] = args.backend if args.backend in JUDGMENT_BACKENDS else None
    with_rules = uses_rules_analysis(args.backend, getattr(args, "with_rules", False))
    meta["role"] = "system-one" if args.backend in JUDGMENT_BACKENDS else "system-two"
    meta["with_rules"] = bool(with_rules)
    meta["analyst"] = getattr(args, "analyst", None)
    if args.backend in {"jev", "jev-rules"}:
        meta["model_params"] = {
            "endpoint": "https://api.typesafe.ai/v1/systemone",
            "price_per_mtok_input": 0.042,
            "output_tokens_billed": False,
            "mask_dates": bool(getattr(args, "mask_dates", False)),
            "includes_rule_analysis": with_rules,
        }
    elif args.backend == "openai-decisions":
        meta["model_params"] = {
            "endpoint": "https://api.openai.com/v1/decisions",
            "model": "gpt-6-luna",
            "price_per_mtok_input": 0.10,
            "output_tokens_billed": False,
            "includes_rule_analysis": with_rules,
        }
    elif args.backend == "gemini-decisions":
        meta["model_params"] = {
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/decisions",
            "model": "gemini-decision-1",
            "price_per_mtok_input": 0.10,
            "output_tokens_billed": False,
            "calibrated_claim": True,
            "includes_rule_analysis": with_rules,
        }
    elif args.backend in {"openai", "gemini"}:
        meta["model_params"] = {
            "label": "LLM as System One (not calibration-trained)",
            "structured_output": True,
            "includes_rule_analysis": with_rules,
        }
    elif args.backend == "rules":
        meta["model_params"] = {"deterministic": True, "hindsight_checklist": True}
    if jev_client is not None:
        meta["jev_usage"] = usage_summary(jev_client)
    if run_mode == "live":
        write_live(out_dir, sc, rows, meta)
        _print_spend(args, uses_epochal, budget, cache_root, jev_client)
        if not rows:
            print(NO_RESULTS.format(why=f" ({stopped})" if stopped else ""))
            return 1
        print(
            f"Wrote {out_dir / 'forecast.csv'} ({len(rows)} forecast(s), pending). After the quarter ends, add the "
            f"outcome to scenarios/{sc.name}/events.csv and run: epochal-pilot score {out_dir}"
        )
        return 0

    by_id = {e.id: e for e in sc.events}
    scored_events = [by_id[i] for i in dict.fromkeys(r["event"] for r in rows)]
    user = load_user_baseline(Path(args.baseline_csv)) if getattr(args, "baseline_csv", None) else None
    base_rows = baseline_forecasts(scored_events, sc.baselines, user)
    scores, detail = analyse(rows, base_rows, cutoffs, seed=args.seed)
    if sc.config.get("directions"):
        detail["direction"] = direction_summary(rows + base_rows, sc.config["directions"], sc.config.get("choice_bp"))
    write_outputs(out_dir, sc, rows, base_rows, scores, detail, meta)
    if not rows:
        print("\n" + NO_RESULTS.format(why=f" ({stopped})" if stopped else ""))
    else:
        print_table(scores, detail)
        print_direction(detail.get("direction"))
    _print_spend(args, uses_epochal, budget, cache_root, jev_client)
    print(f"Wrote {out_dir / 'report.html'}, predictions.csv, scores.csv, stats.json, summary.md")
    return 0 if rows else 1


def _judgment_forecast(args, ev, as_of, shown, fields, ret, pack, sc, predictor, model, context, llm=None):
    """Official pipeline (steps 2-4): gather, analyse in code, then a System One predictor."""
    include_docs = context != "blind"
    include_rules = uses_rules_analysis(args.backend, getattr(args, "with_rules", False)) and include_docs
    meetings_path = sc.path / "fomc_meetings.csv"
    signals = None
    if include_docs and include_rules:
        signals = analyse_event(
            ev=ev,
            as_of=as_of,
            pack=pack,
            meetings_path=meetings_path if meetings_path.is_file() else None,
            choices=shown,
        )
    extra = {"mask_dates": int(bool(getattr(args, "mask_dates", False)))}
    if signals is not None:
        extra["rules_bucket"] = signals.bucket
        extra["rules_expected_bp"] = signals.expected_bp
        extra["rule_signals"] = json.dumps(signals.as_state())
    if args.backend == "rules":
        if signals is None:
            from datetime import date as _date

            from epochal_pilot.rules import RuleSignals

            signals = RuleSignals(as_of=_date.fromisoformat(as_of[:10]), target=ev.event_date)
            signals.notes = ["blind or no pack: defaulting to no change"]
        fc = rules_forecast(signals, shown)
        return fc, extra
    state = build_state(
        as_of=as_of,
        target=ev.event_date.isoformat(),
        range_at_asof=fields.get("range_at_asof") or "",
        question=ev.question,
        meetings=fields.get("meetings_in_window") or "",
        derived_facts=fields.get("derived_facts") or "",
        documents=ret.docs,
        signals=signals,
        include_docs=include_docs,
        include_rules=include_rules,
        max_chars_per_doc=sc.context_chars_per_doc,
    )
    if getattr(args, "mask_dates", False):
        state = mask_value(state, as_of)
    if predictor is None:
        raise RuntimeError(f"no System One predictor for backend {args.backend!r}")
    result = predictor.predict(state, ev.question, shown)
    fc = result.as_forecast()
    meta = result.metadata or {}
    if meta.get("jev_model"):
        extra["jev_model"] = meta["jev_model"]
    if meta.get("model"):
        extra["jev_model"] = extra.get("jev_model") or meta["model"]
    if meta.get("confidence") is not None:
        extra["jev_confidence"] = meta["confidence"]
    if meta.get("input_tokens") is not None:
        extra["input_tokens"] = meta["input_tokens"]
    if meta.get("output_tokens") is not None:
        extra["output_tokens"] = meta["output_tokens"]
    if meta.get("provider"):
        extra["provider"] = meta["provider"]
    judgments = meta.get("judgments") or {}
    if judgments:
        extra["judgments"] = json.dumps(judgments)
    if getattr(args, "analyst", None) and llm is not None:
        analyst_model = DEFAULT_MODELS.get(args.analyst, "mock-model")
        prompt = (
            "You are a System Two analyst. The System One predictor already produced a forecast. "
            "Write a short rationale. Do not change the probabilities or pick a new option.\n\n"
            f"Question: {ev.question}\nTop choice: {fc.answer}\n"
            f"Probabilities: {json.dumps(fc.probabilities)}\n"
        )
        note = llm.ask(analyst_model, prompt)
        extra["analyst"] = (note or "")[:2000]
        extra["analyst_backend"] = args.analyst
        fc.rationale = ((fc.rationale or "") + " | analyst: " + (note or ""))[:500]
    return fc, extra


def _derived_facts(mode: str, context: str, ev: Event, as_of: str, pack, scenario_path: Path) -> str:
    """SEP-gap and remaining-meeting line for gated, non-blind runs. Empty otherwise (no leakage path)."""
    if mode != "gated" or context == "blind":
        return ""
    meetings = scenario_path / "fomc_meetings.csv"
    has_sep = pack is not None and any(d.kind == "fomc_sep" for d in pack.docs)
    if not meetings.is_file() and not has_sep:
        return ""
    return build_sep_gap(
        as_of=as_of,
        target=ev.event_date,
        range_at_asof=(ev.extra.get("range_at_asof") or ""),
        pack=pack,
        meetings=meetings if meetings.is_file() else None,
    ).text


def _print_spend(args, uses_epochal: bool, budget: Budget, cache_root: Path, jev_client=None) -> None:
    if args.dry_run:
        print(f"\nDry run: {budget.this_run} fixture requests (no network, nothing spent)")
    elif uses_epochal:
        print(f"\nEpochal requests: {budget.this_run} this run, {budget.total} lifetime (cache: {cache_root})")
    elif getattr(args, "no_context", False):
        print("\nEpochal requests: 0 (blind, no context)")
    else:
        print("\nEpochal requests: 0 (public context)")
    if jev_client is not None and jev_client.calls:
        u = usage_summary(jev_client)
        print(
            f"Jev usage: {u['calls']} call(s), {u['input_tokens']} input tokens, "
            f"{u['output_tokens']} output tokens (free); approx ${u['approx_cost_usd']:.4f} "
            f"at ${u['price_per_mtok_input']}/Mtok input"
        )


def print_direction(d: dict | None) -> None:
    if not d:
        return
    print(f"\n{'forecaster':34} {'n':>3} {'direction acc':>13} {'mean |error| bp':>15}")
    for name, v in d.items():
        mae = "" if v.get("mae_bp") is None else f"{v['mae_bp']:.1f}"
        print(f"{name[:34]:34} {v['n']:>3} {v['direction_accuracy']:>13.3f} {mae:>15}")
    print("Direction = down / hold / up of the top choice vs the truth; |error| uses each option's bp size.")


def write_live(out_dir: Path, sc: Scenario, rows: list[dict], meta: dict) -> None:
    """A live forecast: timestamped, unscored, for ``epochal-pilot score`` to resolve later."""
    created = meta["created"]
    out_dir.mkdir(parents=True, exist_ok=True)
    live_rows = [{"status": "pending", "forecast_created": created, **r} for r in rows]
    write_csv(out_dir / "forecast.csv", live_rows)
    (out_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    by_id = {e.id: e for e in sc.events}
    L = [f"# {sc.name} live forecast", "", f"Made {created}, before the outcome was known. Status: pending.", ""]
    L += ["| event | as-of | resolves after | model | answer | p |", "|---|---|---|---|---|---|"]
    for r in rows:
        probs = json.loads(r["probabilities"] or "{}")
        L.append(
            f"| {r['event']} | {r['as_of'][:10]} | {by_id[r['event']].event_date} | {r['model']} | "
            f"{r['answer'] or '(abstain)'} | {probs.get(r['answer'], 0.0):.2f} |"
        )
    L += ["", f"Score it after the quarter ends: `epochal-pilot score {out_dir}`.", ""]
    (out_dir / "summary.md").write_text("\n".join(L), encoding="utf-8")


def cmd_score(args) -> int:
    """Resolve a live forecast folder against the scenario's (now updated) outcomes and score it."""
    from epochal_pilot.report import latest_run, read_csv

    run_dir = latest_run(Path(args.runs_dir), live=True) if args.latest or not args.run_dir else Path(args.run_dir)
    if run_dir is None:
        print(f"error: no live forecast folders under {args.runs_dir}", file=sys.stderr)
        return 2
    fpath = run_dir / "forecast.csv"
    if not fpath.is_file():
        raise FileNotFoundError(f"{run_dir}: no forecast.csv (score is for `run --mode live` folders)")
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    sc = load_scenario(Path(args.scenarios_dir), meta["scenario"])
    by_id = {e.id: e for e in sc.events}
    rows = read_csv(fpath)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    scored, pending = [], []
    for r in rows:
        ev = by_id.get(r["event"])
        if ev is None or not ev.resolved:
            r["status"] = "pending"
            pending.append(r)
            continue
        probs = json.loads(r["probabilities"] or "{}")
        shown = list(probs) or ev.choices
        r.update(outcome_fields(probs, r["answer"] or None, ev.outcome, shown))
        r["status"] = "resolved"
        r["resolved_at"] = r.get("resolved_at") or now
        scored.append(r)
    write_csv(fpath, rows)
    if pending:
        waits = sorted({by_id[r["event"]].event_date.isoformat() for r in pending if r["event"] in by_id})
        print(
            f"{len(pending)} forecast(s) still pending (resolve after {', '.join(waits)}). Add the outcome to "
            f"scenarios/{sc.name}/events.csv (or rebuild it with the scenario's tool) and re-run this command."
        )
    if not scored:
        print("No results yet: every forecast in this folder is still pending, so there is nothing to score.")
        return 1
    pred = [{k: v for k, v in r.items() if k not in ("status", "forecast_created", "resolved_at")} for r in scored]
    for r in pred:
        r["correct"], r["brier"], r["p_outcome"] = int(r["correct"]), float(r["brier"]), float(r["p_outcome"])
    events = [by_id[i] for i in dict.fromkeys(r["event"] for r in pred)]
    base_rows = baseline_forecasts(events, sc.baselines)
    scores, detail = analyse(pred, base_rows, meta.get("cutoff_overrides") or {}, seed=int(meta.get("seed", 0) or 0))
    if sc.config.get("directions"):
        detail["direction"] = direction_summary(pred + base_rows, sc.config["directions"], sc.config.get("choice_bp"))
    meta["resolved"] = now
    write_outputs(run_dir, sc, pred, base_rows, scores, detail, meta)
    print(f"Resolved {len(scored)} forecast(s) in {run_dir}.")
    print_table(scores, detail)
    print_direction(detail.get("direction"))
    print(f"Wrote {run_dir / 'report.html'}, predictions.csv, scores.csv, summary.md")
    return 0


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
