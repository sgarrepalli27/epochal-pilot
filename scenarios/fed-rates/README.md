# fed-rates

Predict each FOMC decision from the web as it was `lead_days` (default 7) before the meeting.

| Field | Meaning |
|---|---|
| `event_date` | Day the FOMC statement was released (the second day of the meeting) |
| `outcome` | Decision, one of `Hold`, `Cut 25 bp`, `Cut 50 bp`, `Cut 75 bp`, `Hike 25 bp` |
| `range_before` / `range_after` | Target range before and after; `range_before` is shown to the model (it was public) |
| `previous_outcome` | Prior meeting's decision, used by the `persistence` baseline |
| `source_url` | The Federal Reserve press release with the decision |

Baselines: `no-change` (always Hold), `persistence` (same as last meeting), `majority` (most common
outcome in the evaluated set; uses hindsight, so it is a ceiling for naive guessing), `uniform`.

A market-implied baseline (CME FedWatch) is not included: historical FedWatch probabilities are not
freely redistributable. Add a `market_probs` column if you have a licensed source.

Note: a week before a meeting the market usually prices the decision correctly, so high accuracy here
mostly shows the agent can read market expectations. Try `--lead-days 28` or `--lead-days 56` for a harder test.
