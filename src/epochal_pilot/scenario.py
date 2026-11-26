"""Scenario plugins: ``scenarios/<name>/`` with scenario.json, events.csv and prompt templates."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from string import Template

LETTERS = "ABCDEFGH"
REQUIRED_COLUMNS = {"id", "event_date", "question", "outcome", "source_url"}


@dataclass
class Event:
    id: str
    event_date: date
    question: str
    choices: list[str]
    outcome: str
    source_url: str
    search_query: str = ""
    category: str = ""
    extra: dict = field(default_factory=dict)

    def as_of(self, lead_days: int) -> str:
        """Accept-Datetime value: midnight UTC, ``lead_days`` before the event."""
        return (self.event_date - timedelta(days=lead_days)).isoformat() + "T00:00:00Z"

    def ordered_choices(self, shuffle: bool, seed: int) -> list[str]:
        """Choice order shown to the model. Seeded per event, so runs are reproducible."""
        if not shuffle:
            return list(self.choices)
        h = int(hashlib.sha256(f"{seed}:{self.id}".encode()).hexdigest()[:16], 16)
        order = list(self.choices)
        random.Random(h).shuffle(order)
        return order


@dataclass
class Scenario:
    name: str
    path: Path
    description: str
    events: list[Event]
    lead_days: int = 7
    modes: list[str] = field(default_factory=lambda: ["gated"])
    shuffle: bool = True
    baselines: dict = field(default_factory=dict)
    num_results: int = 5
    max_fetches: int = 2
    templates: dict[str, Template] = field(default_factory=dict)

    def template(self, mode: str) -> Template:
        return self.templates[mode]


def discover(scenarios_dir: Path) -> list[str]:
    return sorted(p.name for p in Path(scenarios_dir).iterdir() if (p / "scenario.json").is_file())


def load_scenario(scenarios_dir: Path, name: str) -> Scenario:
    root = Path(scenarios_dir) / name
    cfg_path = root / "scenario.json"
    if not cfg_path.is_file():
        avail = ", ".join(discover(scenarios_dir)) if Path(scenarios_dir).is_dir() else "none"
        raise FileNotFoundError(f"unknown scenario '{name}' in {scenarios_dir} (available: {avail})")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    default_choices = cfg.get("choices")
    events = load_events(root / cfg.get("events", "events.csv"), default_choices)
    modes = cfg.get("modes", ["gated"])
    templates = {}
    for mode in modes:
        tpl = root / cfg.get("prompts", {}).get(mode, f"prompt_{mode}.md")
        templates[mode] = Template(tpl.read_text(encoding="utf-8"))
    return Scenario(
        name=name,
        path=root,
        description=cfg.get("description", ""),
        events=events,
        lead_days=int(cfg.get("lead_days", 7)),
        modes=modes,
        shuffle=bool(cfg.get("shuffle", True)),
        baselines=cfg.get("baselines", {}),
        num_results=int(cfg.get("num_results", 5)),
        max_fetches=int(cfg.get("max_fetches", 2)),
        templates=templates,
    )


def load_events(path: Path, default_choices: list[str] | None = None) -> list[Event]:
    events: list[Event] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        for row in reader:
            choices = row["choices"].split("|") if row.get("choices") else list(default_choices or [])
            if not 2 <= len(choices) <= len(LETTERS):
                raise ValueError(f"{row['id']}: need 2-{len(LETTERS)} choices")
            if row["outcome"] not in choices:
                raise ValueError(f"{row['id']}: outcome {row['outcome']!r} not among choices")
            known = REQUIRED_COLUMNS | {"choices", "search_query", "category"}
            events.append(
                Event(
                    id=row["id"],
                    event_date=date.fromisoformat(row["event_date"]),
                    question=row["question"],
                    choices=choices,
                    outcome=row["outcome"],
                    source_url=row["source_url"],
                    search_query=row.get("search_query") or row["question"],
                    category=row.get("category", ""),
                    extra={k: v for k, v in row.items() if k not in known},
                )
            )
    ids = [e.id for e in events]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate event ids")
    return events
