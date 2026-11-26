"""The as-of agent: retrieve from the frozen web, prompt the model, parse a structured forecast."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from epochal_pilot.cache import BudgetExceeded
from epochal_pilot.epochal import Epochal, SearchOptions
from epochal_pilot.net import HTTPError
from epochal_pilot.scenario import LETTERS

CONTEXT_CHARS_PER_DOC = 3000
MIN_TEXT_CHARS = 300  # below this, try a fetch for the full archived page

RESPONSE_FORMAT = """Reply with ONLY a JSON object, no other text:
{"answer": "<one letter from $letters>",
 "probabilities": {<every letter from $letters>: <probability>, ...},
 "citations": ["<URL from the context you relied on>", ...],
 "rationale": "<one or two sentences>"}
Probabilities must be between 0 and 1 and sum to 1."""


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Retrieval:
    docs: list[dict] = field(default_factory=list)  # {title, url, published_at, text}
    dropped_future: int = 0  # results dated after as-of (leak guard)
    undated: int = 0
    fetch_failures: int = 0
    search_failures: int = 0

    @property
    def urls(self) -> list[str]:
        return [d["url"] for d in self.docs if d.get("url")]

    def context(self, max_chars_per_doc: int = CONTEXT_CHARS_PER_DOC) -> str:
        parts = []
        for d in self.docs:
            head = f"[{d['title']}] ({d['published_at'] or 'date unknown'}) {d['url']}"
            parts.append(head + "\n" + d["text"][:max_chars_per_doc])
        return "\n\n---\n\n".join(parts)


def fetch_candidates(results: list[dict], as_of: str, max_fetches: int) -> list[str]:
    """URLs ``retrieve`` would send to Fetch for these search results (thin text, not future-dated)."""
    gate = parse_ts(as_of)
    out, seen = [], set()
    for r in results:
        url = r.get("url") or ""
        if not url or url in seen:
            continue
        pub = parse_ts(r.get("published_at"))
        if pub is not None and gate and pub > gate:
            continue
        seen.add(url)
        if len(r.get("text") or "") < MIN_TEXT_CHARS and len(out) < max_fetches:
            out.append(url)
    return out


def retrieve(ep: Epochal, queries: list[str], as_of: str, opts: SearchOptions | None, max_fetches: int) -> Retrieval:
    """Search the web as of ``as_of``; fetch full pages for thin results; drop anything dated later.

    Date checks use the document's own ``published_at`` (search result, then the fetched copy's
    metadata), never the echoed ``as_of`` field, which only repeats the request header.
    """
    gate = parse_ts(as_of)
    out, seen, fetches = Retrieval(), set(), 0
    for q in queries:
        try:
            res = ep.search(q, as_of, opts)
        except BudgetExceeded:
            raise
        except HTTPError as e:
            print(f"  [epochal] search failed (not billed): {e}", file=sys.stderr)
            out.search_failures += 1
            continue
        for r in res.get("results", []):
            url = r.get("url") or ""
            if url in seen:
                continue
            pub = parse_ts(r.get("published_at"))
            if pub is None:
                out.undated += 1
            elif gate and pub > gate:
                out.dropped_future += 1  # belt and braces: Epochal should never return these
                continue
            seen.add(url)
            text = r.get("text") or ""
            published = r.get("published_at")
            if len(text) < MIN_TEXT_CHARS and fetches < max_fetches and url:
                fetches += 1
                try:
                    f = ep.fetch(url, as_of)
                except BudgetExceeded:
                    raise
                except HTTPError as e:  # keep going with the snippet
                    print(f"  [epochal] fetch failed (not billed): {e}", file=sys.stderr)
                    f = {"status": "error"}
                if f.get("status") == "success":  # error -> no archived copy by as_of
                    fpub = parse_ts(f.get("published_at"))
                    if fpub and gate and fpub > gate:
                        out.dropped_future += 1  # the copy's own metadata says it is from later
                        if pub is None:
                            continue  # nothing trustworthy left for this URL
                    else:
                        text = f.get("content") or text
                        published = published or f.get("published_at")
                else:
                    out.fetch_failures += 1
            text = text or r.get("snippet") or ""
            if text:
                out.docs.append({"title": r.get("title", ""), "url": url, "published_at": published, "text": text})
    return out


def format_options(choices: list[str]) -> str:
    return "\n".join(f"{LETTERS[i]}. {c}" for i, c in enumerate(choices))


@dataclass
class Forecast:
    answer: str | None  # choice text, None = abstained
    probabilities: dict[str, float]  # choice text -> probability (sums to 1)
    citations: list[str]
    rationale: str
    raw: str
    retried: bool = False
    parse_mode: str = "json"


def _extract_json(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
                    break
        start = text.find("{", start + 1)
    return None


def parse_letter(text: str, choices: list[str]) -> str | None:
    """Letter from 'ANSWER: X', a leading 'X.'/'X)', or the only option text named."""
    text = text or ""
    valid = LETTERS[: len(choices)]
    m = re.search(r"ANSWER\"?\s*[:\-]?\s*\"?\**\(?([A-H])\b", text, re.IGNORECASE)
    if m and m.group(1).upper() in valid:
        return m.group(1).upper()
    m = re.match(r"\s*\**\(?([A-H])(?:[).:]|\s*$)", text)
    if m and m.group(1) in valid:
        return m.group(1)
    named = [i for i, c in enumerate(choices) if re.search(rf"(?<!\w){re.escape(c)}(?!\w)", text, re.I)]
    if len(named) == 1:
        return LETTERS[named[0]]
    return None


def normalise_probs(raw: object, choices: list[str], answer_letter: str | None) -> dict[str, float]:
    """Map {letter|choice: p} to {choice: p}; fall back to one-hot on the answer."""
    probs = dict.fromkeys(choices, 0.0)
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).strip().strip(".").upper()
            try:
                p = float(v)
            except (TypeError, ValueError):
                continue
            if p > 1 and p <= 100:  # percentages
                p /= 100
            if len(key) == 1 and key in LETTERS[: len(choices)]:
                probs[choices[LETTERS.index(key)]] += max(0.0, p)
            else:
                for c in choices:
                    if c.upper() == key:
                        probs[c] += max(0.0, p)
    total = sum(probs.values())
    if total <= 0:
        if answer_letter is None:
            return dict.fromkeys(choices, 1 / len(choices))
        return {c: float(i == LETTERS.index(answer_letter)) for i, c in enumerate(choices)}
    return {c: p / total for c, p in probs.items()}


def parse_forecast(text: str, choices: list[str]) -> Forecast:
    obj = _extract_json(text)
    letter, mode = None, "json"
    if obj is not None:
        a = str(obj.get("answer", "")).strip().strip(".").upper()
        letter = a if a in LETTERS[: len(choices)] else parse_letter(str(obj.get("answer", "")), choices)
    if letter is None:
        letter, mode = parse_letter(text, choices), "text"
    probs = normalise_probs((obj or {}).get("probabilities"), choices, letter)
    if letter is None and obj and any(v > 0 for v in probs.values()) and len(set(probs.values())) > 1:
        letter, mode = LETTERS[choices.index(max(probs, key=probs.get))], "probs"
    cites = (obj or {}).get("citations") or []
    if not isinstance(cites, list):
        cites = [cites]
    return Forecast(
        answer=choices[LETTERS.index(letter)] if letter else None,
        probabilities=probs if letter else dict.fromkeys(choices, 1 / len(choices)),
        citations=[str(c) for c in cites if c][:10],
        rationale=str((obj or {}).get("rationale", ""))[:500],
        raw=text,
        parse_mode=mode if letter else "abstain",
    )


def force_choice_prompt(prompt: str, reply: str, choices: list[str]) -> str:
    letters = "/".join(LETTERS[: len(choices)])
    return (
        f"{prompt}\n\nYour previous reply was:\n{(reply or '').strip()[:600] or '(empty)'}\n\n"
        f"That did not pick an option. You must choose even if uncertain. Reply with only the JSON "
        f'object, for example {{"answer": "A", "probabilities": {{...}}, "citations": [], "rationale": ""}}, '
        f"using one letter from {letters}."
    )
