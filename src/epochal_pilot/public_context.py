"""Public-domain context packs: dated US-government documents shipped with a scenario.

A pack lives in ``scenarios/<name>/public_context/`` as ``index.json`` plus ``docs/*.md``. For an event,
the agent gets the most recent document of each kind whose public release date is strictly before
the as-of date. Nothing else is ever selected, so the leak check is structural, and ``validate``
re-checks it for every event.

Using a pack costs zero Epochal requests: it is the baseline that shows what the time-gated web adds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from epochal_pilot.agent import Retrieval


@dataclass
class PublicDoc:
    id: str
    kind: str
    published: date
    title: str
    source_url: str
    path: Path

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")


class PublicContext:
    def __init__(self, root: Path):
        self.root = Path(root)
        meta = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.meta = {k: v for k, v in meta.items() if k != "docs"}
        self.docs = [
            PublicDoc(
                id=d["id"],
                kind=d["kind"],
                published=date.fromisoformat(d["published"]),
                title=d["title"],
                source_url=d["source_url"],
                path=self.root / d["file"],
            )
            for d in meta["docs"]
        ]
        self.kinds = list(meta.get("kinds") or dict.fromkeys(d.kind for d in self.docs))

    def select(self, as_of: str, per_kind: int = 1) -> list[PublicDoc]:
        """Latest ``per_kind`` docs of each kind released strictly before the as-of date."""
        gate = date.fromisoformat(as_of[:10])
        out: list[PublicDoc] = []
        for kind in self.kinds:
            prior = sorted((d for d in self.docs if d.kind == kind and d.published < gate), key=lambda d: d.published)
            out += prior[-per_kind:]
        return out

    def retrieval(self, as_of: str, per_kind: int = 1) -> Retrieval:
        ret = Retrieval()
        for d in self.select(as_of, per_kind):
            ret.docs.append(
                {"title": d.title, "url": d.source_url, "published_at": d.published.isoformat(), "text": d.text()}
            )
        return ret

    def leak_check(self, as_ofs: dict[str, str]) -> list[str]:
        """Problems for {event_id: as_of}: any selected doc on/after as-of, missing files, or missing kinds."""
        problems = []
        for d in self.docs:
            if not d.path.is_file():
                problems.append(f"{d.id}: file missing ({d.path.name})")
            if not d.source_url.startswith("https://"):
                problems.append(f"{d.id}: source_url must be https")
        for eid, as_of in as_ofs.items():
            gate = date.fromisoformat(as_of[:10])
            chosen = self.select(as_of)
            for d in chosen:
                if d.published >= gate:  # cannot happen via select(); guards future edits
                    problems.append(f"{eid}: {d.id} published {d.published} is not before as-of {gate}")
            missing = set(self.kinds) - {d.kind for d in chosen}
            if missing:
                problems.append(f"{eid}: no document before {gate} for kind(s) {sorted(missing)}")
        return problems

    def size(self) -> dict:
        by_kind: dict[str, int] = {}
        for d in self.docs:
            by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
        chars = sum(len(d.text()) for d in self.docs if d.path.is_file())
        return {"docs": len(self.docs), "by_kind": by_kind, "chars": chars}


def load_public_context(scenario_path: Path, rel: str | None) -> PublicContext | None:
    if not rel:
        return None
    root = Path(scenario_path) / rel
    return PublicContext(root) if (root / "index.json").is_file() else None
