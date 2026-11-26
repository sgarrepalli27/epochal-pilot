"""Derived prompt facts: SEP year-end median vs the current range, and remaining FOMC meetings.

Computed only from documents and calendars available strictly before the as-of date T, so nothing
after T can leak into the prompt. The SEP median is a calendar-year-end projection, not a
quarter-end forecast; the line states the gap in bp and the implied number of 25 bp moves.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from epochal_pilot.public_context import PublicContext, PublicDoc

_YEAR = re.compile(r"^(20\d{2})$")
_RATE = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)$")
_FUNDS = re.compile(r"federal funds rate", re.I)


def parse_range_midpoint(range_at_asof: str) -> float:
    """Midpoint of a ``0.00-0.25%`` or ``3.75-4.00%`` target range."""
    text = (range_at_asof or "").replace("%", "").replace("\u2013", "-").replace("\u2014", "-").strip()
    lo, hi = (p.strip() for p in text.split("-", 1))
    return (float(lo) + float(hi)) / 2


def fmt_rate(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") + "%"


def fmt_bp(value: float) -> str:
    body = f"{value:.1f}".rstrip("0").rstrip(".")
    if not body.startswith("-"):
        body = "+" + body
    return f"{body} bp"


def implied_25bp_moves(gap_bp: float) -> int:
    """Nearest number of 25 bp steps (half-up); signed, 0 when the gap is under 12.5 bp."""
    n = int((abs(gap_bp) + 12.5) // 25)
    if n == 0 or gap_bp == 0:
        return 0
    return n if gap_bp > 0 else -n


def moves_phrase(gap_bp: float) -> str:
    n = implied_25bp_moves(gap_bp)
    if n == 0:
        return "0 x 25 bp moves"
    kind = "hike" if n > 0 else "cut"
    noun = kind if abs(n) == 1 else f"{kind}s"
    return f"{abs(n)} x 25 bp {noun}"


def meetings_phrase(n: int | None) -> str:
    if n is None:
        return "meeting calendar not available"
    return f"{n} FOMC meeting{'s' if n != 1 else ''} before target"


def load_meeting_dates(path: Path) -> list[date]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out: list[date] = []
    for row in rows:
        raw = (row.get("end_date") or "").strip()
        if not raw:
            continue
        out.append(date.fromisoformat(raw[:10]))
    return out


def count_meetings(meetings: list[date], as_of: date, target: date) -> int:
    """Scheduled meetings strictly after T and on or before the target date."""
    return sum(1 for d in meetings if as_of < d <= target)


def parse_sep_median_funds_rates(text: str) -> dict[int, float]:
    """Year -> median year-end federal funds rate from an SEP Table 1 markdown extract.

    The median block is the first run of year columns, including a trailing longer-run
    column which is discarded. Formats vary: some files put years on the 'Variable' row,
    some on the next row; some have three projection years, some four.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    funds_cells: list[str] | None = None
    year_cells: list[str] | None = None
    for i, ln in enumerate(lines):
        if _FUNDS.search(ln) and "|" in ln:
            funds_cells = _split_cells(ln)
            for prev in reversed(lines[:i]):
                if re.search(r"\b20\d{2}\b", prev) and "|" in prev:
                    year_cells = _split_cells(prev)
                    break
            break
    if not funds_cells or not year_cells:
        return {}

    years = _median_year_columns(year_cells)
    values = _numeric_cells(funds_cells[1:])  # drop the row label
    out: dict[int, float] = {}
    for year, value in zip(years, values, strict=False):
        if year is not None and value is not None:
            out[year] = value
    return out


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def _median_year_columns(cells: list[str]) -> list[int | None]:
    """First cycle of year columns, plus a None placeholder for a closing 'Longer run'."""
    years: list[int | None] = []
    seen: set[int] = set()
    for cell in cells:
        m = _YEAR.match(cell)
        if m:
            year = int(m.group(1))
            if year in seen:
                break
            seen.add(year)
            years.append(year)
            continue
        if years and re.search(r"longer", cell, re.I):
            years.append(None)
            break
    return years


def _numeric_cells(cells: list[str]) -> list[float | None]:
    out: list[float | None] = []
    for cell in cells:
        token = cell.replace("\u2013", "-").replace("\u2014", "-").strip()
        out.append(float(token) if _RATE.match(token) else None)
    return out


@dataclass(frozen=True)
class SepGap:
    as_of: date
    target: date
    target_year: int
    midpoint: float | None
    sep_published: date | None
    median: float | None
    gap_bp: float | None
    implied_moves: int | None
    meetings: int | None
    text: str


def latest_sep_before(pack: PublicContext | None, as_of: str | date) -> PublicDoc | None:
    if pack is None:
        return None
    stamp = as_of if isinstance(as_of, str) else as_of.isoformat()
    chosen = [d for d in pack.select(stamp) if d.kind == "fomc_sep"]
    return chosen[-1] if chosen else None


def build_sep_gap(
    *,
    as_of: str | date,
    target: str | date,
    range_at_asof: str = "",
    pack: PublicContext | None = None,
    meetings: list[date] | Path | None = None,
) -> SepGap:
    """Build the derived line for one event. Never reads a document published on or after T."""
    t = date.fromisoformat(as_of[:10] if isinstance(as_of, str) else as_of.isoformat())
    end = date.fromisoformat(target[:10] if isinstance(target, str) else target.isoformat())
    year = end.year

    midpoint = None
    if range_at_asof.strip():
        try:
            midpoint = parse_range_midpoint(range_at_asof)
        except (ValueError, TypeError):
            midpoint = None

    meeting_count = None
    if meetings is not None:
        dates = load_meeting_dates(meetings) if isinstance(meetings, Path) else list(meetings)
        meeting_count = count_meetings(dates, t, end)

    sep = latest_sep_before(pack, t)
    median = None
    published = None
    if sep is not None:
        published = sep.published
        if published >= t:  # select() already guards this; keep it for future edits
            sep = None
            published = None
        else:
            median = parse_sep_median_funds_rates(sep.text()).get(year)

    gap_bp = None
    moves = None
    if median is not None and midpoint is not None:
        gap_bp = round((median - midpoint) * 100, 1)
        moves = implied_25bp_moves(gap_bp)

    tail = meetings_phrase(meeting_count)
    if median is not None and midpoint is not None and gap_bp is not None:
        line = (
            f"Derived: SEP median end-{year} {fmt_rate(median)} vs current midpoint {fmt_rate(midpoint)} "
            f"= {fmt_bp(gap_bp)}, about {moves_phrase(gap_bp)} over the full year; {tail}"
        )
    else:
        line = f"Derived: no SEP median for end-{year} was available before today; {tail}"

    return SepGap(
        as_of=t,
        target=end,
        target_year=year,
        midpoint=midpoint,
        sep_published=published,
        median=median,
        gap_bp=gap_bp,
        implied_moves=moves,
        meetings=meeting_count,
        text=line,
    )
