"""Command line: ``epochal-pilot list``, ``epochal-pilot run <scenario>``, ``epochal-pilot check``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from epochal_pilot import __version__, config
from epochal_pilot.cache import Budget
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
    b = Budget(Path(args.cache_dir) / "live" / "budget.json", per_run=0, max_total=0)
    print(f"  Epochal requests used (lifetime, this cache): {b.total}")
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
    r.add_argument("--backend", choices=["openrouter", "anthropic", "ollama"], default="openrouter")
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
    r.add_argument("--seed", type=int, default=0, help="answer-shuffle seed")
    r.add_argument("--no-shuffle", action="store_true", help="show options in file order")
    r.add_argument("--num-results", type=int, help="search results per query")
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
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
