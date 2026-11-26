# Command reference

All commands are also available as `epilot`. Flags below match `src/epochal_pilot/cli.py`.

| Command | What it does | Epochal requests |
|---|---|---|
| `list`, `check` | Scenarios; which keys are set (never values); whether `claude` is on PATH; lifetime Epochal requests in this cache | 0 |
| `run <scenario>` | Run the pipeline; writes predictions, scores, and `report.html` | 0 with `--context public`, `--no-context`, or `--dry-run` |
| `run ... --mode live` | Forecast unresolved events; writes `forecast.csv` | 0 with `--context public` |
| `run ... --estimate` | Show Epochal cost and exit | 0 |
| `run/plan ... --only <ids or tags>` | Select events, e.g. `--only turning-point` | — |
| `plan <scenario> --requests N` | Choose events and lead times for N requests; prints a `run` command | 0 |
| `score <run_dir>` / `score --latest` | Score a live forecast once its outcome is in the scenario | 0 |
| `report <run_dir> [--share]` / `report --latest` | HTML + markdown report; `--share` writes a metrics-only folder | 0 |
| `compare <run_dir>...` | Leaderboard and paired tests across run folders | 0 |
| `validate <scenario>` | Schema, cutoffs, wording, public-pack leak check | 0 |
| `new <name>` | Scaffold a scenario folder | 0 |

Useful `run` flags: `--backend`, `--with-rules`, `--analyst`, `--context {public,epochal}`,
`--no-context`, `--mask-dates`, `--mode {backtest,live}`, `--budget`, `--max-total`,
`--lead-days`, `--modes`, `--limit`, `--seed`, `--no-shuffle`, `--num-results`,
`--max-fetches`, `--plan-queries`, `--cutoff MODEL=YYYY-MM`, `--baseline-csv`, `--out`.

## Budget

Epochal accounts include **100 free requests**. Only 2xx responses count; failed requests are
refunded locally. Responses are cached, so reruns with more models or seeds do not spend again.
`--budget` defaults to 10 new requests this run; `--max-total` defaults to 90 for this cache
(leave a margin under the free-tier cap). Run `--estimate` before you spend.

| Context | Epochal requests |
|---|---|
| `--context public` or `--no-context` or `--dry-run` | **0** |
| `--context epochal --only turning-point` | **exactly 10** (one search per turning-point date; cached after) |
| `--mode live --context epochal` | 1 |
| `plan <scenario> --requests N` | 0 (prints a `run` command that fits N) |

`--plan-queries K` costs K extra requests per event; `--max-fetches` can add a fetch after a thin
search. System One backends default to `--context public`, so a forgotten flag cannot spend
Epochal requests. System Two backends default to `--context epochal` — pass `--context public`
if you do not want to spend.

See the README section
[Where the data comes from: `--context public` vs `--context epochal`](../README.md#where-the-data-comes-from---context-public-vs---context-epochal).
