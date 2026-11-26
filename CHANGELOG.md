# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
