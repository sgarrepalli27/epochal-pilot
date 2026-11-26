# epochal-pilot

**Backtest LLM agents against the web as it was on a past date.**

[![CI](https://github.com/sgarrepalli27/epochal-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgarrepalli27/epochal-pilot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

You can't fairly backtest an agent that browses the web: today's web already contains the answer.
`epochal-pilot` freezes the web at a chosen date with the [Epochal Time Portal](https://epochal.com)
(a date-gated web archive), lets a model research and forecast from that snapshot only, then scores
the forecast against what actually happened.

- **Scenarios as plugins.** A folder with an events file, a prompt template and a scoring config.
- **Proper forecasting scores.** Accuracy, multi-class Brier score, and model-free baselines.
- **Leak checks built in.** Results dated after the as-of moment are dropped and counted; scores are
  split by each model's training cutoff; answer options are shuffled to remove position bias.
- **Cheap to rerun.** Every API response is cached on disk, and a persistent budget guard caps Epochal spend.
- **Zero dependencies.** Python 3.10+ standard library only.

## Quickstart

```bash
git clone https://github.com/sgarrepalli27/epochal-pilot.git && cd epochal-pilot
pip install -e .
epochal-pilot run fed-rates --dry-run      # offline: fixtures + mock model, no keys, no spend
cp .env.example .env                       # add EPOCHAL_API_KEY plus OPENROUTER_API_KEY or ANTHROPIC_API_KEY
epochal-pilot run fed-rates --estimate     # how many Epochal requests a real run needs
epochal-pilot run fed-rates --model openai/gpt-4.1-mini --limit 3
```

`epilot` is a short alias for `epochal-pilot`, and `python -m epochal_pilot` works too.
Step-by-step instructions for Windows are in [Replicate on Windows](#replicate-on-windows).

## Backends

| `--backend` | Key | Default model | Where it runs |
|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | `openai/gpt-4.1-mini` | Cloud API; one key for hundreds of models |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` | Cloud API (Claude is **not** local) |
| `ollama` | none | `llama3.2:3b` | **Local** on your machine via [Ollama](https://ollama.com) |

Every backend needs `EPOCHAL_API_KEY` for gated runs (the frozen web). Claude models are reached
through Anthropic's API; [Ollama](https://ollama.com) is the option for running a model locally.
Model ids: [OpenRouter models](https://openrouter.ai/models),
[Claude models](https://docs.anthropic.com/en/docs/about-claude/models/overview), `ollama list`.

Note: current Claude models have training cutoffs after every event in the shipped scenarios, so
their scores are mostly a leak probe, not a fair forecast test. See [Caveats](#caveats).

## Replicate on Windows

Works on Windows 11, x64 or ARM64 (e.g. Surface Pro with Snapdragon X), in PowerShell with Python 3.10+.
CI runs the test suite on `windows-latest` and `windows-11-arm`.

**1. Install Python and Git** (skip what you already have)

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
# Close and reopen PowerShell, then check:
py --version
git --version
```

Or use the installers from [python.org](https://www.python.org/downloads/windows/) (pick the ARM64
installer on Snapdragon devices) and [git-scm.com](https://git-scm.com/download/win).

**2. Clone and create a virtual environment**

```powershell
cd $HOME
git clone https://github.com/sgarrepalli27/epochal-pilot.git
cd epochal-pilot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell says running scripts is disabled, allow it for your user once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

(Or, for the current window only: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.)

**3. Install**

```powershell
python -m pip install --upgrade pip
pip install -e .
```

**4. Add your keys**

```powershell
copy .env.example .env
notepad .env
```

Fill in `EPOCHAL_API_KEY`, then **one** of `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` (or neither
if you use Ollama). Save and close. `.env` is gitignored; never commit it.

**5. Check the setup and try it offline**

```powershell
epochal-pilot check                        # shows which keys are set, never their values
epochal-pilot list                         # available scenarios
epochal-pilot run fed-rates --dry-run      # offline: fixtures + mock model, no spend
```

**6. See what a real run costs, before spending anything**

```powershell
epochal-pilot run fed-rates --estimate
epochal-pilot run future-leak --estimate
```

Epochal's free tier is **100 requests**. A gated event usually costs 1 request (1 search; up to
`--max-fetches` extra fetches for thin results). The response cache (`.epochal-pilot-cache\`) is
**not in git**, so a fresh clone starts with an empty cache and pays for every event once; reruns are free.
Per run, `--budget` (default 10) caps new requests; `--max-total` (default 90) caps the lifetime
total for this cache.

**7. Real runs**

```powershell
# fed-rates: 13 FOMC meetings (~13 requests for all; start with a few)
epochal-pilot run fed-rates --backend openrouter --model openai/gpt-4.1-mini --limit 3
epochal-pilot run fed-rates --backend anthropic --model claude-haiku-5-5 --limit 3
epochal-pilot run fed-rates --backend ollama --model qwen2.5:3b --limit 3

# All 13 meetings (needs --budget 13 or more on a fresh cache)
epochal-pilot run fed-rates --model openai/gpt-4.1-mini --budget 15

# future-leak: 20 events, asked with and without the frozen web (~20 requests)
epochal-pilot run future-leak --model openai/gpt-4.1-mini --budget 22
epochal-pilot run future-leak --backend anthropic --budget 22
```

Requests cached by one run are reused by the next, so compare several models on the same events
for one Epochal cost: `--model openai/gpt-4.1-mini --model google/gemini-2.5-flash`.

For local models, install [Ollama for Windows](https://ollama.com/download), then
`ollama pull qwen2.5:3b`. Small 1–3B models suit a 16 GB laptop; close other apps if memory is tight.

**8. Results**

Each run writes `runs\<scenario>-<date>-<time>\`:

| File | Contents |
|---|---|
| `summary.md` | Score table to read or paste |
| `scores.csv` | One row per model and mode, plus baselines (opens in Excel) |
| `predictions.csv` | One row per event: answer, probabilities, cited URLs, rationale |
| `run.json` | Settings used (never keys) |

Use `--out runs\my-test` to choose the folder.

**9. Configure a run**

| Want to | Flag | Example |
|---|---|---|
| Forecast further ahead | `--lead-days N` | `--lead-days 28` (default 7) |
| Run some events only | `--only ids` / `--limit N` | `--only fomc_2024_09,fomc_2024_11` |
| Cap Epochal spend | `--budget N`, `--max-total N` | `--budget 5` |
| Compare models | repeat `--model` | `--model a --model b` |
| Set a model's training cutoff | `--cutoff MODEL=YYYY-MM` | `--cutoff qwen2.5:3b=2023-12` |
| Ask with or without the frozen web | `--modes gated,ungated` | `--modes gated` |
| Change answer shuffling | `--seed N`, `--no-shuffle` | `--seed 1` |
| Let the model write extra searches | `--plan-queries K` | `--plan-queries 2` (costs K more requests per event) |

Event ids are the `id` column of `scenarios\<name>\events.csv`. Run `epochal-pilot run --help` for all flags.
Defaults for a scenario (lead days, options, baselines) live in `scenarios\<name>\scenario.json`.

**10. Add your own scenario**

```powershell
Copy-Item -Recurse scenarios\fed-rates scenarios\my-scenario
notepad scenarios\my-scenario\events.csv       # your events, outcomes and source URLs
notepad scenarios\my-scenario\scenario.json    # description, choices, lead_days, baselines
epochal-pilot run my-scenario --dry-run
epochal-pilot run my-scenario --estimate
```

See [Add a scenario](#add-a-scenario) and [docs/adding-a-scenario.md](docs/adding-a-scenario.md).

### macOS and Linux

The same steps, with these differences:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && ${EDITOR:-nano} .env
epochal-pilot check && epochal-pilot run fed-rates --dry-run && epochal-pilot run fed-rates --estimate
```

## Scenarios

| Scenario | Events | What it asks | Modes |
|---|---|---|---|
| [`fed-rates`](scenarios/fed-rates) | 13 FOMC meetings, Jan 2024 – Jul 2025 | Hold / cut 25 / cut 50 / cut 75 / hike 25, from the web N days before (default 7) | gated |
| [`future-leak`](scenarios/future-leak) | 20 resolved 2024–25 events (elections, Fed, launches, earnings) | Does the model already know the outcome? | gated + ungated |

Every outcome links to a public source (Federal Reserve press releases for `fed-rates`).

## How it works

```
event (date, question, options)
   │  as_of = event_date − lead_days
   ▼
Epochal search/fetch  ── header Accept-Datetime: as_of ──►  web as it was on that day
   │  drop any result dated after as_of (counted as docs_dropped_after_as_of)
   ▼
prompt: "Today is <as_of>. Use only this context." + shuffled options
   ▼
model ──► {"answer": "C", "probabilities": {...}, "citations": [...], "rationale": "..."}
   │  no option picked → one forced-choice retry → else abstain (scored wrong, uniform probs)
   ▼
score vs real outcome: accuracy, Brier, baselines, pre/post training-cutoff split
```

**Modes.** `gated` gives the model only the as-of context. `ungated` gives no context and asks from
memory; comparing the two shows how much the model already knows.

**Outputs** go to `runs/<scenario>-<time>/`: `predictions.csv` (one row per model × event × mode,
with probabilities and citations), `scores.csv`, `summary.md`, and `run.json` (config, never keys).

### Scores

| Column | Meaning |
|---|---|
| `accuracy` | Share of events where the top answer was right (abstentions count as wrong) |
| `brier` | Multi-class Brier score, Σ(p − outcome)². 0 = perfect, 2 = confidently wrong; uniform guessing over k options scores 1 − 1/k |
| `chance` | Mean of 1/k |
| `acc_pre_cutoff` / `acc_post_cutoff` | Accuracy on events before / after the model's stated training cutoff ([`models.json`](src/epochal_pilot/models.json), override with `--cutoff MODEL=YYYY-MM`) |
| `leak_score_upper_bound` | Gated accuracy minus chance |
| `gap_ungated_minus_gated` | Memory-only accuracy minus gated accuracy |
| `always_fav_letter_acc` | Accuracy of always picking the model's favourite option letter (position-bias check) |
| `citations_in_context` | Share of cited URLs that were really in the as-of context |
| `baseline:*` | `no-change`, `persistence` (same as last time), `majority` (hindsight), `uniform` |

### Budget and cache

| Control | Default | Notes |
|---|---|---|
| `--budget N` | 10 | Max **new** Epochal requests this run; the run stops cleanly and scores what it has |
| `--max-total N` | 90 | Lifetime cap stored in `.epochal-pilot-cache/live/budget.json` (Epochal's free tier is 100) |
| `--estimate` | | Prints cached vs new requests and exits |
| `--dry-run` | | Fixtures and a mock model; separate cache, no keys, no network |

A gated event costs 1 search plus up to `--max-fetches` (default 2) page fetches for results with
little text; in practice search returns page text, so most events cost 1 request. Cached requests
are free, so rerunning with new models or prompts costs nothing on Epochal.

## Add a scenario

```
scenarios/my-scenario/
  scenario.json       # config
  events.csv          # one row per event
  prompt_gated.md     # template ($today, $context, $question, $options, $response_format, any CSV column)
  prompt_ungated.md   # optional
```

`events.csv` needs `id, event_date, question, outcome, source_url`, plus `choices` (pipe-separated)
unless `scenario.json` sets shared `choices`. Optional: `search_query` (defaults to the question),
`category`, and any extra columns, which become `$column` variables in the prompt.

```json
{
  "description": "One line shown by `epochal-pilot list`.",
  "choices": ["Yes", "No"],
  "lead_days": 7,
  "modes": ["gated"],
  "shuffle": true,
  "baselines": {"constant": {"always-no": "No"}, "majority": true, "uniform": true}
}
```

See [docs/adding-a-scenario.md](docs/adding-a-scenario.md) for the full reference.

## Caveats

- **The leak score is an upper bound.** A week-ahead web legitimately predicts many events: Fed moves
  are usually priced in, polls lead elections, product prices leak. Compare pre- vs post-cutoff
  accuracy and the baselines before calling anything leakage.
- **Stated cutoffs are claims.** Vendors' training cutoffs can be vague or understated.
- **Newer models saw the outcomes.** Models trained after an event (for example current Claude models,
  cutoff Jun 2026) can't be fairly backtested on it; use them to measure leakage, and use models with
  earlier cutoffs, or events after a model's cutoff, for honest forecasting scores.
- **The archive is not the whole web.** Epochal coverage varies by site and date; undated results
  are kept and counted.
- **Small samples.** 13 or 20 events give wide error bars. Treat results as a probe, not a benchmark verdict.
- **Not investment advice.** `fed-rates` is a research benchmark. Nothing here is a recommendation to trade.

## Security

Keys are read from the environment or `.env` (gitignored) and are never printed, logged or written
to outputs. See [SECURITY.md](SECURITY.md).

## Contributing

Issues and pull requests are welcome, especially new scenarios. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE). Not affiliated with Epochal, OpenRouter, Anthropic, Ollama or the Federal Reserve.
