# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Renamed to `epochal-pilot` (package `epochal_pilot`, CLI `epochal-pilot`, alias `epilot`).
  Cache key format unchanged, so existing cached responses stay valid.

### Added
- Anthropic Claude API backend (`--backend anthropic`, `ANTHROPIC_API_KEY`, default `claude-haiku-5-5`).
- Windows support: explicit UTF-8 I/O, BOM- and CRLF-tolerant `.env`, safe console output,
  `.gitattributes`; CI on Windows (x64 and ARM64) and macOS.
- README: step-by-step "Replicate on Windows" guide.

## [0.1.0] - 2026-11-25

### Added
- `epochal-pilot` CLI: `list`, `check`, `run` with `--dry-run`, `--estimate`, `--budget`, `--max-total`,
  `--lead-days`, `--modes`, `--seed`, `--plan-queries` and `--cutoff`.
- Epochal Time Portal client (search and fetch with `Accept-Datetime`), on-disk cache and a
  persistent request budget.
- OpenRouter (default) and Ollama backends; structured JSON forecasts with a forced-choice retry.
- Leak guard: results dated after the as-of moment are dropped and counted.
- Scoring: accuracy, multi-class Brier, pre/post training-cutoff split, position-bias check,
  citation check, and baselines (no-change, persistence, majority, uniform).
- Scenarios: `fed-rates` (13 FOMC meetings, Jan 2024 – Jul 2025, verified against federalreserve.gov)
  and `future-leak` (20 events, ported from the original probe, now with seeded option shuffling).
