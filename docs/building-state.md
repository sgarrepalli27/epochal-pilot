# Building state

Context is gathered as of T, then reduced to a JSON state for the predictor. Choose the source with
`--context public` or `--context epochal` (see the README section
[Where the data comes from](../README.md#where-the-data-comes-from---context-public-vs---context-epochal)).

Derived facts (for example the SEP gap and remaining FOMC meetings) and in-code rule signals are
then attached, producing the state JSON. The shape matches `build_state` in
`src/epochal_pilot/jev.py`. Blind runs (`--no-context`) omit `documents`, `derived_facts`,
`rule_analysis`, and `business_rules`.

## Illustrative state (not a live payload)

Numbers and text below are **illustrative**, not copied from a live API response or a real run.

```json
{
  "as_of": "2022-02-15",
  "forecast_date": "2022-03-31",
  "policy_range": "0.00-0.25%",
  "question": "What will the net change in the FOMC target range be by quarter-end?",
  "scheduled_meetings_before_target": "16 March 2022",
  "derived_facts": "SEP median end-2022 0.9% vs current midpoint 0.125% = +77.5 bp (illustrative).",
  "documents": [
    {
      "title": "FOMC Statement",
      "published": "2022-01-26",
      "url": "https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
      "text": "…"
    }
  ],
  "rule_analysis": {
    "inflation": {"core_pce_yoy_pct": 5.2, "gap_vs_2pct_goal_pp": 3.2, "measure_used": "core_pce"},
    "labour": {"unemployment_pct": 4.0, "unemployment_trend": "stable"},
    "shock_present": false,
    "rules_point_estimate": {"expected_bp": 25, "nearest_bucket": "Hike 25 bp"}
  },
  "business_rules": "Core inflation well above 2% with a tight labour market: lean hike. …"
}
```

## Adding another time-portal source

Epochal is the built-in time-portal client (`src/epochal_pilot/epochal.py`). To add another
date-gated source:

1. Implement a client that gates every request on T (Epochal uses the `Accept-Datetime` header).
2. Filter results so only documents published **strictly before T** are kept.
3. Return documents as `{title, published_at, url, text}` so they can enter `build_state`.
4. Wire a new `--context` value in the runner, or contribute a first-release pack under
   `public_context/` (see [adding-a-scenario.md](adding-a-scenario.md)).

Do not include API keys in URLs, errors, logs, or cache keys.
