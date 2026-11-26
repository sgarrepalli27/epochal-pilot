# fed-rates

Predict each FOMC decision from information available `lead_days` (default 7) before the meeting.

| Field | Meaning |
|---|---|
| `event_date` | Day the FOMC statement was released (the last day of the meeting) |
| `outcome` | Decision, one of `Hold`, `Cut 25 bp`, `Cut 50 bp`, `Cut 75 bp`, `Hike 25 bp` |
| `range_before` / `range_after` | Target range before and after; `range_before` is shown to the model (it was public) |
| `previous_outcome` | Prior meeting's decision, used by the `persistence` baseline |
| `source_url` | The Federal Reserve press release with the decision |

23 meetings: Jan 2024 - Jul 2025 (13) and Sep 2025 - Oct 2026 (10). The later set includes three
cuts (Sep, Oct, Dec 2025), five holds, a **25 bp hike on 16 Sep 2026** and a hold after it, so it has
turning points that a "no change" rule misses. Every outcome was checked against its
federalreserve.gov press release. [`models.json`](../../src/epochal_pilot/models.json) lists which
events fall after each model's training cutoff (`fed_rates_post_cutoff`).

## Context sources

| `--context` | What the model sees | Epochal requests |
|---|---|---|
| `epochal` (default) | Web search results as of the as-of date (time-gated by Epochal) | ~1-3 per event |
| `public` | The latest document of each kind below released strictly before the as-of date | 0 |

### Public-domain context pack (`public_context/`)

114 documents, 304,393 characters, released 2023-12-08 to 2026-10-14:

| Kind | Count | Source | Dated by |
|---|---|---|---|
| FOMC statement (previous meetings only) | 23 | federalreserve.gov press releases | Meeting day |
| FOMC minutes, policy-discussion excerpt | 23 | federalreserve.gov minutes | Public release day (about 3 weeks after the meeting) |
| BLS CPI news release, summary text | 34 | bls.gov release archives | Release day |
| BLS Employment Situation, summary text | 34 | bls.gov release archives | Release day |

Each entry in `index.json` has its `source_url` and `published` date. Selection is strictly
`published < as_of date`, so a meeting's own statement is never in its context, and the leak check
in `epochal-pilot validate fed-rates` (and the test suite) re-verifies every event. At long lead times
(for example `--lead-days 28` for Jan 2024) a kind can be missing; that is reported, never back-filled.

The 2025 federal shutdown is reflected as it happened: for the 29 Oct 2025 meeting the latest CPI
and jobs releases are the early-September ones (August data), and the delayed releases appear only after their real dates.

Rebuild or extend it with `python tools/build_public_context.py` (public web only, never Epochal).
Speeches are not included: there are many per month and choosing which to include would add
selection bias.

**License.** Works of the Federal Reserve Board and the U.S. Bureau of Labor Statistics are in the
public domain ([Board website policy](https://www.federalreserve.gov/disclaimer.htm),
[BLS copyright information](https://www.bls.gov/opub/copyright-information.htm)). Source: Board of
Governors of the Federal Reserve System; U.S. Bureau of Labor Statistics.

## Baselines

`no-change` (always Hold), `persistence` (same as last meeting), `majority` (most common outcome in
the evaluated set; **hindsight**, shown for context and never used as the skill reference), `uniform`.

A market-implied baseline is not shipped: CME FedWatch, Kalshi and Polymarket terms don't allow
redistributing their historical prices. If you have data you may use, pass it with
`--baseline-csv` (columns `name,event,Hold,Cut 25 bp,...`).

Note: a week before a meeting the market usually prices the decision correctly, so high accuracy here
mostly shows the agent can read market expectations. Try `--lead-days 28` or `--lead-days 56`.
