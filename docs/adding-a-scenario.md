# Adding a scenario

A scenario is a folder in `scenarios/`. `epochal-pilot list` finds any folder containing `scenario.json`.

```bash
epochal-pilot new my-scenario        # scaffold scenario.json, events.csv, prompts, README
epochal-pilot validate my-scenario   # schema, dates vs model cutoffs, wording, context leaks
epochal-pilot run my-scenario --dry-run
```

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
| `num_results` | 10 | Search results per query (1-10; 10 costs the same single request) |
| `max_fetches` | 2 | Full-page fetches per event for results with under 300 characters of text |
| `published_after_days` | 30 | Only search pages published within this many days before the as-of date; `null` = no limit |
| `categories` | `[]` (all) | Epochal verticals: `news`, `finance` (SEC filings), `legal`, `publications`, `companies`, `people`, `maps` |
| `domains` / `exclude_domains` | `[]` | Restrict to / exclude sites (up to 200 entries combined, no overlap) |
| `public_context` | none | Folder with a public-domain context pack (`index.json` + `docs/`), enables `--context public` |
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
  so it is flagged `(hindsight)` and never used as the skill-score reference.
- `uniform`: equal probability on every option.

## events.csv

| Column | Required | Notes |
|---|---|---|
| `id` | yes | Unique, stable (it seeds the option shuffle) |
| `event_date` | yes | `YYYY-MM-DD`, the day the outcome became known |
| `question` | yes | A forecast in the **future tense** ("will ...?"), without the answer; `validate` flags past tense |
| `outcome` | yes | Must equal one of the choices exactly |
| `source_url` | yes | Public page that states the outcome |
| `choices` | if no shared choices | Pipe-separated, 2–8 options |
| `search_query` | no | Seed query for Epochal (defaults to the question) |
| `contested` | no | `1` marks an event as contested for `epochal-pilot plan` |
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

## Public context packs (optional)

`public_context/index.json` lists documents (`id`, `kind`, `published` as `YYYY-MM-DD`, `title`,
`source_url`, `file`) under a `docs` key, plus `kinds`. For each event the agent gets the latest
document of each kind with `published` strictly before the as-of date. Ship only material you may
redistribute (for example US-government works) and never anything returned by Epochal.

## Checklist

- [ ] Outcomes verified against the `source_url`, using public data only
- [ ] `epochal-pilot validate <name>` passes (no errors; fix wording warnings)
- [ ] `epochal-pilot run <name> --dry-run` passes
- [ ] `epochal-pilot run <name> --estimate` shows a reasonable request count
- [ ] Scenario `README.md` with caveats
- [ ] Loader test in `tests/test_scenarios.py`
