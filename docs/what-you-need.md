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
