# fed-quarterly

Stand on a past date T, read only what was public before T, and forecast where the Fed's target range
for the federal funds rate will be at the end of the quarter. Then roll T forward and do it again.

| | |
|---|---|
| As-of dates T | Feb 15, May 15, Aug 15, Nov 15 of every year, 2016-2026 |
| Target | The FOMC target range at the quarter-end after T: Mar 31, Jun 30, Sep 30, Dec 31 (about 6 weeks ahead) |
| Options | Net change from T: `Cut 100+ bp`, `Cut 75 bp`, `Cut 50 bp`, `Cut 25 bp`, `No change`, `Hike 25 bp`, `Hike 50 bp`, `Hike 75+ bp` |
| Points | **43 backtest** (2016 Q1 - 2026 Q3) + **1 live** (T = 15 Nov 2026 -> 31 Dec 2026, covers the 8-9 Dec 2026 FOMC) |
| Scored by | Accuracy, Brier, Brier skill vs the best non-hindsight baseline, log loss, calibration, bootstrap CIs, plus **direction accuracy** (down / hold / up) and **mean absolute error in bp** |

The answer key has 23 `No change`, 11 `Hike 25 bp`, 4 `Cut 25 bp`, 2 `Hike 75+ bp`, and one each of
`Cut 100+ bp` (Mar 2020), `Cut 50 bp` (Sep 2024) and `Hike 50 bp` (Dec 2022). "No change" is right 53%
of the time, so the interesting points are the turns.

## Two modes

| `--mode` | Events | What you get |
|---|---|---|
| `backtest` (default) | The 43 quarters whose end has passed | Scores against the answer key, right away |
| `live` | Quarters not yet resolved (now: 2026 Q4) | A timestamped `forecast.csv`, unscored. Nothing can leak: the outcome doesn't exist yet |

When the quarter ends, add any new change to `fomc_changes.csv` (none needed if the Fed held), run
`python tools/build_fed_quarterly.py events`, then `epochal-pilot score <live run folder>` (or
`score --latest`). You can also type the outcome into the live row of `events.csv` by hand.

## Leak check: is it forecasting or remembering?

Epochal freezes the web, not the model. A model trained after 2020 has read what the Fed did in 2020.

* **With context vs blind.** `--context public` (or `epochal`) gives documents released before T plus
  a derived SEP-gap line (year-end median vs the current midpoint, implied 25 bp moves, remaining
  meetings). `--no-context` asks the same question with only the as-of date and the current range —
  no documents, no derived facts, and no rule analysis. A confidently correct blind answer is a sign
  of memorisation, not reading.
* **`--mask-dates`.** Same documents, but calendar years are rewritten relative to T (`Year 0`,
  `Year -1`, …). Use this for TypeSafe Jev, whose training cutoff is unpublished.
* **Pre- vs post-cutoff.** Run the same events with an **old-cutoff** model and a **recent** one.
  `pre/post cutoff` columns in the report split each model's accuracy at its training cutoff
  (`--cutoff MODEL=YYYY-MM` sets it). Accuracy that drops after the cutoff is memory, not skill.
* `--modes gated,ungated` is a related check (ungated drops the documents but not the derived line's
  sibling fields already in the template). Prefer `--no-context` for a clean blind run.
