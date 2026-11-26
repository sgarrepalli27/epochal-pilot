"""Deterministic business-rule analysis for fed-quarterly (step 3 of the jev-rules pipeline).

Numbers and wording are taken only from documents released strictly before the as-of date
(the shipped first-release pack, plus the SEP-gap line). The mapping from those signals to a
forecast bucket was written with knowledge of 2016-2026 history: it is a documented desk
checklist, not a discovery, and it can be wrong on shocks that were not yet in the latest
statement (for example mid-February 2020).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from epochal_pilot.agent import Forecast, normalise_probs
from epochal_pilot.derived import SepGap, build_sep_gap, parse_range_midpoint
from epochal_pilot.public_context import PublicContext, PublicDoc
from epochal_pilot.scenario import LETTERS, Event

# The text placed in Jev's state so the model judges against the same checklist the code used.
RULES_TEXT = """\
BUSINESS RULES (a desk checklist computed from first-release data dated before the as-of date.
These rules were written with knowledge of 2016-2026 history; treat them as documented guidance,
not as a discovery. An SEP year-end median is a calendar-year projection, not the quarter-end target.)

1. Inflation gap: take core PCE year-on-year if available, otherwise core CPI, minus the 2% goal.
   A gap of about +2 percentage points or more is a hike vote; a gap near or below zero is a hold or a cut vote.
2. Labour: unemployment at or above 5.5%, a rise in unemployment, or a large payroll decline
   supports a cut.
3. Markets: a 2-year Treasury yield 25 bp or more above the policy-range midpoint supports a hike;
   25 bp or more below supports a cut.
4. Fed signals: hawkish statement language (for example "soon be appropriate to raise",
   "further firming", a dissent preferring a hike) supports a hike; dovish language
   ("patient", easing, a dissent preferring a cut) supports a cut.
5. SEP gap: the year-end median versus the current midpoint is a vote only when the target is
   itself a year-end (December) and the gap is at least 12.5 bp. Do not treat the full-year gap
   as the quarter-end move.
6. Meetings: with no scheduled FOMC meeting before the target, only a shock can change the range;
   default to no change.
7. Shocks: pandemic, bank-stress, or financial-crisis language in the latest statement or minutes
   supports a large cut.
