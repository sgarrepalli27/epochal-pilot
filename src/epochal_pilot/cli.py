"""Command line.

epochal-pilot list | check
epochal-pilot run <scenario> [--context epochal|public] [--dry-run] [--estimate]
epochal-pilot plan <scenario> --requests N
epochal-pilot report <run_dir> [--share]
epochal-pilot compare <run_dir> <run_dir> ...
epochal-pilot validate <scenario>
epochal-pilot new <name>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from epochal_pilot import __version__, config
from epochal_pilot.cache import Budget
from epochal_pilot.llm import ClaudeCLIError, find_claude
from epochal_pilot.scenario import discover, load_scenario


def default_scenarios_dir() -> str:
    here = Path.cwd() / "scenarios"
    if here.is_dir():
        return str(here)
    repo = Path(__file__).resolve().parents[2] / "scenarios"  # src/epochal_pilot/cli.py -> repo root
    return str(repo)


def cmd_list(args) -> int:
    d = Path(args.scenarios_dir)
    for name in discover(d):
        sc = load_scenario(d, name)
        print(f"{name:16} {len(sc.events):3} events  modes={','.join(sc.modes):15} {sc.description}")
    return 0


def cmd_check(args) -> int:
    config.load_dotenv(args.env_file)
    print(f"epochal-pilot {__version__}")
    for name in (config.EPOCHAL_KEY_ENV, config.OPENROUTER_KEY_ENV, config.ANTHROPIC_KEY_ENV):
        print(f"  {name:20} {config.mask(os.environ.get(name))}")
    claude = find_claude()
    print(
        f"  {'claude (Claude Code)':20} "
        f"{'on PATH: ' + claude if claude else 'not found (needed only for --backend claude-cli)'}"
    )
    b = Budget(Path(args.cache_dir) / "live" / "budget.json", per_run=0, max_total=0)
    print(f"  Epochal requests used (lifetime, this cache): {b.total} (free tier: 100 per account)")
    print(f"  scenarios: {', '.join(discover(Path(args.scenarios_dir)))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epochal-pilot", description="Backtest LLM agents against the web as of a past date."
    )
    p.add_argument("--version", action="version", version=f"epochal-pilot {__version__}")
    p.add_argument("--scenarios-dir", default=default_scenarios_dir())
    p.add_argument("--cache-dir", default=os.environ.get("EPOCHAL_PILOT_CACHE_DIR", ".epochal-pilot-cache"))
    p.add_argument("--env-file", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list scenarios").set_defaults(func=cmd_list)
    sub.add_parser("check", help="show which keys are set (never their values) and budget used").set_defaults(
        func=cmd_check
    )

    r = sub.add_parser("run", help="run a scenario")
    r.add_argument("scenario")
    r.add_argument("--model", action="append", help="model id; repeat for several (default per backend)")
    r.add_argument(
        "--backend",
        choices=["openrouter", "anthropic", "ollama", "claude-cli"],
        default="openrouter",
        help="openrouter (key), anthropic (key), ollama (local), claude-cli (local Claude Code login, tools off)",
    )
    r.add_argument(
        "--context",
        choices=["epochal", "public"],
        default="epochal",
        help="gated-mode context: the Epochal time-gated web, or the shipped public-domain pack (0 requests)",
    )
    r.add_argument("--baseline-csv", help="extra baseline forecasts you supply (name,event,<choice>...)")
    r.add_argument("--dry-run", action="store_true", help="offline fixtures + mock model; no keys, no spend")
    r.add_argument("--estimate", dest="estimate_only", action="store_true", help="show Epochal cost and exit")
    r.add_argument("--budget", type=int, default=10, help="max NEW Epochal requests this run (default 10)")
    r.add_argument(
        "--max-total",
        type=int,
        default=int(os.environ.get("EPOCHAL_PILOT_MAX_TOTAL", 90)),
        help="lifetime cap on Epochal requests for this cache (default 90; free tier is 100)",
    )
    r.add_argument("--lead-days", type=int, help="as-of = event date minus this (default from scenario)")
    r.add_argument("--modes", help="comma list: gated,ungated (default from scenario)")
    r.add_argument("--only", help="comma-separated event ids")
    r.add_argument("--limit", type=int, help="first N events")
    r.add_argument("--seed", type=int, default=0, help="answer-shuffle and bootstrap seed")
    r.add_argument("--no-shuffle", action="store_true", help="show options in file order")
    r.add_argument("--num-results", type=int, help="search results per query (1-10, default 10: same cost)")
    r.add_argument("--max-fetches", type=int, help="full-page fetches per event for thin results")
    r.add_argument(
        "--plan-queries",
        type=int,
        default=0,
        metavar="K",
        help="let the model write K extra search queries per event (costs K more requests)",
    )
    r.add_argument("--cutoff", action="append", metavar="MODEL=YYYY-MM", help="override a model's training cutoff")
    r.add_argument("--out", help="output directory (default runs/<scenario>-<time>)")
    r.set_defaults(func=lambda a: __import__("epochal_pilot.runner", fromlist=["run"]).run(a))

    pl = sub.add_parser("plan", help="choose events/lead times that make the most of N Epochal requests")
    pl.add_argument("scenario")
    pl.add_argument("--requests", type=int, required=True, help="Epochal requests you want to spend")
    pl.add_argument("--model", action="append", help="model(s) you will run (for cutoffs); repeatable")
    pl.add_argument("--lead-days", help="comma list, e.g. 7,28 (default from scenario)")
    pl.add_argument("--fetches-per-event", type=float, default=1.0, help="expected fetches per new search")
    pl.set_defaults(func=lambda a: __import__("epochal_pilot.planner", fromlist=["cmd_plan"]).cmd_plan(a))

    rp = sub.add_parser("report", help="HTML + markdown report for a run folder (offline)")
    rp.add_argument("run_dir")
    rp.add_argument("--share", action="store_true", help="also write share/: metrics only, no Epochal content")
    rp.set_defaults(func=lambda a: __import__("epochal_pilot.report", fromlist=["cmd_report"]).cmd_report(a))

    cp = sub.add_parser("compare", help="leaderboard + paired tests across run folders")
    cp.add_argument("run_dirs", nargs="+")
    cp.add_argument("--out", help="write compare.html and compare.md here")
    cp.set_defaults(func=lambda a: __import__("epochal_pilot.report", fromlist=["cmd_compare"]).cmd_compare(a))

    v = sub.add_parser("validate", help="check a scenario: schema, dates vs cutoffs, wording, context leaks")
    v.add_argument("scenario")
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")
    v.set_defaults(func=lambda a: __import__("epochal_pilot.validate", fromlist=["cmd_validate"]).cmd_validate(a))

    n = sub.add_parser("new", help="scaffold a new scenario folder")
    n.add_argument("name")
    n.set_defaults(func=lambda a: __import__("epochal_pilot.validate", fromlist=["cmd_new"]).cmd_new(a))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows consoles may use a legacy code page: never crash on non-ASCII event names.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", line_buffering=True)
    try:
        return args.func(args)
    except config.MissingKeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ClaudeCLIError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