* `--mode live` is the clean test: forecasts made before the outcome exists.
* **Official jev-rules flow** (`--backend jev-rules`): gather the first-release pack published strictly
  before T, analyse it against business rules **in code**, then ask Jev (Choice) on context + rule
  analysis + rules text. `--backend jev` is the same Choice without the rule analysis; `--backend rules`
  is the checklist alone (no model). Other official System One Decisions APIs (`openai-decisions`,
  `gemini-decisions`) can use the same code analysis via `--with-rules`. See
  [System One vs System Two](../../README.md#system-one-vs-system-two) and the
  [jev-rules flow](../../README.md#jev-rules-flow) in the project README.

```powershell
epochal-pilot run fed-quarterly --only fq_2022q1 --context public --backend ollama --model qwen2.5:3b --out runs\fq-public
epochal-pilot run fed-quarterly --only fq_2022q1 --no-context --backend ollama --model qwen2.5:3b --out runs\fq-blind

# TypeSafe Jev / official pipeline (0 Epochal requests; needs TYPESAFE_API_KEY for jev / jev-rules)
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --out runs\fq-jev
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --no-context --out runs\fq-jev-blind
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --mask-dates --out runs\fq-jev-mask
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev-rules --out runs\fq-jev-rules
epochal-pilot run fed-quarterly --only fq_2022q1 --backend openai-decisions --out runs\fq-openai-decisions
epochal-pilot run fed-quarterly --only fq_2022q1 --backend gemini-decisions --out runs\fq-gemini-decisions
epochal-pilot run fed-quarterly --only fq_2022q1 --backend rules --out runs\fq-rules
```

## Data points and triggers the model is given

The prompt (`prompt_gated.md`) lists what to weigh and the rules of thumb rates desks use:

| Group | Data points | In the public pack as |
|---|---|---|
| Inflation | Core PCE (the Fed's 2% target measure), CPI and core CPI | `bea_pce`, `bls_cpi` |
| Labor market | Unemployment rate, payroll growth | `bls_jobs` |
| The Fed's signals | Latest statement wording, dot plot (SEP median), minutes | `fomc_statement`, `fomc_sep`, `fomc_minutes` |
| Market pricing | 2-year Treasury yield vs the policy range, yield curve (10y-2y, 10y-3m) | `treasury_yields` |
| Growth | GDP, business surveys, consumer spending | the minutes' staff review; `bea_pce` (spending) |
| Shocks | Financial stress, bank failures, pandemics | statements and minutes; news via `--context epochal` |

Triggers: core inflation well above 2% with low unemployment leans toward a hike; rising unemployment,
weak growth or a shock leans toward a cut (shocks can bring unscheduled moves); a change in statement
language ("patient", "further policy firming") usually comes before a turn; the 2-year yield shows what
markets expect. The prompt also lists the **scheduled** FOMC meetings between T and the quarter-end
(the calendar is published a year ahead; unscheduled meetings are never listed). The prompt also
includes a **derived SEP-gap line** computed only from the latest SEP published before T: the median
year-end funds rate versus the current range midpoint, the gap in bp, the implied number of 25 bp
moves, and how many scheduled meetings remain. If no SEP is available before T, the line says so
instead of guessing.

## Context sources

| `--context` | What the model sees | Epochal requests |
|---|---|---|
| `public` | For each kind below, the latest public-domain document released strictly before T | **0** |
| `epochal` | 10 web search results as of T (published in the 45 days before T), no page fetches | **1 per T** |
| `--no-context` | Blind: as-of date and current range only; no documents, derived facts, or rule analysis | **0** |
| `--mask-dates` | Same context; calendar years rewritten relative to T | same as the context flag |
| `--backend jev`, `jev-rules`, or `rules` | Official / comparison judgment path; defaults to `public` | **0** |

### Public first-release pack (`public_context/`)

Built by `python tools/build_fed_quarterly.py context` from US-government sources only. It ships just
the documents that are the latest of their kind before some T (7 per T, 307 in all):

| Kind | Source | Dated by |
|---|---|---|
| `fomc_statement` | federalreserve.gov press release | Meeting day |
| `fomc_minutes` | federalreserve.gov minutes: policy discussion + staff review excerpts | Public release day (about 3 weeks after) |
| `fomc_sep` | Summary of Economic Projections, Table 1 (dot-plot medians) | Meeting day |
| `bls_cpi` | BLS CPI news release, summary | Release day |
| `bls_jobs` | BLS Employment Situation, summary | Release day |
| `bea_pce` | BEA Personal Income and Outlays: PCE and core PCE prices | Release day (from the embargo line) |
| `treasury_yields` | Treasury daily par yield curve: 3m, 2y, 10y and spreads, now / 1m / 3m / 12m before | Last business day before T |

**First release only.** Every number is the one printed in the release at the time, so later revisions
can't leak in. Treasury market yields are never revised.

**Optional ALFRED snapshot.** With `FRED_API_KEY` set (free at
[fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html)), the tool adds an
`alfred_firstrelease` doc per T: core PCE, core CPI, CPI, unemployment, payrolls, real GDP and retail
sales exactly as ALFRED had them the day before T. Not shipped by default (no key on the build machine).

**Gaps.** ISM survey data is proprietary and not included. GDP and retail sales come only through the
minutes' staff review (and ALFRED, if you build it). There is no financial-stress index without ALFRED;
stress shows up in statements and minutes (e.g. March 2020, March 2023) and in the Epochal news. A few
monthly BEA pages are missing from bea.gov (Jun 2017, Dec 2018-Feb 2019, Aug 2022, Oct-Nov 2025), so
those T's use the previous release, as a reader at the time would have during the shutdowns.

`epochal-pilot validate fed-quarterly` re-checks that every selected document predates its T.

**License.** Works of the Federal Reserve Board, BLS, BEA and the U.S. Treasury are in the public
domain ([Board](https://www.federalreserve.gov/disclaimer.htm),
[BLS](https://www.bls.gov/opub/copyright-information.htm),
[BEA](https://www.bea.gov/help/guidelines-for-citing-bea),
[Treasury](https://home.treasury.gov/subfooter/site-policies-and-notices)).

## Spending Epochal requests

Ten as-of dates are tagged `turning-point` (where the path turned or moved by more than usual):
2016-11-15, 2018-11-15, 2019-05-15, 2019-08-15, 2020-02-15, 2022-02-15, 2022-05-15, 2024-08-15,
2025-08-15, 2026-08-15. With `max_fetches: 0` each costs exactly one search.

```bash
epochal-pilot plan fed-quarterly --requests 10 --only turning-point      # 0 requests: shows the plan
epochal-pilot run  fed-quarterly --only turning-point --estimate         # 0 requests: exact cost (10)
epochal-pilot run  fed-quarterly --only turning-point --budget 10        # 10 requests, then cached
epochal-pilot run  fed-quarterly --context public --only turning-point   # same 10 points, 0 requests
epochal-pilot compare runs/<epochal run> runs/<public run>               # what the as-of web adds
```

The live point costs one more request: `run fed-quarterly --mode live --budget 1`.

## Files

| File | What |
|---|---|
| `fomc_changes.csv` | All 31 target-range changes Dec 2016 - Sep 2026, each with its federalreserve.gov statement URL |
| `fomc_meetings.csv` | Scheduled FOMC meetings 2015-2026 (the 17-18 Mar 2020 meeting was scheduled, then cancelled) |
| `events.csv` | Generated: one row per T with `as_of`, `range_at_asof`, `outcome`, `change_bp`, `direction`, `previous_outcome`, `meetings_in_window`, `tags`, `status` |
| `public_context/` | Generated: the public pack |

## Baselines

`no-change`, `persistence` (repeat the most recent FOMC decision before T), `uniform`, and `majority`
(hindsight; context only). Bring your own market-implied odds with `--baseline-csv` if you are licensed
to use them.

Not investment advice.
