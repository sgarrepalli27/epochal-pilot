# Contributing

Thanks for helping. The most useful contributions are **new scenarios**, **scoring improvements** and
**backends**.

## Setup

```bash
git clone https://github.com/sgarrepalli27/epochal-pilot.git && cd epochal-pilot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check . && ruff format --check .
```

Tests are offline: Epochal and model calls are mocked. Please don't add tests that need keys or network.

## Adding a scenario

1. Create `scenarios/<name>/` (see [docs/adding-a-scenario.md](docs/adding-a-scenario.md)).
2. Every event needs a resolved outcome and a public `source_url` that states it. Use public data only.
3. Run `epochal-pilot run <name> --dry-run` and `epochal-pilot run <name> --estimate`.
4. Add a short `README.md` in the scenario folder: what is asked, the source, and known caveats.
5. Add a test in `tests/test_scenarios.py` that loads it.

## Pull requests

- Keep changes focused; one feature or fix per PR.
- Add or update tests, and update `CHANGELOG.md` under *Unreleased*.
- Keep the core dependency-free (standard library only). Optional extras are fine if justified.
- Never commit `.env`, cache files, API keys, or private or proprietary data.

## Commit style

Short imperative subject (`Add earnings scenario`, `Fix Brier for abstentions`), with a body if needed.

By contributing you agree your work is licensed under Apache-2.0, and you follow our
[Code of Conduct](CODE_OF_CONDUCT.md).
