"""Rebuild the fed-rates public-domain context pack (maintainer tool; uses the public web, never Epochal).

    python tools/build_public_context.py            # writes scenarios/fed-rates/public_context/

What it collects, for the whole event window plus one month of lead-in:

* FOMC post-meeting statements      federalreserve.gov/newsevents/pressreleases/monetaryYYYYMMDDa.htm
* FOMC minutes (policy discussion)  federalreserve.gov/monetarypolicy/fomcminutesYYYYMMDD.htm
* BLS CPI news release (summary)    bls.gov/news.release/archives/cpi_MMDDYYYY.htm
* BLS Employment Situation (summary) bls.gov/news.release/archives/empsit_MMDDYYYY.htm

Each document is trimmed to its decision-relevant text and saved with its source URL and its
public release date. Federal Reserve Board and BLS web content is in the public domain
(https://www.federalreserve.gov/disclaimer.htm, https://www.bls.gov/opub/copyright-information.htm).
"""

from __future__ import annotations

import csv
import gzip
import html
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios" / "fed-rates"
OUT = SCEN / "public_context"
FED = "https://www.federalreserve.gov"
BLS = "https://www.bls.gov"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}
MAX_CHARS = {"fomc_statement": 3500, "fomc_minutes": 6000, "bls_cpi": 3000, "bls_jobs": 3000}


def get(url: str) -> str:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except Exception as e:  # pragma: no cover - network
            print(f"  retry {attempt + 1} {url}: {e}", file=sys.stderr)
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"could not fetch {url}")


def text_of(fragment: str, sep: str = "\n") -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", fragment, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\u2011", "-").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            blank = True
            continue
        if blank and out:
            out.append("")
        out.append(ln)
        blank = False
    return sep.join(out).strip()


def cap(t: str, n: int) -> str:
    if len(t) <= n:
        return t
    cut = t.rfind("\n\n", 0, n)
    return t[: cut if cut > n // 2 else n].rstrip() + "\n\n[...trimmed]"


def statement(ymd: str) -> tuple[str, str, str]:
    url = f"{FED}/newsevents/pressreleases/monetary{ymd}a.htm"
    page = get(url)
    m = re.search(r'<div[^>]+id="article"[^>]*>(.*?)<div[^>]+id="lastUpdate"', page, re.S)
    body = text_of(m.group(1) if m else page)
    for stop in ("Implementation Note", "For media inquiries", "Last Update"):
        i = body.find(stop)
        if i > 0:
            body = body[:i]
    body = "\n".join(ln for ln in body.splitlines() if ln.strip() not in {"Share", "-->"} and "-->" not in ln)
    body = re.sub(r"\n{3,}", "\n\n", body)
    title = f"FOMC statement, {datetime.strptime(ymd, '%Y%m%d'):%d %B %Y}"
    return url, title, cap(body.strip(), MAX_CHARS["fomc_statement"])


def minutes(ymd: str) -> tuple[str, str, str]:
    url = f"{FED}/monetarypolicy/fomcminutes{ymd}.htm"
    page = get(url)
    body = text_of(page)
    paras = [p.strip() for p in body.split("\n") if len(p.strip()) > 200]
    # Participants' discussion of the policy outlook + the Committee's policy action.
    full = "\n".join(paras)
    j = full.find("In their discussion")
    if j < 0:
        j = full.find("members agreed that")
    keep: list[str] = []
    pol = [p for p in paras if re.search(r"monetary policy|policy rate|target range|federal funds rate", p)]
    action_i = next(
        (
            i
            for i, p in enumerate(paras)
            if p.startswith(("In their discussions of monetary policy", "In their discussion of monetary policy"))
        ),
        None,
    )
    if action_i is not None:
        before = [p for p in paras[:action_i] if p in pol][-3:]
        keep = before + paras[action_i : action_i + 2]
    else:
        keep = pol[-5:]
    title = f"Minutes of the FOMC meeting of {datetime.strptime(ymd, '%Y%m%d'):%d %B %Y} (policy discussion excerpt)"
    return url, title, cap("\n\n".join(keep), MAX_CHARS["fomc_minutes"])


def bls_release(kind: str, mdy: str) -> tuple[str, str, str]:
    stem = "cpi" if kind == "bls_cpi" else "empsit"
    url = f"{BLS}/news.release/archives/{stem}_{mdy}.htm"
    page = get(url)
    m = re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S)
    if m:
        body = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    else:
        i = page.find("embargoed")
        body = text_of(page[max(0, i - 200) : i + 20000])
    head = "CONSUMER PRICE INDEX" if stem == "cpi" else "THE EMPLOYMENT SITUATION"
    i = body.find(head)
    body = body[i:] if i >= 0 else body
    stops = (
        ("Table A.", "Table A ", "Consumer Price Index Data")
        if stem == "cpi"
        else (
            "The change in total nonfarm payroll employment for",
            "Revisions to",
            "Summary table A",
        )
    )
    ks = [k for k in (body.find(stop, 200) for stop in stops) if k > 0]
    if ks:
        body = body[: min(ks)]
    # Drop text-box borders used for notices (e.g. shutdown delays) but keep their words.
    body = "\n".join(re.sub(r"\s+", " ", ln.replace("|", " ")).strip() for ln in body.splitlines())
    body = "\n".join(ln for ln in body.splitlines() if not re.fullmatch(r"[_\-\s]{5,}", ln))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    # Re-flow hard-wrapped lines into paragraphs.
    body = "\n\n".join(" ".join(p.split("\n")) for p in body.split("\n\n"))
    title = "BLS Consumer Price Index news release" if stem == "cpi" else "BLS Employment Situation news release"
    first = body.split("\n\n", 1)[0][:80]
    return url, f"{title}: {first}", cap(body, MAX_CHARS[kind])


