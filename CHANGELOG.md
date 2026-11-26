# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-11-26

Rolling quarterly Fed forecasts, with a backtest and a live forward test.

### Added
- **`fed-quarterly` scenario**: from each as-of date T (Feb 15, May 15, Aug 15, Nov 15, 2016-2026),
  forecast the net change in the FOMC target range by the quarter-end (8 buckets from `Cut 100+ bp` to
  `Hike 75+ bp`). 43 backtest points and 1 live point (T = 2026-11-15, resolves after 2026-12-31). The
  answer key is derived from `fomc_changes.csv` (31 changes, each checked against federalreserve.gov).
  The prompt lists the data points and triggers to weigh, and the scheduled FOMC meetings before
  quarter-end. Ten as-of dates are tagged `turning-point` for a 10-request Epochal run.
- **First-release public pack** for `fed-quarterly` (307 docs, 0 requests): FOMC statements, minutes
  (policy + staff review), SEP dot-plot table, BLS CPI and jobs, BEA PCE and Treasury yield-curve
  snapshots, each the latest released strictly before T. `tools/build_fed_quarterly.py` rebuilds the
  meetings calendar, the answer key and the pack; with `FRED_API_KEY` it adds an ALFRED vintage snapshot.
- **`run --mode live`** forecasts unresolved events (`status=live`, blank outcome) and writes a
  timestamped `forecast.csv`; **`epochal-pilot score <dir>|--latest`** grades it once the outcome is in.
- **`--only` accepts tags** (e.g. `--only turning-point`) in `run` and `plan`.
- Events may pin a fixed **`as_of`** date (rolling calendars); `lead_days` then doesn't apply.
- **Direction accuracy and mean absolute error (bp)** for scenarios that map choices to directions and
  sizes (`directions`, `choice_bp` in scenario.json); in the console, `summary.md` and re-scored reports.
- `context_chars_per_doc` in scenario.json.
- **`report --latest`**: report the newest run folder under `runs/`.

### Changed
- A run that scores nothing now prints a clear **"No results"** message with the reason (for example an
  expired Claude Code login) instead of an empty table, and the report says the same.
- `report` on a folder without `predictions.csv` explains what a run folder looks like and suggests
  `--latest`; on a live folder it points to `score`.
- `validate` reports backtest/live counts and tags, accepts live rows and checks `as_of < event_date`.

## [0.2.0] - 2026-11-26

An evaluation harness first: most features now need zero Epochal requests.

### Added
- **Statistics** (`stats.py`, stdlib): seeded bootstrap 95% CIs for accuracy, Brier and log loss;
  Brier skill score (with CI) vs the best non-hindsight baseline; log loss; calibration bins,
  reliability table and ECE; paired bootstrap and exact McNemar tests (model vs reference, gated vs
  ungated, run vs run); small-sample warnings. Hindsight baselines are flagged and never a reference.
- **Public-domain context** (`--context public`): 114 trimmed Federal Reserve and BLS documents for
  `fed-rates`, each with source URL and release date; only documents released before the as-of date
  are used, with a leak check in `validate` and the tests. `tools/build_public_context.py` rebuilds it.
- **Reports**: `epochal-pilot report <run>` (self-contained HTML with inline SVG reliability chart,
  per-event table, CIs, baselines, cost ledger; plus markdown), `--share` metrics-only export,
  `epochal-pilot compare <runs...>` leaderboard with paired tests (e.g. public vs Epochal context).
- **Planning**: `epochal-pilot plan <scenario> --requests N` picks events and lead times that carry the
  most signal (post-cutoff, contested) per request.
- **Validation**: `epochal-pilot validate <scenario>` (schema, dates vs model cutoffs, source URLs,
  past-tense / answer-leaking wording, public-pack leaks) and `epochal-pilot new <name>` scaffold.
- **`claude-cli` backend**: local Claude Code (`claude -p --output-format json`) with every tool and
  MCP server disabled, prompt on stdin, no API key; `check` reports whether `claude` is on PATH.
- **10 new FOMC meetings** (Sep 2025 - Oct 2026, including the Sep 2026 hike), verified against
  federalreserve.gov; `models.json` lists post-cutoff events per model.
- `--baseline-csv` for user-supplied forecasts (e.g. licensed market odds) as a non-hindsight reference.
- Run manifest in `run.json`: git SHA, package version, event/prompt/scenario hashes, model params,
  seed, search settings, and an Epochal ledger with request ids and copy ids (stripped from `share/`).
- Search filters in `scenario.json`: `categories`, `published_after_days` (default 30), `domains`,
  `exclude_domains`. `docs/what-you-need.md`.

### Changed
- `num_results` defaults to 10 (the API maximum, same cost as 1).
- `--estimate` counts searches, fetches (exact when the search is cached) and planned queries.
- The budget counts only 2xx responses: failed requests are refunded locally (Epochal doesn't bill them).
- The fetch date check uses the fetched copy's `published_at`, not the echoed `as_of`.
- `future-leak` questions are asked in the future tense; the iPhone 16e question no longer names the product.
- Ollama context window raised to 16k tokens (`OLLAMA_NUM_CTX` to override).
- Because search bodies changed, v0.1 cached searches are not reused.

## [0.1.0] - 2026-11-25

### Added
- `epochal-pilot` CLI: `list`, `check`, `run` with `--dry-run`, `--estimate`, `--budget`, `--max-total`,
  `--lead-days`, `--modes`, `--seed`, `--plan-queries` and `--cutoff`.
- Epochal Time Portal client, on-disk cache and a persistent request budget.
- OpenRouter and Ollama backends; structured JSON forecasts with a forced-choice retry.
- Leak guard, accuracy, multi-class Brier, pre/post cutoff split, position-bias and citation checks, baselines.
- Scenarios: `fed-rates` (13 FOMC meetings) and `future-leak` (20 events).
- Named `epochal-pilot` (alias `epilot`); Anthropic backend; Windows support and CI on Windows x64/ARM64 and macOS.