"""

HAWKISH_PHRASES = (
    "soon be appropriate to raise",
    "further firming",
    "additional firming",
    "further policy firming",
    "removing policy accommodation",
)
DOVISH_PHRASES = (
    "soon be appropriate to lower",
    "preferred to lower",
    "preferring to lower",
    "preferred to cut",
    "reduce the target range",
    "lower the target range",
    "additional accommodation",
    "the committee is patient",
    "will be patient",
)
# Ongoing "pandemic" / "COVID-19" mentions in 2021-22 statements are backdrop, not a live shock.
# Mid-February 2020 also has no emergency language in the January statement — the rules miss that turn.
SHOCK_PHRASES = (
    "bank failure",
    "bank failures",
    "silicon valley bank",
    "financial crisis",
    "systemic risk",
    "severe strains",
    "emergency lending",
    "unscheduled meeting",
    "emergency meeting",
)
DISSENT_HIKE_PHRASES = ("preferred to raise", "preferring to raise")
DISSENT_CUT_PHRASES = ("preferred to lower", "preferring to lower", "preferred to cut")

BUCKET_BP = {
    "Cut 100+ bp": -100,
    "Cut 75 bp": -75,
    "Cut 50 bp": -50,
    "Cut 25 bp": -25,
    "No change": 0,
    "Hike 25 bp": 25,
    "Hike 50 bp": 50,
    "Hike 75+ bp": 75,
}


@dataclass
class RuleSignals:
    """Parsed first-release numbers and the checklist conclusion for one as-of date."""

    as_of: date
    target: date
    range_at_asof: str = ""
    midpoint: float | None = None
    cpi_yoy: float | None = None
    core_cpi_yoy: float | None = None
    pce_yoy: float | None = None
    core_pce_yoy: float | None = None
    unemployment: float | None = None
    unemployment_trend: str | None = None  # up, down, unchanged
    payrolls: int | None = None  # thousands
    two_year: float | None = None
    yield_gap_bp: float | None = None
    sep_median: float | None = None
    sep_gap_bp: float | None = None
    sep_implied_moves: int | None = None
    meetings: int | None = None
    statement_tone: str = "unclear"  # hawkish, dovish, mixed, unclear
    matched_phrases: list[str] = field(default_factory=list)
    shock_present: bool = False
    shock_notes: str = ""
    inflation_gap_pp: float | None = None
    expected_bp: float = 0.0
    bucket: str = "No change"
    notes: list[str] = field(default_factory=list)

    def core_inflation(self) -> float | None:
        if self.core_pce_yoy is not None:
            return self.core_pce_yoy
        if self.core_cpi_yoy is not None:
            return self.core_cpi_yoy
        return self.cpi_yoy

    def as_state(self) -> dict:
        """Structured analysis for Jev's state (JSON-serialisable, no secrets)."""
        return {
            "inflation": {
                "cpi_yoy_pct": self.cpi_yoy,
                "core_cpi_yoy_pct": self.core_cpi_yoy,
                "pce_yoy_pct": self.pce_yoy,
                "core_pce_yoy_pct": self.core_pce_yoy,
                "gap_vs_2pct_goal_pp": self.inflation_gap_pp,
                "measure_used": (
                    "core_pce"
                    if self.core_pce_yoy is not None
                    else "core_cpi"
                    if self.core_cpi_yoy is not None
                    else "cpi"
                    if self.cpi_yoy is not None
                    else None
                ),
            },
            "labour": {
                "unemployment_pct": self.unemployment,
                "unemployment_trend": self.unemployment_trend,
                "payrolls_thousands": self.payrolls,
            },
            "markets": {"two_year_yield_pct": self.two_year, "two_year_minus_midpoint_bp": self.yield_gap_bp},
            "sep": {
                "year_end_median_pct": self.sep_median,
                "gap_vs_midpoint_bp": self.sep_gap_bp,
                "implied_25bp_moves_over_full_year": self.sep_implied_moves,
            },
            "fed_signals": {
                "statement_tone": self.statement_tone,
                "matched_phrases": self.matched_phrases,
                "scheduled_meetings_before_target": self.meetings,
            },
            "shock_present": self.shock_present,
            "shock_notes": self.shock_notes or None,
            "rules_point_estimate": {"expected_bp": self.expected_bp, "nearest_bucket": self.bucket},
            "notes": self.notes,
        }


def _pct_after(patterns: list[str], text: str) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            return float(m.group(1))
    return None


def parse_cpi(text: str) -> tuple[float | None, float | None]:
    yoy = _pct_after(
        [
            r"all items index rose ([\d.]+)\s*percent for the 12 months",
            r"all items index increased ([\d.]+)\s*percent "
            r"(?:before seasonal adjustment|from one year|over the last 12 months)",
            r"increased ([\d.]+)\s*percent over the last 12 months",
            r"12-month.{0,80}increased ([\d.]+)\s*percent",
        ],
        text,
    )
    core = _pct_after(
        [
            r"all items less food and energy index rose ([\d.]+)\s*percent",
            r"all items less food and energy index (?:increased|rose) ([\d.]+)\s*percent",
        ],
        text,
    )
    return yoy, core


