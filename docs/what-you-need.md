# What you need and why

Short answers first. Setup steps for each option are at the end.

## Why Epochal?

To test forecasting, the model must see only what was known **before** the event. Today's web already
reports the outcome, so a model that searches it is reading the answer, not forecasting.

Epochal's Time Portal returns the web as it was at a moment you choose (the `Accept-Datetime`
header). `epochal-pilot` asks for the web as of, say, 7 days before an FOMC meeting, and also drops
anything whose own publish date is later. The model forecasts from that frozen snapshot.

You get 100 free requests per account. You don't need any of them to start: the `fed-rates` scenario
ships a public-domain context pack (Fed statements and minutes, BLS CPI and jobs releases), each
dated by its public release. `--context public` uses it and costs 0 requests.

## Why a model at all?

The model **is the thing under test**. It reads the as-of context and returns a probability for each
option. `epochal-pilot` scores those probabilities against what happened: accuracy, Brier, log loss,
calibration, and skill against simple baselines like "no change".

## Which model?

It depends on the question you're asking. A model's **training cutoff** decides what it can answer honestly.

| Option | Cost | Privacy | Training cutoff vs events | Good for | Speed | Notes |
|---|---|---|---|---|---|---|
| **Local, Ollama** (`--backend ollama`, e.g. `qwen2.5:3b`) | Free | Stays on your PC | Older (2023-24) → most events are after it | **Forecasting test**: the model can't have memorised outcomes | Slow on CPU (often tens of seconds per event) | Windows ARM64 (Snapdragon) is supported by Ollama; 1-3B models fit 16 GB |
| **Claude via Claude Code** (`--backend claude-cli`) | Uses your Claude subscription | Sent to Anthropic | Recent (after most events) | **Leak detection**: does it already know? | Seconds | No key; tools disabled so it can't search the answer |
| **Anthropic API** (`--backend anthropic`) | Pay per token, cents per run | Sent to Anthropic | Recent | Leak detection; scripted runs | Seconds | Needs `ANTHROPIC_API_KEY` |
| **OpenRouter** (`--backend openrouter`) | Cents per run | Sent to the model's provider | Mixed: pick old or new | Both; many models with one key | Seconds | Needs `OPENROUTER_API_KEY`; `gpt-4.1-mini` cutoff 2024-06, `gemini-2.5-flash` 2025-01 |
| **TypeSafe Jev** (`--backend jev` or `jev-rules`; recommended System One default) | $0.042 / 1M input tokens; output free (as of 2026-11-26; [models](https://docs.typesafe.ai/models)) | Sent to TypeSafe | Cutoff unpublished ([Jev 1.14](https://docs.typesafe.ai/models), Oct 2026) | Judgment over options; official `jev-rules` flow is code analysis then Choice | Seconds | Needs `TYPESAFE_API_KEY`; defaults to `--context public`. TypeSafe documents that Jev is [trained to return calibrated decisions](https://docs.typesafe.ai/models). **Tested live.** Use `--no-context` and `--mask-dates` to quantify leak, not a pre/post cutoff split. |
| **OpenAI Decisions** (`--backend openai-decisions`) | Uncached input $0.10 / 1M tokens; output not charged (as of 2026-11-26; [Decisions guide](https://developers.openai.com/api/docs/guides/decisions), [pricing](https://developers.openai.com/api/docs/pricing)) | Sent to OpenAI | Unpublished ([`gpt-6-luna`](https://developers.openai.com/api/docs/guides/decisions)) | Official Decisions API as a System One predictor | Seconds | Needs `OPENAI_API_KEY`. Official docs describe probability estimates and do not claim they are calibrated. **Tested only against mocked/simulated responses, not yet run live.** |
| **Gemini Decisions** (`--backend gemini-decisions`) | Paid input $0.10 / 1M tokens; output N/A; free tier free of charge (as of 2026-11-26; [pricing](https://aistudio.google.com/docs/pricing), [model page](https://aistudio.google.com/docs/models/gemini-decision-1)) | Sent to Google | Unpublished ([`gemini-decision-1`](https://aistudio.google.com/docs/models/gemini-decision-1)) | Official Decisions API as a System One predictor | Seconds | Needs `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). Official docs state that the API [returns calibrated probabilities](https://aistudio.google.com/docs/decisions-api). **Tested only against mocked/simulated responses, not yet run live.** |
| **Rules checklist** (`--backend rules`; default when no TypeSafe key) | Free | Stays on your PC | n/a (no model) | A no-model baseline from the same code signals | Instant | No key; the checklist was written with 2016–2026 hindsight |

Cutoffs used for the pre/post split are in [`models.json`](../src/epochal_pilot/models.json). For any
other model, pass `--cutoff MODEL=YYYY-MM`.

### The core signal: old cutoff vs new cutoff

Run **two kinds of model on the same events with the same context**:

- An **old-cutoff** model can only forecast. Its score is skill.
- A **new-cutoff** model can forecast *or* remember. If it beats the old model by a lot on events
  it was trained on, and the gap closes on events after its cutoff, that gap is memory (leakage).

`epochal-pilot compare` gives a paired test on the shared events, so you can tell a real gap from noise.
The `future-leak` scenario adds an `ungated` mode (no context at all) for a direct "does it already
know?" check.

## Leak check

Two complementary tests, both 0 Epochal requests when you use the public pack or go blind:

1. **With context vs `--no-context` (blind).** Run the same events twice. With `--context public` the
   model sees documents released before T (and, for `fed-quarterly`, a derived SEP-gap line). With
   `--no-context` it gets only the as-of date and the current policy range — no documents, no derived
   facts, and no rule analysis. A confidently correct blind answer is a strong sign of memorisation.
2. **`--mask-dates`.** Same documents, but calendar years are rewritten relative to T (`Year 0`,
   `Year -1`, …). Use this when a cutoff is unpublished (Jev).
3. **Pre- vs post-cutoff.** The report splits accuracy at the model's training cutoff
   (`--cutoff MODEL=YYYY-MM`). Accuracy that holds after the cutoff is skill; a collapse after the
   cutoff is memory. Compare an old-cutoff model with a recent one on the same events.

```bash
epochal-pilot run fed-quarterly --only fq_2022q1 --context public --backend ollama --model qwen2.5:3b --out runs/fq-public
epochal-pilot run fed-quarterly --only fq_2022q1 --no-context --backend ollama --model qwen2.5:3b --out runs/fq-blind
epochal-pilot compare runs/fq-public runs/fq-blind

# TypeSafe Jev / official jev-rules pipeline (0 Epochal requests; needs TYPESAFE_API_KEY)
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --out runs/fq-jev
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --no-context --out runs/fq-jev-blind
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --mask-dates --out runs/fq-jev-mask
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev-rules --out runs/fq-jev-rules
epochal-pilot run fed-quarterly --only fq_2022q1 --backend openai-decisions --out runs/fq-openai-decisions
epochal-pilot run fed-quarterly --only fq_2022q1 --backend gemini-decisions --out runs/fq-gemini-decisions
epochal-pilot run fed-quarterly --only fq_2022q1 --backend rules --out runs/fq-rules
```

Live forecasts (`--mode live`) cannot have been memorised, because the outcome does not exist yet.
See also [Leak checks](../README.md#leak-checks) in the README and [leak-checks.md](leak-checks.md). For `--context public` vs `--context epochal`, see the [README comparison](../README.md#where-the-data-comes-from---context-public-vs---context-epochal).

## Minimum setup per option

All options start the same:

```bash
git clone https://github.com/sgarrepalli27/epochal-pilot.git && cd epochal-pilot
pip install -e .
cp .env.example .env        # Windows: copy .env.example .env
```

| Option | Install | Put in `.env` | First command (0 Epochal requests) |
|---|---|---|---|
| Nothing yet | — | — | `epochal-pilot run fed-rates --dry-run --context public` |
| Ollama | [ollama.com/download](https://ollama.com/download), then `ollama pull qwen2.5:3b` | — (optional `OLLAMA_NUM_CTX=8192` on small PCs) | `epochal-pilot run fed-rates --context public --backend ollama --model qwen2.5:3b` |
| Claude Code | [code.claude.com](https://code.claude.com); run `claude` once to log in | — (optional `CLAUDE_CLI_TIMEOUT=300`) | `epochal-pilot run fed-rates --context public --backend claude-cli --cutoff default=2026-06` |
| Anthropic API | — | `ANTHROPIC_API_KEY=` | `epochal-pilot run fed-rates --context public --backend anthropic` |
| OpenRouter | — | `OPENROUTER_API_KEY=` | `epochal-pilot run fed-rates --context public --model openai/gpt-4.1-mini` |
| TypeSafe Jev | — | `TYPESAFE_API_KEY=` | `epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev-rules` |
| OpenAI Decisions | — | `OPENAI_API_KEY=` | `epochal-pilot run fed-quarterly --only fq_2022q1 --backend openai-decisions` |
| Gemini Decisions | — | `GEMINI_API_KEY=` | `epochal-pilot run fed-quarterly --only fq_2022q1 --backend gemini-decisions` |
| Rules checklist | — | — | `epochal-pilot run fed-quarterly --only fq_2022q1 --backend rules` |
| Epochal (frozen web) | — | `EPOCHAL_API_KEY=` (US only) | `epochal-pilot plan fed-rates --requests 25`, then `run ... --estimate` |

`epochal-pilot check` shows which keys are set (never their values) and whether `claude` is on your PATH.

## The 0-request path, in order

1. `epochal-pilot validate fed-rates`: checks the scenario and that every public document predates its as-of date.
2. `epochal-pilot run fed-rates --dry-run --context public`: the whole pipeline with a mock model.
3. `epochal-pilot run fed-rates --context public --backend ollama --model qwen2.5:3b` (or `claude-cli`):
   real forecasts from official documents.
4. `epochal-pilot report runs/<folder>`: open `report.html`.
5. `epochal-pilot plan fed-rates --requests 25`: decide where your Epochal requests go.

Only then add `EPOCHAL_API_KEY` and run with the frozen web. Compare the two runs with
`epochal-pilot compare` to see what the web adds over official documents alone.

## fed-quarterly on a tight budget

`fed-quarterly` forecasts the Fed's target range at each quarter-end, 2016-2026, from what was public
6 weeks earlier. Its public pack covers every as-of date, so the whole 10-year backtest is free.

| Step | Command | Epochal requests |
|---|---|---|
| Full backtest, 43 quarters, official first-release data | `epochal-pilot run fed-quarterly --context public --backend ollama --model qwen2.5:3b` | 0 |
| Same with Claude Code (a leak probe: Claude was trained after these dates) | `epochal-pilot run fed-quarterly --context public --backend claude-cli --cutoff default=2026-06` | 0 |
| Plan the turning points | `epochal-pilot plan fed-quarterly --requests 10 --only turning-point` | 0 |
| The 10 turning points with the as-of web | `epochal-pilot run fed-quarterly --only turning-point --budget 10` | 10 (cached after) |
| Live forecast for 31 Dec 2026 | `epochal-pilot run fed-quarterly --mode live --context public` | 0 (1 with `--context epochal`) |
| Score the live forecast in January | `epochal-pilot score --latest` | 0 |

Why both a local model and Claude: a local model with a 2023 cutoff is a fair forecaster for 2024-2026
quarters; Claude (cutoff mid-2026) has read most outcomes, so its backtest score is an upper bound on
memory. The live quarter is the one test where neither can remember the answer.
