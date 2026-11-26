"""Build the fed-quarterly scenario: answer key, as-of calendar and public first-release context pack.

Maintainer tool. It uses the public web (federalreserve.gov, bls.gov, bea.gov, home.treasury.gov and,
optionally, the ALFRED vintage API) and never Epochal.

    python tools/build_fed_quarterly.py meetings   # scheduled FOMC meetings -> fomc_meetings.csv (network)
    python tools/build_fed_quarterly.py events     # as-of dates + answer key -> events.csv (offline)
    python tools/build_fed_quarterly.py context    # public context pack -> public_context/ (network)
    python tools/build_fed_quarterly.py all        # all three

``events`` is offline and deterministic: it reads ``fomc_changes.csv`` (every target-range change since
2016, each checked against its federalreserve.gov statement) and ``fomc_meetings.csv``. A quarter whose
end is not yet past ``--today`` (default: today) becomes a LIVE event with an empty outcome. To resolve
it later, add any new change to ``fomc_changes.csv`` and re-run ``events --today <a date after the
quarter end>``; then ``epochal-pilot score <live run folder>``.

The context pack holds, for every as-of date T, only the latest document of each kind released strictly
before T:

* FOMC post-meeting statement            federalreserve.gov/newsevents/pressreleases/monetaryYYYYMMDDa.htm
* FOMC minutes (staff review + policy)   federalreserve.gov/monetarypolicy/fomcminutesYYYYMMDD.htm
* Summary of Economic Projections table  federalreserve.gov/monetarypolicy/fomcprojtablYYYYMMDD.htm
* BLS CPI news release (summary)         bls.gov/news.release/archives/cpi_MMDDYYYY.htm
* BLS Employment Situation (summary)     bls.gov/news.release/archives/empsit_MMDDYYYY.htm
* BEA Personal Income and Outlays (PCE)  bea.gov/news/YYYY/personal-income-and-outlays-<month>-<year>
* Treasury par yield curve snapshot      home.treasury.gov daily yield curve CSV (values dated before T)
* Optional, with FRED_API_KEY set: an ALFRED snapshot of BLS/BEA/Census series *as they were known the
  day before T* (first-release vintages, no later revisions).

All of it is US-government public-domain material (https://www.federalreserve.gov/disclaimer.htm,
https://www.bls.gov/opub/copyright-information.htm, https://www.bea.gov/help/guidelines-for-citing-bea,
https://home.treasury.gov/subfooter/site-policies-and-notices). ISM survey data is proprietary and is
not included; growth data comes from the BEA release and the minutes' staff review.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html
import itertools
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_public_context as base

_raw_get = base.get


def _get(url: str) -> str:
    """Like base.get, but repair pages served as Windows-1252 (BLS) instead of UTF-8."""
    text = _raw_get(url)
    if "\ufffd" in text:
        import gzip
        import urllib.request

        req = urllib.request.Request(url, headers=base.UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        for enc in ("utf-8", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    return text


base.get = _get

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios" / "fed-quarterly"
OUT = SCEN / "public_context"
FED = "https://www.federalreserve.gov"
FIRST_YEAR, LAST_YEAR = 2016, 2026
QUARTERS = [((2, 15), (3, 31)), ((5, 15), (6, 30)), ((8, 15), (9, 30)), ((11, 15), (12, 31))]
CHOICES = [
    "Cut 100+ bp",
    "Cut 75 bp",
    "Cut 50 bp",
    "Cut 25 bp",
    "No change",
    "Hike 25 bp",
    "Hike 50 bp",
    "Hike 75+ bp",
]
# ~10 as-of dates where the rate path turned or moved by more than usual: where Epochal requests carry signal.
TURNING_POINTS = {
    "2016-11-15": "first hike in a year (Dec 2016)",
    "2018-11-15": "last hike of the 2016-18 cycle (Dec 2018)",
    "2019-05-15": "pivot from hikes to 'patient' (hold before the July 2019 cut)",
    "2019-08-15": "mid-cycle cuts under way (Sep 2019)",
    "2020-02-15": "COVID shock: 150 bp of emergency cuts in March 2020",
    "2022-02-15": "lift-off after two years at zero (Mar 2022)",
    "2022-05-15": "first 75 bp hike (Jun 2022)",
    "2024-08-15": "first cut of the cycle, 50 bp (Sep 2024)",
    "2025-08-15": "cuts resume after a nine-month pause (Sep 2025)",
    "2026-08-15": "back to hiking (Sep 2026)",
}
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip


# ------------------------------------------------------------------------------------------ answer key


def parse_range(r: str) -> tuple[float, float]:
    lo, hi = r.replace("%", "").split("-")
    return float(lo), float(hi)


def fmt_range(r: tuple[float, float]) -> str:
    return f"{r[0]:.2f}-{r[1]:.2f}%"


def load_changes(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r["announcement_date"])
    for a, b in itertools.pairwise(rows):
        if a["range_after"] != b["range_before"]:
            raise ValueError(
                f"fomc_changes.csv: chain broken between {a['announcement_date']} and {b['announcement_date']}"
            )
    return rows


def range_on(changes: list[dict], d: date, inclusive: bool) -> tuple[float, float]:
    """Target range in force at the start of day ``d`` (``inclusive``: after any decision announced on d)."""
    cur = parse_range(changes[0]["range_before"])
    for c in changes:
        ad = date.fromisoformat(c["announcement_date"])
        if ad < d or (inclusive and ad == d):
            cur = parse_range(c["range_after"])
    return cur


def bucket(delta_bp: int) -> str:
    if delta_bp <= -100:
        return "Cut 100+ bp"
    if delta_bp >= 75:
        return "Hike 75+ bp"
    if delta_bp == 0:
        return "No change"
    if delta_bp % 25:
        raise ValueError(f"change of {delta_bp} bp is not a multiple of 25")
    return f"{'Cut' if delta_bp < 0 else 'Hike'} {abs(delta_bp)} bp"


def load_meetings(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def long_date(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def build_events(changes: list[dict], meetings: list[dict], today: date) -> list[dict]:
    sched = sorted(date.fromisoformat(m["end_date"]) for m in meetings)
    changes_by_day = {c["announcement_date"]: c for c in changes}
    events = []
    for y in range(FIRST_YEAR, LAST_YEAR + 1):
        for qi, ((am, ad), (qm, qd)) in enumerate(QUARTERS, start=1):
            t, qe = date(y, am, ad), date(y, qm, qd)
            if t >= today:
                continue  # we cannot even stand at T yet
            r0 = range_on(changes, t, inclusive=False)
            in_window = [m for m in sched if t <= m <= qe]
            last_meeting = max((m for m in sched if m < t), default=None)
            # Persistence baseline: repeat the most recent FOMC decision before T (scheduled or not).
            last_change = max((c for c in changes if date.fromisoformat(c["announcement_date"]) < t),
                              key=lambda c: c["announcement_date"], default=None)  # fmt: skip
            prev = "No change"
            if last_meeting and last_change:
                lc = date.fromisoformat(last_change["announcement_date"])
                if lc >= last_meeting:
                    prev = bucket(round((sum(parse_range(last_change["range_after"])) -
                                         sum(parse_range(last_change["range_before"]))) / 2 * 100))  # fmt: skip
            resolved = qe < today
            moves = [c for c in changes if t <= date.fromisoformat(c["announcement_date"]) <= qe]
            row = {
                "id": f"fq_{y}q{qi}",
                "event_date": qe.isoformat(),
                "as_of": t.isoformat(),
                "status": "backtest" if resolved else "live",
                "question": (
                    f"What will the FOMC's target range for the federal funds rate be at the end of the quarter, "
                    f"on {long_date(qe)}, compared with the {fmt_range(r0)} range in force on {long_date(t)}?"
                ),
                "outcome": "",
                "range_at_asof": fmt_range(r0),
                "range_at_quarter_end": "",
                "change_bp": "",
                "direction": "",
                "previous_outcome": prev,
                "meetings_in_window": ", ".join(long_date(m) for m in in_window) or "none scheduled",
                "event_date_long": long_date(qe),
                "tags": "turning-point" if t.isoformat() in TURNING_POINTS else "",
                "search_query": f"Federal Reserve interest rate outlook {t:%B %Y} next FOMC meeting expectations",
                "source_url": (moves[-1]["statement_url"] if moves else f"{FED}/monetarypolicy/openmarket.htm"),
                "notes": TURNING_POINTS.get(t.isoformat(), ""),
            }
            if resolved:
                r1 = range_on(changes, qe, inclusive=True)
                delta = round((sum(r1) - sum(r0)) / 2 * 100)
                row.update(
                    outcome=bucket(delta),
                    range_at_quarter_end=fmt_range(r1),
                    change_bp=str(delta),
                    direction="up" if delta > 0 else "down" if delta < 0 else "hold",
                )
                if moves:
                    unsched = [c["announcement_date"] for c in moves if c["meeting_type"] != "scheduled"]
                    if unsched:
                        row["notes"] = "; ".join(filter(None, [row["notes"], "unscheduled: " + ", ".join(unsched)]))
            else:
                row["notes"] = "; ".join(filter(None, [row["notes"], "LIVE: resolves after " + qe.isoformat()]))
            events.append(row)
    del changes_by_day
    return events


def write_events(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def cmd_events(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    changes = load_changes(SCEN / "fomc_changes.csv")
    meetings = load_meetings(SCEN / "fomc_meetings.csv")
    rows = build_events(changes, meetings, today)
    write_events(rows, SCEN / "events.csv")
    n_live = sum(r["status"] == "live" for r in rows)
    print(f"wrote {len(rows)} events ({len(rows) - n_live} backtest, {n_live} live) to {SCEN / 'events.csv'}")
    return 0


# ------------------------------------------------------------------------------------------ meetings


def cmd_meetings(args) -> int:
    """Scheduled FOMC meetings (end dates), as they were on the calendar in advance."""
    rows: dict[str, dict] = {}
    for y in range(FIRST_YEAR - 1, 2021):
        page = base.get(f"{FED}/monetarypolicy/fomchistorical{y}.htm")
        for ymd in sorted(set(re.findall(r"fomcminutes(\d{8})\.htm", page))):
            rows[ymd] = {"end_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "note": ""}
    # 2020: the 15 March meeting was unscheduled; the scheduled 17-18 March meeting was cancelled.
    rows.pop("20200315", None)
    rows["20200318"] = {"end_date": "2020-03-18", "note": "scheduled; cancelled after the unscheduled 15 March meeting"}
    cal = base.get(f"{FED}/monetarypolicy/fomccalendars.htm")
    parts = re.split(r"(\d{4}) FOMC Meetings</a>", cal)
    for year, body in zip(parts[1::2], parts[2::2], strict=False):
        y = int(year)
        if y < 2021 or y > LAST_YEAR:
            continue
        for m in re.finditer(
            r"fomc-meeting__month[^>]*><strong>([A-Za-z/]+)</strong></div>\s*"
            r"<div class=\"fomc-meeting__date[^>]*>([^<]+)</div>",
            body,
        ):
            month_txt, days = m.group(1), html.unescape(m.group(2)).strip()
            if "unscheduled" in days.lower() or "notation" in days.lower():
                continue
            last_day = int(re.findall(r"\d+", days)[-1])
            mname = month_txt.split("/")[-1]
            mon = datetime.strptime(mname[:3], "%b").month
            d = date(y, mon, last_day)
            rows[d.strftime("%Y%m%d")] = {"end_date": d.isoformat(), "note": "SEP" if "*" in days else ""}
    out = [rows[k] for k in sorted(rows) if FIRST_YEAR - 1 <= int(k[:4]) <= LAST_YEAR]
    with open(SCEN / "fomc_meetings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["end_date", "note"], lineterminator="\n")
        w.writeheader()
        w.writerows(out)
    print(f"wrote {len(out)} scheduled meetings to {SCEN / 'fomc_meetings.csv'}")
    return 0


# ------------------------------------------------------------------------------------------ context


def minutes_release_dates() -> dict[str, str]:
    out: dict[str, str] = {}
    pages = [base.get(f"{FED}/monetarypolicy/fomchistorical{y}.htm") for y in range(FIRST_YEAR - 1, 2021)]
    pages.append(base.get(f"{FED}/monetarypolicy/fomccalendars.htm"))
    for page in pages:
        for m in re.finditer(r"fomcminutes(\d{8})\.htm", page):
            ymd = m.group(1)
            if ymd in out:
                continue
            win = base.text_of(page[max(0, m.start() - 400) : m.end() + 400], " ")
            found = [
                datetime.strptime(x, "%B %d, %Y").date()
                for x in re.findall(r"Released ([A-Z][a-z]+ \d{1,2}, \d{4})", win)
            ]
            meet = datetime.strptime(ymd, "%Y%m%d").date()
            ok = [d for d in found if meet < d <= meet + timedelta(days=35)]
            if ok:
                out[ymd] = min(ok, key=lambda d: abs((d - meet).days - 21)).isoformat()
    return out


def sep_table(ymd: str) -> tuple[str, str, str] | None:
    url = f"{FED}/monetarypolicy/fomcprojtabl{ymd}.htm"
    try:
        page = base.get(url)
    except RuntimeError:
        return sep_pdf(ymd)
    m = re.search(r"<table.*?</table>", page, re.S | re.I)
    if not m:
        return None
    lines = []
    for tr in re.findall(r"<tr.*?</tr>", m.group(0), re.S | re.I):
        cells = [base.text_of(c, " ") for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
        if any(cells):
            lines.append(" | ".join(cells))
    body = (
        "Summary of Economic Projections, Table 1 "
        "(median, central tendency and range of FOMC participants' projections)\n\n"
    )
    body += "\n".join(lines)
    note = re.search(r"Note:(.*?)</p>", page, re.S)
    if note:
        body += "\n\nNote:" + base.text_of(note.group(1), " ")
    title = f"FOMC Summary of Economic Projections (dot plot medians), {datetime.strptime(ymd, '%Y%m%d'):%d %B %Y}"
    return url, title, base.cap(body, 3500)


def sep_pdf(ymd: str) -> tuple[str, str, str] | None:
    """Some SEPs (e.g. March 2022) exist only as PDF: use pdftotext (poppler) when it is installed."""
    import shutil
    import subprocess
    import tempfile
    import urllib.request

    exe = shutil.which("pdftotext")
    url = f"{FED}/monetarypolicy/files/fomcprojtabl{ymd}.pdf"
    if not exe:
        return None
    try:
        req = urllib.request.Request(url, headers={k: v for k, v in base.UA.items() if k != "Accept-Encoding"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception:  # pragma: no cover - network
        return None
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "sep.pdf"
        pdf.write_bytes(data)
        out = subprocess.run([exe, "-layout", "-f", "1", "-l", "2", str(pdf), "-"], capture_output=True, text=True)
    text = out.stdout
    i = text.find("Table 1")
    j = text.find("Note:", i)
    if i < 0:
        return None
    body = text[i : j if j > i else i + 3000]
    body = "\n".join(re.sub(r"\s{2,}", " | ", ln.strip()) for ln in body.splitlines() if ln.strip())
    title = f"FOMC Summary of Economic Projections (dot plot medians), {datetime.strptime(ymd, '%Y%m%d'):%d %B %Y}"
    return url, title, base.cap("Summary of Economic Projections (from the PDF)\n\n" + body, 3500)


def minutes_doc(ymd: str) -> tuple[str, str, str]:
    url, title, policy = base.minutes(ymd)
    page = base.get(url)
    text = base.text_of(page)
    staff = ""
    i = text.find("Staff Review of the Economic Situation")
    if i >= 0:
        paras = [p.strip() for p in text[i:].split("\n") if len(p.strip()) > 200][:3]
        staff = "Staff review of the economic situation (excerpt):\n\n" + "\n\n".join(paras)
    body = "Policy discussion (excerpt):\n\n" + base.cap(policy, 3000)
    if staff:
        body += "\n\n" + base.cap(staff, 1400)
    return url, title.replace("(policy discussion excerpt)", "(staff review + policy discussion excerpts)"), body


def bea_pce(year: int, month: int) -> tuple[str, str, str, str] | None:
    ry = year + 1 if month == 12 else year
    url = f"https://www.bea.gov/news/{ry}/personal-income-and-outlays-{MONTHS[month - 1]}-{year}"
    try:
        page = base.get(url)
    except RuntimeError:
        return None
    m = re.search(r"EMBARGOED UNTIL RELEASE AT[^,]*,\s*\w+,\s*([A-Z][a-z]+ \d{1,2}, \d{4})", page, re.I)
    if not m:
        return None
    pub = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    b = re.search(r'class="[^"]*field--name-body[^"]*"[^>]*>(.*)', page, re.S)
    text = base.text_of(b.group(1) if b else page)
    for stop in ("Next release:", "Next Release:", "The full text"):
        k = text.find(stop, 300)
        if k > 0:
            text = text[:k]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    prices = [p for p in paras if re.search(r"price index", p, re.I) and len(p) < 1200]
    keep = list(dict.fromkeys(paras[:2] + prices[:4]))
    text = "\n\n".join(keep)
    title = f"BEA Personal Income and Outlays, {MONTHS[month - 1].title()} {year} (PCE and core PCE prices)"
    return url, title, base.cap(text, 3000), pub


def bls_doc(kind: str, mdy: str) -> tuple[str, str, str, str]:
    url, title, body = base.bls_release(kind, mdy)
    pub = f"{mdy[4:]}-{mdy[:2]}-{mdy[2:4]}"
    return url, title, body, pub


def treasury_rows(years: range) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    for y in years:
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
            f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv"
        )
        for r in csv.DictReader(base.get(url).splitlines()):
            d = datetime.strptime(r["Date"], "%m/%d/%Y").date()
            vals = {}
            for k in ("3 Mo", "2 Yr", "10 Yr"):
                with contextlib.suppress(KeyError, ValueError):
                    vals[k] = float(r[k])
            if len(vals) == 3:
                out[d] = vals
    return out


def yields_doc(rows: dict[date, dict[str, float]], t: date) -> tuple[str, str, str, str]:
    days = sorted(d for d in rows if d < t)
    d0 = days[-1]

    def at(back: int) -> tuple[date, dict[str, float]]:
        target = d0 - timedelta(days=back)
        d = max(x for x in days if x <= target)
        return d, rows[d]

    lines = [
        f"U.S. Treasury daily par yield curve rates (percent), last business day before {long_date(t)}.",
        "",
        "date | 3-month | 2-year | 10-year | 10y-2y spread | 10y-3m spread",
    ]
    for label, back in (("latest", 0), ("1 month earlier", 30), ("3 months earlier", 91), ("12 months earlier", 365)):
        d, v = at(back)
        lines.append(
            f"{d.isoformat()} ({label}) | {v['3 Mo']:.2f} | {v['2 Yr']:.2f} | {v['10 Yr']:.2f} | "
            f"{v['10 Yr'] - v['2 Yr']:+.2f} | {v['10 Yr'] - v['3 Mo']:+.2f}"
        )
    lines += [
        "",
        "Reading it: a 2-year yield well above (below) the policy range means markets expect hikes (cuts) over the "
        "next two years; a negative 10y-3m spread (inverted curve) has often preceded slowdowns.",
    ]
    url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"
    return url, f"Treasury yield curve snapshot as of {d0.isoformat()}", "\n".join(lines), d0.isoformat()


ALFRED_SERIES = {
    "PCEPILFE": ("Core PCE price index", "yoy"),
    "CPILFESL": ("Core CPI index", "yoy"),
    "CPIAUCSL": ("CPI-U index", "yoy"),
    "UNRATE": ("Unemployment rate, %", "level"),
    "PAYEMS": ("Nonfarm payrolls, thousands", "mom_diff"),
    "GDPC1": ("Real GDP, chained dollars", "qoq_saar"),
    "RSAFS": ("Retail and food services sales", "mom"),
}


def alfred_vintage(series: str, vintage: date, key: str) -> list[tuple[date, float]]:
    """The whole series exactly as ALFRED had it on ``vintage`` (no later revisions)."""
    q = urllib.parse.urlencode(
        {
            "series_id": series,
            "api_key": key,
            "file_type": "json",
            "realtime_start": vintage.isoformat(),
            "realtime_end": vintage.isoformat(),
            "observation_start": (vintage - timedelta(days=800)).isoformat(),
        }
    )
    data = json.loads(base.get(f"https://api.stlouisfed.org/fred/series/observations?{q}"))
    out = []
    for o in data.get("observations", []):
        if o.get("value") not in (None, "", "."):
            out.append((date.fromisoformat(o["date"]), float(o["value"])))
    return out


def alfred_summary(obs: list[tuple[date, float]], how: str) -> str:
    """One line describing the latest observation of a vintage. Pure function, unit-tested."""
    if not obs:
        return "n/a"
    d, v = obs[-1]
    if how == "level":
        return f"{v:g} ({d:%b %Y})"
    if how == "yoy":
        prior = [x for x in obs if x[0] == date(d.year - 1, d.month, 1)]
        return f"{(v / prior[0][1] - 1) * 100:.1f}% y/y ({d:%b %Y})" if prior else f"{v:g} ({d:%b %Y})"
    if how == "mom_diff" and len(obs) > 1:
        return f"{v - obs[-2][1]:+,.0f}k m/m ({d:%b %Y})"
    if how == "mom" and len(obs) > 1:
        return f"{(v / obs[-2][1] - 1) * 100:+.1f}% m/m ({d:%b %Y})"
    if how == "qoq_saar" and len(obs) > 1:
        return f"{((v / obs[-2][1]) ** 4 - 1) * 100:+.1f}% q/q annualized ({d.year} Q{(d.month - 1) // 3 + 1})"
    return f"{v:g} ({d:%b %Y})"


def alfred_doc(t: date, key: str) -> tuple[str, str, str, str]:
    vint = t - timedelta(days=1)
    lines = [f"Macro data as first published and known on {vint.isoformat()} (ALFRED vintage; no later revisions).", ""]
    for sid, (label, how) in ALFRED_SERIES.items():
        lines.append(f"{label} [{sid}]: {alfred_summary(alfred_vintage(sid, vint, key), how)}")
        time.sleep(0.6)
    return (
        "https://alfred.stlouisfed.org/",
        f"First-release macro snapshot as of {vint.isoformat()}",
        "\n".join(lines),
        vint.isoformat(),
    )


def latest_before(cands: list[tuple[str, object]], t: date) -> tuple[str, object] | None:
    prior = [c for c in cands if date.fromisoformat(c[0]) < t]
    return max(prior, key=lambda c: c[0]) if prior else None


def cmd_context(args) -> int:
    with open(SCEN / "events.csv", newline="", encoding="utf-8") as f:
        ts = sorted({date.fromisoformat(r["as_of"]) for r in csv.DictReader(f)})
    changes = load_changes(SCEN / "fomc_changes.csv")
    meetings = [m["end_date"] for m in load_meetings(SCEN / "fomc_meetings.csv") if "cancelled" not in m["note"]]
    meetings = sorted(set(meetings) | {c["announcement_date"] for c in changes})
    min_rel = minutes_release_dates()
    _, cpi_rel, emp_rel = base.calendar()
    today = date.today()

    wanted: dict[str, tuple] = {}  # doc_id -> (kind, published, fetcher)
    for t in ts:
        st = latest_before([(m, m) for m in meetings], t)
        if st:
            wanted[f"fomc_statement_{st[0].replace('-', '')}"] = ("fomc_statement", st[0], st[1])
        mins = [(rel, ymd) for ymd, rel in min_rel.items() if date.fromisoformat(rel) < today]
        mn = latest_before(mins, t)
        if mn:
            wanted[f"fomc_minutes_{mn[1]}"] = ("fomc_minutes", mn[0], mn[1])
        seps = [(m, m) for m in meetings if int(m[5:7]) in (3, 6, 9, 12)]
        for cand in sorted((c for c in seps if date.fromisoformat(c[0]) < t), reverse=True)[:2]:
            wanted.setdefault(f"fomc_sep_{cand[0].replace('-', '')}", ("fomc_sep", cand[0], cand[1]))
        for kind, rel in (("bls_cpi", cpi_rel), ("bls_jobs", emp_rel)):
            c = latest_before([(pub, mdy) for mdy, pub in rel.items() if date.fromisoformat(pub) < today], t)
            if c:
                wanted[f"{kind}_{c[0].replace('-', '')}"] = (kind, c[0], c[1])

    (OUT / "docs").mkdir(parents=True, exist_ok=True)
    index: list[dict] = []

    def save(doc_id: str, kind: str, pub: str, url: str, title: str, body: str) -> None:
        if len(body) < 200:
            print(f"  WARNING: short body for {doc_id} ({len(body)} chars)", file=sys.stderr)
        (OUT / "docs" / f"{doc_id}.md").write_text(body.strip() + "\n", encoding="utf-8", newline="\n")
        index.append({"id": doc_id, "kind": kind, "published": pub, "title": title, "source_url": url,
                      "file": f"docs/{doc_id}.md", "chars": len(body)})  # fmt: skip
        print(f"  {pub} {doc_id:34} {len(body):5} chars")

    # SEP: keep only the latest SEP actually published before each T (some meetings had none, e.g. Mar 2020).
    sep_cache: dict[str, tuple | None] = {}
    for kind, pub, key in sorted(wanted.values(), key=lambda v: v[1]):
        if kind == "fomc_sep":
            sep_cache[pub] = sep_table(key.replace("-", ""))
            time.sleep(0.3)
    for t in ts:
        got = sorted((p for p, v in sep_cache.items() if v and date.fromisoformat(p) < t), reverse=True)
        if got:
            p = got[0]
            url, title, body = sep_cache[p]
            doc_id = f"fomc_sep_{p.replace('-', '')}"
            if doc_id not in {d["id"] for d in index}:
                save(doc_id, "fomc_sep", p, url, title, body)

    for doc_id, (kind, pub, key) in sorted(wanted.items(), key=lambda kv: (kv[1][1], kv[0])):
        if kind == "fomc_sep":
            continue
        if kind == "fomc_statement":
            url, title, body = base.statement(key.replace("-", ""))
        elif kind == "fomc_minutes":
            url, title, body = minutes_doc(key)
        else:
            url, title, body, pub = bls_doc(kind, key)
        save(doc_id, kind, pub, url, title, body)
        time.sleep(0.3)

    # BEA PCE: probe every month that could be the latest release before some T.
    pce: list[tuple[str, tuple]] = []
    for t in ts:
        for back in range(1, 5):
            y, m = t.year, t.month - back
            while m <= 0:
                y, m = y - 1, m + 12
            key = (y, m)
            if any(k == key for _, (k, _x) in pce):
                continue
            got = bea_pce(y, m)
            if got:
                pce.append((got[3], (key, got)))
            time.sleep(0.2)
    pce_ids = set()
    for t in ts:
        c = latest_before([(pub, v) for pub, v in pce], t)
        if c and c[0] not in pce_ids:
            pce_ids.add(c[0])
            (_y, _m), (url, title, body, pub) = c[1]
            save(f"bea_pce_{pub.replace('-', '')}", "bea_pce", pub, url, title, body)

    rows = treasury_rows(range(ts[0].year - 1, ts[-1].year + 1))
    for t in ts:
        url, title, body, pub = yields_doc(rows, t)
        doc_id = f"treasury_yields_{pub.replace('-', '')}"
        if doc_id not in {d["id"] for d in index}:
            save(doc_id, "treasury_yields", pub, url, title, body)

    kinds = {
        "fomc_statement": "FOMC post-meeting statement (latest before T)",
        "fomc_minutes": "FOMC minutes: staff review of the economy + policy discussion (dated by release day)",
        "fomc_sep": "Summary of Economic Projections table: dot-plot medians (latest before T)",
        "bls_cpi": "BLS CPI news release, summary text (CPI and core CPI)",
        "bls_jobs": "BLS Employment Situation, summary text (unemployment, payrolls)",
        "bea_pce": "BEA Personal Income and Outlays release (PCE and core PCE inflation, spending)",
        "treasury_yields": "Treasury par yields: 3m, 2y, 10y and curve spreads, dated before T",
    }
    key = os.environ.get("FRED_API_KEY")
    if key and not args.no_alfred:
        kinds["alfred_firstrelease"] = "ALFRED vintage as known the day before T (first-release data)"
        for t in ts:
            url, title, body, pub = alfred_doc(t, key)
            save(f"alfred_firstrelease_{pub.replace('-', '')}", "alfred_firstrelease", pub, url, title, body)
    else:
        print("  FRED_API_KEY not set: skipping the ALFRED first-release snapshot (optional)", file=sys.stderr)

    index.sort(key=lambda d: (d["published"], d["id"]))
    meta = {
        "description": "US-government public-domain documents for fed-quarterly. For each as-of date T the agent "
        "gets the latest document of each kind whose 'published' date is strictly before T; nothing else.",
        "license": "Public domain (works of the Federal Reserve Board, BLS, BEA and the U.S. Treasury).",
        "license_sources": [
            "https://www.federalreserve.gov/disclaimer.htm",
            "https://www.bls.gov/opub/copyright-information.htm",
            "https://www.bea.gov/help/guidelines-for-citing-bea",
            "https://home.treasury.gov/subfooter/site-policies-and-notices",
        ],
        "built": today.isoformat(),
        "builder": "tools/build_fed_quarterly.py",
        "kinds": kinds,
        "docs": index,
    }
    (OUT / "index.json").write_text(
        json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(index)} docs to {OUT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("step", choices=["meetings", "events", "context", "all"])
    p.add_argument("--today", help="YYYY-MM-DD: quarters ending before this are resolved (default: today)")
    p.add_argument("--no-alfred", action="store_true", help="skip ALFRED even if FRED_API_KEY is set")
    args = p.parse_args(argv)
    rc = 0
    if args.step in ("meetings", "all"):
        rc |= cmd_meetings(args)
    if args.step in ("events", "all"):
        rc |= cmd_events(args)
    if args.step in ("context", "all"):
        rc |= cmd_context(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