def parse_pce(text: str) -> tuple[float | None, float | None]:
    yoy = _pct_after(
        [
            r"From the same month one year ago,\s*the PCE price index(?: for \w+)? increased ([\d.]+)\s*percent",
            r"The PCE price index for \w+ increased ([\d.]+)\s*percent from one year ago",
            r"(?<![Ee]xcluding food and energy, )(?<![Ee]xcluding food and energy,\s)"
            r"the PCE price index(?: for \w+)? increased ([\d.]+)\s*percent "
            r"from (?:one year ago|the same month one year ago)",
        ],
        text,
    )
    core = _pct_after(
        [
            r"Excluding food and energy,\s*the PCE price index(?: for \w+)? increased "
            r"([\d.]+)\s*percent from (?:one year ago|the same month one year ago)",
            r"Excluding food and energy,\s*the PCE price index increased ([\d.]+)\s*percent from one year ago",
        ],
        text,
    )
    return yoy, core


def parse_jobs(text: str) -> tuple[float | None, str | None, int | None]:
    u = _pct_after(
        [
            r"unemployment rate(?: was little changed)?(?: at|, at) ([\d.]+)\s*percent",
            r"unemployment rate changed little at ([\d.]+)\s*percent",
            r"unemployment rate.{0,40}?at ([\d.]+)\s*percent",
        ],
        text,
    )
    trend = None
    head = text[:800].lower()
    if re.search(r"unemployment rate.{0,60}(edged up|rose|increased)", head):
        trend = "up"
    elif re.search(r"unemployment rate.{0,60}(edged down|declined|fell|decreased)", head):
        trend = "down"
    elif re.search(r"unemployment rate.{0,60}(little changed|changed little|unchanged|held)", head):
        trend = "unchanged"
    payrolls = None
    m = re.search(
        r"payroll employment (rose|increased|fell|decreased|declined) by\s+([\d,]+)",
        text,
        re.I,
    )
    if m:
        n = int(m.group(2).replace(",", ""))
        if n >= 1000:  # BLS writes "467,000"; store thousands
            n //= 1000
        payrolls = n if m.group(1).lower() in {"rose", "increased"} else -n
    return u, trend, payrolls


def parse_two_year(text: str) -> float | None:
    m = re.search(r"\(latest\)\s*\|\s*[\d.]+\s*\|\s*([\d.]+)\s*\|", text)
    return float(m.group(1)) if m else None


