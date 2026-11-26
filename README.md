# epochal-pilot

**Backtest LLM forecasters against the web as it was on a past date, and score them honestly.**

[![CI](https://github.com/sgarrepalli27/epochal-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgarrepalli27/epochal-pilot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

You can't fairly backtest an agent that browses the web: today's web already contains the answer.
`epochal-pilot` freezes the web at a chosen date with the [Epochal Time Portal](https://epochal.com),
lets a model forecast from that snapshot only, then scores the forecast against what happened, with
the statistics a small sample needs.

It is an **evaluation harness, not an API wrapper**: most of it works with zero Epochal requests.

- **Honest statistics.** Bootstrap 95% intervals, Brier skill vs the best non-hindsight baseline,
  log loss, calibration (reliability) tables, paired bootstrap and McNemar tests, small-sample warnings.
- **Two context sources.** The time-gated web (Epochal), or a shipped pack of public-domain Fed and
  BLS documents (`--context public`, 0 requests). Compare them on the same events.
- **Leak checks.** Results dated after the as-of moment are dropped; scores split by each model's
  training cutoff; options shuffled; question wording checked for answer leaks.
- **Reports.** Self-contained HTML (inline SVG, no JavaScript) and markdown, a cross-run leaderboard,
  and a metrics-only `--share` export that is safe to publish.
- **Request planner.** `plan --requests N` picks the events that carry the most signal per request.
- **Zero dependencies.** Python 3.10+ standard library only. Windows, macOS, Linux (x64 and ARM64).

## What you need and why

| You need | Why | Cost |
|---|---|---|
| **A model** (the forecaster under test) | It reads the as-of context and gives probabilities for each option. That is what gets scored. | Free (local) to cents |
| **Epochal key** (optional at first) | The web frozen before the event, so the model can't read the outcome. | 100 free requests |
| **Nothing else** | Public-context mode, reports, stats, planning and validation are offline. | 0 |

Pick the model by the question you're asking:

| Question | Use | Why |
|---|---|---|
| Can a model **forecast** from evidence? | A model whose training cutoff is **before** the events (local Ollama, `gpt-4.1-mini`) | It can't have memorised outcomes |
| Does a model **already know** the outcome (leak)? | A model trained **after** the events (Claude via `claude-cli` or `anthropic`) | Compare with the gated score and with old-cutoff models |
| Best signal | **Both**, on the same events | Old-cutoff vs new-cutoff on the same evidence separates skill from memory |

Setup per option, plus the 0-request path, is in **[docs/what-you-need.md](docs/what-you-need.md)**.

## Quickstart (0 Epochal requests, no keys)

```bash
git clone https://github.com/sgarrepalli27/epochal-pilot.git && cd epochal-pilot
pip install -e .
epochal-pilot validate fed-rates                          # schema, cutoffs, leak check of the public pack
epochal-pilot run fed-rates --dry-run --context public    # offline mock model + real public Fed/BLS docs
epochal-pilot report runs/<the folder it printed>         # HTML + markdown report
```

With a real model and still 0 Epochal requests:

```bash
epochal-pilot run fed-rates --context public --backend ollama --model qwen2.5:3b     # local, free
epochal-pilot run fed-rates --context public --backend claude-cli                    # your Claude Code login
```

`epilot` is a short alias for `epochal-pilot`; `python -m epochal_pilot` works too.
Windows steps are in [Replicate on Windows](#replicate-on-windows).

## Your 100 requests

Epochal's free tier is 100 search/fetch requests per account. Spend them last, and only where they add signal.

| Step | Command | Epochal requests |
|---|---|---|
| 1. Check setup | `epochal-pilot check` | 0 |
| 2. Validate scenarios | `epochal-pilot validate fed-rates` | 0 |
| 3. Try everything offline | `epochal-pilot run fed-rates --dry-run` | 0 |
| 4. Real model, public context | `epochal-pilot run fed-rates --context public --backend ollama --model qwen2.5:3b` | 0 |
| 5. Plan | `epochal-pilot plan fed-rates --requests 25 --model qwen2.5:3b --model openai/gpt-4.1-mini` | 0 |
| 6. Estimate | `epochal-pilot run fed-rates --estimate` | 0 |
| 7. Full fed-rates run, several models | `epochal-pilot run fed-rates --model openai/gpt-4.1-mini --model google/gemini-2.5-flash --budget 30` | ~23-30 |
| 8. Full future-leak run | `epochal-pilot run future-leak --budget 25` | ~20-25 |
| 9. Compare and report | `epochal-pilot compare runs/a runs/b` and `report <run> --share` | 0 |

That is a full, high-signal run for about **50 requests**, leaving about 50 for your own scenario
(`epochal-pilot new my-scenario`). Responses are cached, so rerunning with more models, prompts or
seeds costs nothing. Searches return 10 results for the price of one; only 2xx responses are counted
(failed requests are refunded locally, as Epochal doesn't bill them).

## Epochal terms: what you may share

Read the [Time Portal Product Terms](https://epochal.com/legal/time-portal-product-terms). In short:

- **Don't publish Results**: no cached responses, URLs, titles, snippets or page text from Epochal,
  and no evaluation datasets built from them (§3.3, §3.4). The cache (`.epochal-pilot-cache/`) and
  `runs/` are gitignored for this reason. Don't commit them or hand them to others.
- **Share metrics only.** `epochal-pilot report <run> --share` writes `share/` with scores, event
  ids, outcomes and the options models chose: no Epochal content, request ids or model rationales.
- **US only.** Epochal currently refuses requests from outside the United States.
- **Each person uses their own key.** A clone pays for its own requests; there is no shared cache.

The public-domain context pack is different: it's US-government work, shipped in this repo, and free to reuse.

## Commands

| Command | What it does | Requests |
|---|---|---|
| `list`, `check` | Scenarios; which keys are set (never values); whether `claude` is on PATH | 0 |
| `run <scenario>` | Run models on events; writes predictions, scores, stats, report | 0 with `--context public` or `--dry-run` |
| `run ... --estimate` | Exact new searches; fetches exact when a search is cached, else a range | 0 |
| `plan <scenario> --requests N` | Picks events and lead times for most signal per request; prints the `run` command | 0 |
| `report <run_dir> [--share]` | HTML + markdown report; `--share` adds a metrics-only folder | 0 |
| `compare <run_dir>...` | Leaderboard plus paired tests on shared events (e.g. public vs Epochal context) | 0 |
| `validate <scenario>` | Schema, dates vs model cutoffs, source URLs, wording, public-pack leak check | 0 |
| `new <name>` | Scaffold a scenario folder | 0 |

### Backends

| `--backend` | Needs | Default model | Notes |
|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | `openai/gpt-4.1-mini` | Hundreds of models; cents per run |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` | Claude API |
| `claude-cli` | [Claude Code](https://code.claude.com) installed and logged in | your Claude Code default | No key; uses your Claude subscription; **all tools disabled** |
| `ollama` | [Ollama](https://ollama.com) running | `llama3.2:3b` | Local, free, private |

**`claude-cli`.** Runs `claude -p --output-format json --max-turns 1 --no-session-persistence
--strict-mcp-config --disallowedTools "*"` with the prompt on stdin, from an empty temporary folder.
Every tool (WebSearch, WebFetch, Bash, file reads) and every MCP server is removed, so the model
can't look up the outcome: its answer must come from the prompt. Use `--model sonnet` (or any Claude
Code model name) to choose a model. Claude's training cutoff is after most shipped events, so this is a
**leak probe**, not a fair forecast test; pass `--cutoff default=YYYY-MM` (or `--cutoff sonnet=...`)
for the right pre/post split. `CLAUDE_CLI_TIMEOUT` (seconds, default 300) sets the timeout.

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

**3. Install**

```powershell
python -m pip install --upgrade pip
pip install -e .
```

**4. Zero-request steps first**

```powershell
epochal-pilot check
epochal-pilot validate fed-rates
epochal-pilot run fed-rates --dry-run --context public --out runs\pub-dry
epochal-pilot report runs\pub-dry
start runs\pub-dry\report.html
```

**5. Add keys (only the ones you need)**

```powershell
copy .env.example .env
notepad .env
```

`EPOCHAL_API_KEY` for the frozen web; `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` for cloud models;
nothing for `ollama` or `claude-cli`. `.env` is gitignored; never commit it.

**6. Local or Claude Code models, still 0 Epochal requests**

```powershell
# Ollama: install from https://ollama.com/download, then
ollama pull qwen2.5:3b
epochal-pilot run fed-rates --context public --backend ollama --model qwen2.5:3b

# Claude Code: install from https://code.claude.com, run `claude` once to log in, then
epochal-pilot run fed-rates --context public --backend claude-cli --cutoff default=2026-06
```

Small 1-3B models suit a 16 GB laptop; close other apps if memory is tight, and set
`OLLAMA_NUM_CTX=8192` in `.env` if Ollama runs out of memory.

**7. Spend Epochal requests**

```powershell
epochal-pilot plan fed-rates --requests 25 --model openai/gpt-4.1-mini
epochal-pilot run fed-rates --estimate
epochal-pilot run fed-rates --model openai/gpt-4.1-mini --model google/gemini-2.5-flash --budget 30 --out runs\fed-epochal
epochal-pilot run fed-rates --context public --model openai/gpt-4.1-mini --model google/gemini-2.5-flash --out runs\fed-public
epochal-pilot compare runs\fed-public runs\fed-epochal --out runs\compare
```

### macOS and Linux

The same steps with `python3 -m venv .venv && source .venv/bin/activate`, `cp .env.example .env`,
and forward slashes in paths.

## Outputs

Each run writes `runs/<scenario>-<context>-<time>/` (choose with `--out`):

| File | Contents | Shareable |
|---|---|---|
| `report.html`, `summary.md` | Scores with CIs, paired tests, reliability chart, per-event table, cost ledger | Metrics only, but regenerate with `--share` to be safe |
| `scores.csv` | One row per forecaster, with CIs, skill, calibration, pre/post-cutoff split | Yes |
| `stats.json` | Calibration bins, reference baseline, paired tests | Yes |
| `predictions.csv` | Per event: answer, probabilities, **cited URLs, rationale** | **No** (Epochal content) |
| `baseline_predictions.csv` | Baseline probabilities per event | Yes |
| `run.json` | Manifest: git SHA, version, events/prompt hashes, model + params, seed, search settings, Epochal ledger with request/copy ids | Local; `share/run.json` drops ids |
| `share/` | Written by `report --share`: metrics-only copies | Yes |

## Interpreting results

- **Look at intervals first.** With 23 events, one event moves accuracy by about 4 points. Overlapping
  intervals mean "can't tell".
- **Skill, not accuracy.** `brier_skill` (BSS) compares with the best *non-hindsight* baseline on the
  same events (`no-change`, `persistence`, `uniform`, or your own `--baseline-csv`). BSS > 0 with an
  interval above 0 is real skill. `majority(hindsight)` uses the outcomes being scored: context only.
- **Paired tests for differences.** `p vs ref`, and the gated-vs-ungated and run-vs-run tables, test
  differences on the same events (bootstrap of Brier differences, exact McNemar on right/wrong).
- **Calibration.** In the reliability chart, points on the diagonal are well calibrated; below it is
  overconfident. ECE summarises the gap.
- **Leakage.** High accuracy *before* the cutoff with a drop *after* it, or ungated accuracy close to
  gated accuracy, points to memorised outcomes. Accuracy that holds up after the cutoff is skill.
- **Public vs Epochal context.** `compare` on the same events shows what the time-gated web adds over
  official documents alone.

| Column | Meaning |
|---|---|
| `accuracy` [`_ci_low`, `_ci_high`] | Share of events where the top answer was right; 95% bootstrap CI |
| `brier` | Multi-class Brier, Σ(p − outcome)²: 0 perfect, 2 confidently wrong; uniform over k options = 1 − 1/k |
| `log_loss` | −ln p(outcome), floored at 1e-6 (a confident miss costs ~13.8) |
| `brier_skill`, `reference` | 1 − Brier / reference Brier, vs the best non-hindsight baseline |
| `p_vs_reference` | Paired bootstrap p-value, model vs reference Brier |
| `ece` | Expected calibration error over 5 confidence bins |
| `acc_pre_cutoff` / `acc_post_cutoff` | Split at the model's training cutoff ([models.json](src/epochal_pilot/models.json); `--cutoff MODEL=YYYY-MM`) |
| `leak_score_upper_bound` | Gated accuracy minus chance (an upper bound, not proof) |
| `always_fav_letter_acc` | Accuracy of always picking the favourite option letter (position bias) |
| `small_n` | 1 when n < 30 |

## Scenarios

| Scenario | Events | Question | Context packs |
|---|---|---|---|
| [`fed-rates`](scenarios/fed-rates) | 23 FOMC meetings, Jan 2024 – Oct 2026 (10 from Sep 2025, incl. the Sep 2026 hike) | Hold / cut 25 / cut 50 / cut 75 / hike 25 | Epochal, public |
| [`future-leak`](scenarios/future-leak) | 20 resolved 2024–25 events (elections, Fed, launches, earnings) | Does the model already know the outcome? | Epochal (+ ungated) |

Every outcome links to its public source. The `fed-rates` public pack holds 114 trimmed documents
(FOMC statements and minutes, BLS CPI and jobs releases), each dated by public release; a document is
used only if released strictly before the as-of date ([details](scenarios/fed-rates/README.md)).

**Market baseline.** Not shipped: Kalshi's and Polymarket's terms don't allow redistributing their
price histories. If you have data you're allowed to use, pass it with `--baseline-csv my.csv`
(columns `name,event,<one column per option>`); it becomes a non-hindsight skill reference.

## How it works

```
event (date, question, options)
   │  as_of = event_date − lead_days
   ▼
context: Epochal search/fetch (Accept-Datetime: as_of)  or  public pack (docs released < as_of)
   │  drop anything dated after as_of (search published_at, then the fetched copy's published_at)
   ▼
prompt: "Today is <as_of>. Use only this context." + shuffled options
   ▼
model ──► {"answer": "C", "probabilities": {...}, "citations": [...], "rationale": "..."}
   │  no option picked → one forced-choice retry → else abstain (scored wrong, uniform probs)
   ▼
score: accuracy, Brier, log loss + CIs, skill vs baselines, calibration, paired tests, cutoff split
```

### Budget and retrieval

| Setting | Default | Where |
|---|---|---|
| `num_results` | 10 (API max, same cost as 1) | `scenario.json`, `--num-results` |
| `published_after_days` | 30 (search window before as-of); `null` for none | `scenario.json` |
| `categories` | all (`news`, `finance` = SEC filings, ...) | `scenario.json` |
| `domains` / `exclude_domains` | none | `scenario.json` |
| `max_fetches` | 2 per event, only for results with little text | `scenario.json`, `--max-fetches` |
| `--budget N` | 10 new requests per run | CLI |
| `--max-total N` | 90 lifetime for this cache | CLI, `EPOCHAL_PILOT_MAX_TOTAL` |

## Add a scenario

```bash
epochal-pilot new my-scenario          # scaffold
# edit scenarios/my-scenario/events.csv (future-tense questions, source_url per event)
epochal-pilot validate my-scenario
epochal-pilot run my-scenario --dry-run
```

See [docs/adding-a-scenario.md](docs/adding-a-scenario.md).

## Caveats

- **Small samples.** Tens of events give wide intervals. The tool says so; believe it.
- **Stated cutoffs are claims.** Vendors' training cutoffs can be vague or understated.
- **Newer models saw the outcomes.** Use them to measure leakage, not forecasting skill.
- **A week out, markets usually know.** Fed decisions are mostly priced in; try `--lead-days 28`.
- **The archive is not the whole web.** Epochal coverage varies; undated results are kept and counted.
- **Not investment advice.** This is a research benchmark.

## Security

Keys are read from the environment or `.env` (gitignored) and are never printed, logged or written
to outputs. See [SECURITY.md](SECURITY.md).

## Contributing

Issues and pull requests are welcome, especially new scenarios. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE). The public context pack is US-government work in the public domain
([Federal Reserve Board](https://www.federalreserve.gov/disclaimer.htm),
[BLS](https://www.bls.gov/opub/copyright-information.htm)). Not affiliated with Epochal, OpenRouter,
Anthropic, Ollama, the Federal Reserve or BLS.
