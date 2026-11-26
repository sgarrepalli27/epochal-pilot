"""Replace calendar years and explicit dates with relative labels (Year 0, Year -1, ...).

Used by ``--mask-dates`` so a model is less able to recall a dated outcome from the year number.
Relative order is preserved: an earlier year always maps to a more negative label.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_YEAR = re.compile(r"(?<![\d])((?:19|20)\d{2})(?!\d)")
_ISO = re.compile(r"(?<![\d])((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)")
_YMD = re.compile(r"(?<![\d])((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")
_MDY = re.compile(r"(?<![\d])(\d{2})(\d{2})((?:19|20)\d{2})(?!\d)")
_MONTH = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
# 15 February 2022 | February 15, 2022 | February 15 2022 | 15-16 December 2021
_LONG = re.compile(
    rf"(?P<a>(?:(?:\d{{1,2}}(?:\s*[-/\u2013]\s*\d{{1,2}})?)\s+)?(?:{_MONTH})\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?)"
    r",?\s+(?P<y>(?:19|20)\d{2})(?!\d)",
    re.I,
)


def year_label(year: int, as_of_year: int) -> str:
    delta = year - as_of_year
    return "Year 0" if delta == 0 else f"Year {delta:+d}"


def _valid_md(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def mask_text(text: str, as_of: str | date) -> str:
    """Rewrite dates and years in ``text`` relative to the as-of year. Idempotent enough for one pass."""
    if not text:
        return text
    pivot = date.fromisoformat(as_of[:10] if isinstance(as_of, str) else as_of.isoformat()).year

    def lab(year: int) -> str:
        return year_label(year, pivot)

    def iso(m: re.Match) -> str:
        return f"{lab(int(m.group(1)))}-{m.group(2)}-{m.group(3)}"

    def ymd(m: re.Match) -> str:
        month, day = int(m.group(2)), int(m.group(3))
        if not _valid_md(month, day):
            return m.group(0)
        return f"{lab(int(m.group(1)))}-{m.group(2)}-{m.group(3)}"

    def mdy(m: re.Match) -> str:
        month, day = int(m.group(1)), int(m.group(2))
        if not _valid_md(month, day):
            return m.group(0)
        return f"{lab(int(m.group(3)))}-{m.group(1)}-{m.group(2)}"

    def long(m: re.Match) -> str:
        return f"{m.group('a')} {lab(int(m.group('y')))}"

    def year_only(m: re.Match) -> str:
        return lab(int(m.group(1)))

    out = _ISO.sub(iso, text)
    out = _YMD.sub(ymd, out)
    out = _MDY.sub(mdy, out)
    out = _LONG.sub(long, out)
    return _YEAR.sub(year_only, out)


def mask_value(value: Any, as_of: str | date) -> Any:
    """Walk a JSON-like structure and mask every string."""
    if isinstance(value, str):
        return mask_text(value, as_of)
    if isinstance(value, list):
        return [mask_value(v, as_of) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v, as_of) for k, v in value.items()}
    return value
