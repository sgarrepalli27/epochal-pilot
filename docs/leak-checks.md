# Leak checks

The as-of context cannot contain the answer, but a model trained after the event may already know
it. Live forecasts (`--mode live`) sidestep that problem, because the outcome does not exist yet.
For backtests, use the checks below. All of them are 0 Epochal requests when you use
`--context public` or `--no-context`.

The short table also lives in the README ([Leak checks](../README.md#leak-checks)).

| Check | Flag | What it does |
|---|---|---|
| Blind | `--no-context` | Same question with the as-of date and current policy range only. No documents, derived facts, or rule analysis. 0 Epochal requests. Overrides `--context`. A confidently correct blind answer is a strong sign of memorisation. |
| Date masking | `--mask-dates` | Keeps the documents but rewrites calendar years relative to T (`Year 0`, `Year -1`, …). Use this when a cutoff is unpublished (Jev). |
| Pre/post cutoff | report split; `--cutoff MODEL=YYYY-MM` | Accuracy is split at the model's training cutoff ([models.json](../src/epochal_pilot/models.json)). Accuracy that holds after the cutoff is skill; a collapse suggests memory. |
| Gated vs ungated | `--modes gated,ungated` | Compare answers with as-of context vs memory only. Prefer `--no-context` for a clean blind run. |

## Old cutoff vs new cutoff

Run two kinds of model on the same events with the same context:

- An **old-cutoff** model can only forecast. Its score is skill.
- A **new-cutoff** model can forecast *or* remember. If it beats the old model by a lot on events
  it was trained on, and the gap closes on events after its cutoff, that gap is memory (leakage).

`epochal-pilot compare` gives a paired test on the shared events, so you can tell a real gap from
noise. The `future-leak` scenario adds an `ungated` mode (no context at all) for a direct "does it
already know?" check.

```bash
epochal-pilot run fed-quarterly --only fq_2022q1 --context public --backend ollama --model qwen2.5:3b --out runs/fq-public
epochal-pilot run fed-quarterly --only fq_2022q1 --no-context --backend ollama --model qwen2.5:3b --out runs/fq-blind
epochal-pilot compare runs/fq-public runs/fq-blind

# TypeSafe Jev / official jev-rules pipeline (0 Epochal requests; needs TYPESAFE_API_KEY)
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --out runs/fq-jev
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --no-context --out runs/fq-jev-blind
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev --mask-dates --out runs/fq-jev-mask
epochal-pilot run fed-quarterly --only fq_2022q1 --backend jev-rules --out runs/fq-jev-rules
```