def calendar() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Minutes release dates and BLS release dates from the official listing pages."""
    cal = get(f"{FED}/monetarypolicy/fomccalendars.htm")
    minutes_rel = {}
    for m in re.finditer(r"fomcminutes(\d{8})\.htm", cal):
        seg = text_of(cal[m.end() : m.end() + 600], " ")
        r = re.search(r"Released ([A-Z][a-z]+ \d{1,2}, \d{4})", seg)
        if r:
            minutes_rel[m.group(1)] = datetime.strptime(r.group(1), "%B %d, %Y").date().isoformat()
    rel = {}
    for stem in ("cpi", "empsit"):
        page = get(f"{BLS}/bls/news-release/{stem}.htm")
        rel[stem] = {
            mdy: f"{mdy[4:]}-{mdy[:2]}-{mdy[2:4]}"
            for mdy in set(re.findall(rf"/news\.release/archives/{stem}_(\d{{8}})\.htm", page))
        }
    return minutes_rel, rel["cpi"], rel["empsit"]


def main() -> int:
    with open(SCEN / "events.csv", newline="", encoding="utf-8") as f:
        events = list(csv.DictReader(f))
    meetings = sorted({e["event_date"].replace("-", "") for e in events} | {"20231213"})
    first_needed = date(2023, 12, 1)
    last_needed = max(date.fromisoformat(e["event_date"]) for e in events)
    today = date.today()
    minutes_rel, cpi_rel, emp_rel = calendar()

    plan: list[tuple[str, str, str, str]] = []  # (doc_id, kind, published, key)
    for ymd in meetings[:-1]:  # the last meeting's own statement is the outcome: never a context doc
        pub = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        plan.append((f"fomc_statement_{ymd}", "fomc_statement", pub, ymd))
        if ymd in minutes_rel and date.fromisoformat(minutes_rel[ymd]) < last_needed:
            plan.append((f"fomc_minutes_{ymd}", "fomc_minutes", minutes_rel[ymd], ymd))
    for kind, rel in (("bls_cpi", cpi_rel), ("bls_jobs", emp_rel)):
        for mdy, pub in sorted(rel.items(), key=lambda kv: kv[1]):
            d = date.fromisoformat(pub)
            if first_needed <= d < min(last_needed, today):
                plan.append((f"{kind}_{pub.replace('-', '')}", kind, pub, mdy))

    (OUT / "docs").mkdir(parents=True, exist_ok=True)
    index = []
    for doc_id, kind, pub, key in sorted(plan, key=lambda x: (x[2], x[0])):
        if kind == "fomc_statement":
            url, title, body = statement(key)
        elif kind == "fomc_minutes":
            url, title, body = minutes(key)
        else:
            url, title, body = bls_release(kind, key)
        if len(body) < 200:
            print(f"  WARNING: short body for {doc_id} ({len(body)} chars)", file=sys.stderr)
        (OUT / "docs" / f"{doc_id}.md").write_text(body.strip() + "\n", encoding="utf-8", newline="\n")
        index.append(
            {
                "id": doc_id,
                "kind": kind,
                "published": pub,
                "title": title,
                "source_url": url,
                "file": f"docs/{doc_id}.md",
                "chars": len(body),
            }
        )
        print(f"  {pub} {doc_id:32} {len(body):5} chars")
        time.sleep(0.5)
    meta = {
        "description": "US-government public-domain documents for the fed-rates scenario. A document is "
        "only used for an event when its 'published' date is strictly before the event's as-of date.",
        "license": "Public domain (works of the Federal Reserve Board and the U.S. Bureau of Labor Statistics).",
        "license_sources": [
            "https://www.federalreserve.gov/disclaimer.htm",
            "https://www.bls.gov/opub/copyright-information.htm",
        ],
        "built": today.isoformat(),
        "builder": "tools/build_public_context.py",
        "kinds": {
            "fomc_statement": "FOMC post-meeting statement (previous meetings only)",
            "fomc_minutes": "FOMC minutes, policy-discussion excerpt (dated by public release day)",
            "bls_cpi": "BLS CPI news release, summary text",
            "bls_jobs": "BLS Employment Situation news release, summary text",
        },
        "docs": index,
    }
    (OUT / "index.json").write_text(
        json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(index)} docs to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