def _phrases_in(text: str, phrases: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [p for p in phrases if p in low]


def parse_statement_tone(texts: list[str]) -> tuple[str, list[str], bool, str]:
    blob = "\n".join(texts)
    hawk = _phrases_in(blob, HAWKISH_PHRASES + DISSENT_HIKE_PHRASES)
    dove = _phrases_in(blob, DOVISH_PHRASES + DISSENT_CUT_PHRASES)
    shock = _phrases_in(blob, SHOCK_PHRASES)
    if hawk and dove:
        tone = "mixed"
    elif hawk:
        tone = "hawkish"
    elif dove:
        tone = "dovish"
    else:
        tone = "unclear"
    notes = ""
    if shock:
        notes = "matched: " + ", ".join(shock)
    return tone, hawk + dove, bool(shock), notes


def _docs_by_kind(docs: list[PublicDoc]) -> dict[str, PublicDoc]:
    return {d.kind: d for d in docs}


def expected_bp(sig: RuleSignals) -> tuple[float, list[str]]:
    """Point estimate in bp, plus the notes that explain it. See RULES_TEXT.

    Signals vote on direction; size comes from the inflation gap (or a shock), not from
    adding every bonus. That avoids treating a first 25 bp lift-off as a 75 bp move.
    """
    notes: list[str] = []
    votes = 0
    core = sig.core_inflation()
    gap = None
    if core is not None:
        gap = round(core - 2.0, 2)
        sig.inflation_gap_pp = gap
        if gap >= 2:
            votes += 1
            notes.append(f"core inflation {core}% is above 2% (gap +{gap} pp): hike vote")
        elif gap <= -0.5:
            votes -= 1
            notes.append(f"core inflation {core}% is below the 2% goal (gap {gap} pp): cut vote")
        else:
            notes.append(f"core inflation {core}% is near the 2% goal (gap {gap} pp)")
    else:
        notes.append("no year-on-year core inflation figure in the latest releases")

    if sig.unemployment is not None:
        if sig.unemployment >= 5.5 or (sig.unemployment_trend == "up" and sig.unemployment >= 4.5):
            votes -= 1
            notes.append(f"unemployment {sig.unemployment}% ({sig.unemployment_trend or 'trend unclear'}): cut vote")
        else:
            notes.append(f"unemployment {sig.unemployment}% ({sig.unemployment_trend or 'trend unclear'})")
    if sig.payrolls is not None and sig.payrolls <= -100:
        votes -= 1
        notes.append(f"payrolls {sig.payrolls:,} thousand: cut vote")

    if sig.yield_gap_bp is not None:
        if sig.yield_gap_bp >= 25:
            votes += 1
            notes.append(f"2-year yield is {sig.yield_gap_bp:+.1f} bp vs the policy midpoint: hike vote")
        elif sig.yield_gap_bp <= -25:
            votes -= 1
            notes.append(f"2-year yield is {sig.yield_gap_bp:+.1f} bp vs the policy midpoint: cut vote")
        else:
            notes.append(f"2-year yield is close to the policy midpoint ({sig.yield_gap_bp:+.1f} bp)")

    dissent_set = set(DISSENT_HIKE_PHRASES) | set(DISSENT_CUT_PHRASES)
    dissent_only = bool(set(sig.matched_phrases) & dissent_set) and not (set(sig.matched_phrases) - dissent_set)
    if sig.statement_tone == "hawkish" and not dissent_only:
        votes += 1
        notes.append("statement tone is hawkish: hike vote")
    elif sig.statement_tone == "hawkish" and dissent_only:
        notes.append("hawkish dissents only; committee did not signal a hike (no vote)")
    elif sig.statement_tone == "dovish" and not dissent_only:
        votes -= 1
        notes.append("statement tone is dovish: cut vote")
    elif sig.statement_tone == "dovish" and dissent_only:
        notes.append("dovish dissents only; committee did not signal a cut (no vote)")
    elif sig.statement_tone == "mixed":
        notes.append("statement tone is mixed (hawkish and dovish phrases)")
    else:
        notes.append("statement tone is unclear")

    if sig.sep_gap_bp is not None and sig.meetings:
        if sig.target.month == 12 and abs(sig.sep_gap_bp) >= 12.5:
            if sig.sep_gap_bp > 0:
                votes += 1
                notes.append(f"SEP year-end gap {sig.sep_gap_bp:+.1f} bp at a December target: hike vote")
            else:
                votes -= 1
                notes.append(f"SEP year-end gap {sig.sep_gap_bp:+.1f} bp at a December target: cut vote")
        else:
            notes.append(f"SEP gap {sig.sep_gap_bp:+.1f} bp is a year-end prior, not counted as a quarter vote")

    if sig.shock_present:
        votes -= 2
        notes.append(f"shock language present ({sig.shock_notes}): two cut votes")

    if sig.meetings == 0 and not sig.shock_present:
        notes.append("no scheduled meeting before the target and no shock: force no change")
        return 0.0, notes

    def size(magnitude_votes: int, inflation_gap: float | None, core_level: float | None) -> float:
        if sig.shock_present and magnitude_votes < 0:
            return -100.0 if (sig.midpoint or 0) >= 1 else -75.0
        if abs(magnitude_votes) < 2:
            return 0.0
        step = 25
        if (inflation_gap is not None and abs(inflation_gap) >= 4) or (core_level is not None and core_level >= 6):
            step = 50
        return step if magnitude_votes > 0 else -step

    bp = size(votes, gap, core)
    notes.append(f"net votes {votes:+d} -> {bp:+.0f} bp")
    return bp, notes


def nearest_bucket(bp: float, choices: list[str] | None = None) -> str:
    options = choices or list(BUCKET_BP)
    return min(options, key=lambda c: abs(BUCKET_BP.get(c, 0) - bp))


def distribution_from_bp(bp: float, choices: list[str]) -> dict[str, float]:
    """Softmax-like bump around the point estimate (25 bp scale). Always sums to 1."""
    weights = []
    for c in choices:
        center = BUCKET_BP.get(c)
        if center is None:
            weights.append(0.0)
            continue
        weights.append(math.exp(-(((bp - center) / 25.0) ** 2) / 2))
    total = sum(weights) or 1.0
    return {c: w / total for c, w in zip(choices, weights, strict=True)}


def analyse_event(
    *,
    ev: Event,
    as_of: str,
    pack: PublicContext | None,
    meetings_path: Path | None = None,
    docs: list[PublicDoc] | None = None,
    choices: list[str] | None = None,
) -> RuleSignals:
    """Step 3: parse first-release data from before T and apply the checklist."""
    t = date.fromisoformat(as_of[:10])
    sig = RuleSignals(as_of=t, target=ev.event_date, range_at_asof=(ev.extra.get("range_at_asof") or "").strip())
    if sig.range_at_asof:
        try:
            sig.midpoint = parse_range_midpoint(sig.range_at_asof)
        except (ValueError, TypeError):
            sig.midpoint = None

    chosen = docs if docs is not None else (pack.select(as_of) if pack is not None else [])
    by = _docs_by_kind(chosen)
    if "bls_cpi" in by:
        sig.cpi_yoy, sig.core_cpi_yoy = parse_cpi(by["bls_cpi"].text())
    if "bea_pce" in by:
        sig.pce_yoy, sig.core_pce_yoy = parse_pce(by["bea_pce"].text())
    if "bls_jobs" in by:
        sig.unemployment, sig.unemployment_trend, sig.payrolls = parse_jobs(by["bls_jobs"].text())
    if "treasury_yields" in by:
        sig.two_year = parse_two_year(by["treasury_yields"].text())
    if sig.two_year is not None and sig.midpoint is not None:
        sig.yield_gap_bp = round((sig.two_year - sig.midpoint) * 100, 1)

    tone_sources = [by[k].text() for k in ("fomc_statement", "fomc_minutes") if k in by]
    if tone_sources:
        sig.statement_tone, sig.matched_phrases, sig.shock_present, sig.shock_notes = parse_statement_tone(tone_sources)

    if pack is not None or meetings_path:
        gap: SepGap = build_sep_gap(
            as_of=as_of,
            target=ev.event_date,
            range_at_asof=sig.range_at_asof,
            pack=pack,
            meetings=meetings_path if meetings_path and meetings_path.is_file() else None,
        )
        sig.sep_median = gap.median
        sig.sep_gap_bp = gap.gap_bp
        sig.sep_implied_moves = gap.implied_moves
        sig.meetings = gap.meetings

    sig.expected_bp, sig.notes = expected_bp(sig)
    options = choices or ev.choices or list(BUCKET_BP)
    sig.bucket = nearest_bucket(sig.expected_bp, options)
    return sig


def rules_forecast(sig: RuleSignals, choices: list[str]) -> Forecast:
    """Map the checklist point estimate to a full distribution (the no-model baseline)."""
    probs = distribution_from_bp(sig.expected_bp, choices)
    answer = max(probs, key=probs.get) if choices else None
    letter = LETTERS[choices.index(answer)] if answer in choices else None
    probs = normalise_probs(probs, choices, letter)
    rationale = (
        f"rules baseline (hindsight checklist): expected {sig.expected_bp:+.1f} bp -> {sig.bucket}. "
        + "; ".join(sig.notes[:6])
    )
    return Forecast(
        answer=answer,
        probabilities=probs,
        citations=[],
        rationale=rationale[:500],
        raw=rationale,
        parse_mode="rules",
    )
