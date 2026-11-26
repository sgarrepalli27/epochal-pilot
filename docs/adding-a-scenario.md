# Adding a scenario

A scenario is a folder in `scenarios/`. `epochal-pilot list` finds any folder containing `scenario.json`.

## scenario.json

| Key | Default | Meaning |
|---|---|---|
| `description` | `""` | One line shown by `epochal-pilot list` |
| `events` | `events.csv` | Events file name |
| `choices` | none | Shared options, if not given per event |
| `lead_days` | 7 | as-of = event date minus this many days (override with `--lead-days`) |
| `modes` | `["gated"]` | `gated` (as-of context) and/or `ungated` (memory only) |
| `prompts` | `prompt_<mode>.md` | Template file per mode |
| `shuffle` | `true` | Shuffle options per event (seeded by `--seed`) |
| `num_results` | 5 | Search results per query |
| `max_fetches` | 2 | Full-page fetches per event for results with under 300 characters of text |
| `baselines` | `{}` | See below |

### Baselines

```json
"baselines": {
  "constant": {"no-change": "Hold"},
  "persistence_column": "previous_outcome",
  "majority": true,
  "uniform": true
}
```

- `constant`: always predicts the named option.
- `persistence_column`: predicts the value in that CSV column (for example the previous outcome).
- `majority`: always predicts the most common outcome in the evaluated events. It uses hindsight,
  so treat it as a ceiling for naive guessing.
- `uniform`: equal probability on every option.

## events.csv

| Column | Required | Notes |
|---|---|---|
| `id` | yes | Unique, stable (it seeds the option shuffle) |
| `event_date` | yes | `YYYY-MM-DD`, the day the outcome became known |
| `question` | yes | Phrase it as a forecast, without the answer |
| `outcome` | yes | Must equal one of the choices exactly |
| `source_url` | yes | Public page that states the outcome |
| `choices` | if no shared choices | Pipe-separated, 2–8 options |
| `search_query` | no | Seed query for Epochal (defaults to the question) |
| other columns | no | Available in templates as `$column` |

## Prompt templates

Python `string.Template` syntax. Variables:

| Variable | Value |
|---|---|
| `$today` | as-of date (`YYYY-MM-DD`) |
| `$as_of` | as-of timestamp sent to Epochal |
| `$context` | Retrieved documents (gated mode) |
| `$question` | The event question |
| `$options` | Lettered, shuffled options |
| `$letters` | e.g. `A/B/C/D` |
| `$response_format` | The required JSON answer format; include it |

## Checklist

- [ ] Outcomes verified against the `source_url`, using public data only
- [ ] `epochal-pilot run <name> --dry-run` passes
- [ ] `epochal-pilot run <name> --estimate` shows a reasonable request count
- [ ] Scenario `README.md` with caveats
- [ ] Loader test in `tests/test_scenarios.py`
