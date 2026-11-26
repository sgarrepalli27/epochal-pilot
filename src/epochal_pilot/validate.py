"""``epochal-pilot validate`` and ``epochal-pilot new``: keep scenarios honest and easy to add."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from epochal_pilot.public_context import load_public_context
from epochal_pilot.runner import search_options
from epochal_pilot.scenario import load_scenario
from epochal_pilot.scoring import is_post_cutoff, models_table

# Wording that reads as if the outcome already happened (it can cue memorised answers).
PAST_TENSE = re.compile(
    r"^\s*(did|was|were|had)\b|\b(won|decided|announced|reported|received|gave|introduced|named|beat|missed|"
    r"happened|occurred|ended up)\b",
    re.IGNORECASE,
)
FUTURE_HINT = re.compile(r"\b(will|would|to be|going to)\b", re.IGNORECASE)


def wording_issues(question: str, outcome: str, choices: list[str]) -> list[str]:
    issues = []
    if PAST_TENSE.search(question) and not FUTURE_HINT.search(question):
        issues.append("past-tense wording; ask in the future tense as of the as-of date ('will ...?')")
    q = question.lower()
    others = [c for c in choices if c != outcome]
    if len(outcome) > 3 and outcome.lower() in q and not any(c.lower() in q for c in others):
        issues.append(f"question mentions the outcome '{outcome}' but no other option")
    return issues


def validate_scenario(scenarios_dir: Path, name: str, today: date | None = None) -> tuple[list[str], list[str], dict]:
    """(errors, warnings, info)."""
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []
    info: dict = {}
    try:
        sc = load_scenario(Path(scenarios_dir), name)
    except (FileNotFoundError, ValueError, KeyError) as e:
        return [f"cannot load: {e}"], [], {}
    info["events"] = len(sc.events)
    if not sc.events:
        errors.append("no events")
    try:
        search_options(sc)
    except SystemExit as e:
        errors.append(str(e))
    for ev in sc.events:
        if not re.match(r"^https?://\S+$", ev.source_url or ""):
            errors.append(f"{ev.id}: source_url missing or not a URL")
        if not ev.question.strip():
            errors.append(f"{ev.id}: empty question")
        if len(set(ev.choices)) != len(ev.choices):
            errors.append(f"{ev.id}: duplicate choices")
        if ev.event_date >= today:
            errors.append(f"{ev.id}: event date {ev.event_date} is not in the past (outcome unknown)")
        for issue in wording_issues(ev.question, ev.outcome, ev.choices):
            warnings.append(f"{ev.id}: {issue}")
        if "gated" in sc.modes and not ev.search_query:
            warnings.append(f"{ev.id}: no search_query (the question is used)")
    for mode, tpl in sc.templates.items():
        if "$question" not in tpl.template or "$options" not in tpl.template:
            errors.append(f"prompt_{mode}: needs $question and $options")
        if mode == "gated" and "$context" not in tpl.template:
            errors.append("prompt_gated: needs $context")
    # Dates vs model cutoffs.
    table = models_table()
    per_model = {}
    for m, v in table.items():
        post = [e.id for e in sc.events if is_post_cutoff(e.event_date, v["cutoff"])]
        per_model[m] = {"cutoff": v["cutoff"], "post_cutoff": len(post), "pre_cutoff": len(sc.events) - len(post)}
    info["cutoffs"] = per_model
    if per_model and not any(v["post_cutoff"] for v in per_model.values()):
        warnings.append("every event is before every known model cutoff: results can't separate skill from memory")
    # Public context pack: every selected doc must predate the as-of date.
    pack = load_public_context(sc.path, sc.public_context)
    if sc.public_context and pack is None:
        errors.append(f"public_context '{sc.public_context}' has no index.json")
    if pack is not None:
        problems = pack.leak_check({e.id: e.as_of(sc.lead_days) for e in sc.events})
        errors += [f"public_context: {p}" for p in problems]
        info["public_context"] = pack.size()
    return errors, warnings, info


def cmd_validate(args) -> int:
    errors, warnings, info = validate_scenario(Path(args.scenarios_dir), args.scenario)
    print(f"Scenario {args.scenario}: {info.get('events', 0)} events")
    if info.get("cutoffs"):
        print(f"\n{'model':36} {'cutoff':8} {'pre':>4} {'post':>4}")
        for m, v in info["cutoffs"].items():
            print(f"{m:36} {v['cutoff']:8} {v['pre_cutoff']:>4} {v['post_cutoff']:>4}")
    if info.get("public_context"):
        pc = info["public_context"]
        print(
            f"\npublic context: {pc['docs']} docs, {pc['chars']:,} chars, {pc['by_kind']}; leak check: "
            f"{'FAILED' if any('public_context' in e for e in errors) else 'every doc predates its as-of date'}"
        )
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    strict_fail = args.strict and warnings
    print(f"\n{'FAIL' if errors or strict_fail else 'OK'}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors or strict_fail else 0


SCENARIO_JSON = {
    "description": "One line: what this scenario forecasts.",
    "events": "events.csv",
    "lead_days": 7,
    "modes": ["gated", "ungated"],
    "prompts": {"gated": "prompt_gated.md", "ungated": "prompt_ungated.md"},
    "shuffle": True,
    "num_results": 10,
    "max_fetches": 2,
    "published_after_days": 30,
    "categories": [],
    "domains": [],
    "exclude_domains": [],
    "baselines": {"majority": True, "uniform": True},
    "verified": "Say how and when each outcome was checked against its source_url.",
}
EVENTS_CSV = (
    "id,event_date,question,choices,outcome,search_query,source_url\n"
    'example_1,2025-01-15,"Will <something> happen by 15 January 2025?",Yes|No,Yes,'
    "<a query someone would have typed a week before>,https://example.com/source-that-settles-it\n"
)
PROMPT_GATED = """Today's date is $today. You only know what is in the context below, retrieved from the
web as it existed on $today. Do not use any knowledge of events after $today.

CONTEXT:
$context

QUESTION: $question
$options

You must pick exactly one option even if uncertain.

$response_format
"""
PROMPT_UNGATED = """Today's date is $today. Do not use any knowledge of events after $today.

QUESTION: $question
$options

You must pick exactly one option even if uncertain.

$response_format
"""
README = """# {name}

Describe the question set, why it is a good test, and how outcomes were verified.

* Ask every question in the **future tense** as of the as-of date ("will ...?").
* Give every event a `source_url` that settles the outcome.
* Run `epochal-pilot validate {name}` before opening a pull request.
"""


def cmd_new(args) -> int:
    name = args.name.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name):
        print("error: use a short lowercase name with letters, digits and dashes (e.g. cpi-surprises)")
        return 2
    root = Path(args.scenarios_dir) / name
    if root.exists():
        print(f"error: {root} already exists")
        return 2
    root.mkdir(parents=True)
    (root / "scenario.json").write_text(json.dumps(SCENARIO_JSON, indent=2) + "\n", encoding="utf-8")
    (root / "events.csv").write_text(EVENTS_CSV, encoding="utf-8", newline="\n")
    (root / "prompt_gated.md").write_text(PROMPT_GATED, encoding="utf-8", newline="\n")
    (root / "prompt_ungated.md").write_text(PROMPT_UNGATED, encoding="utf-8", newline="\n")
    (root / "README.md").write_text(README.format(name=name), encoding="utf-8", newline="\n")
    print(f"Created {root}/ (scenario.json, events.csv, prompt_gated.md, prompt_ungated.md, README.md)")
    print(f"Next: edit events.csv, then `epochal-pilot validate {name}` and `epochal-pilot run {name} --dry-run`.")
    return 0
