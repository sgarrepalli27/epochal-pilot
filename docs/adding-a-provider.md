# Adding a System One provider

A System One predictor is a class with `predict(state, question, options)` that returns a
distribution over `options` plus metadata. Built-in official Decisions / judgment backends:

- `jev` / `jev-rules` — TypeSafe Jev (`POST https://api.typesafe.ai/v1/systemone`, model [`jev-latest`](https://docs.typesafe.ai/models)). Tested live.
- `openai-decisions` — [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions) (`POST https://api.openai.com/v1/decisions`, model [`gpt-6-luna`](https://developers.openai.com/api/docs/guides/decisions)). Tested only against mocked/simulated responses, not yet run live.
- `gemini-decisions` — [Gemini Decisions API](https://aistudio.google.com/docs/decisions-api) (`POST https://generativelanguage.googleapis.com/v1beta/decisions`, model [`gemini-decision-1`](https://aistudio.google.com/docs/models/gemini-decision-1)). Tested only against mocked/simulated responses, not yet run live.
- `rules` — deterministic checklist (no model)

`openai` and `gemini` are schema-locked LLM fallbacks, labeled **LLM as System One (not
calibration-trained)**. They are not official Decisions APIs.

System Two backends (`openrouter`, `anthropic`, `ollama`, `claude-cli`) are for analysis and
optional `--analyst` rationales, not the recommended default predictor.

## The plugin interface

```python
from epochal_pilot.providers import Predictor, PredictResult, register

@register("myco")
class MyPredictor(Predictor):
    name = "myco"
    role = "system-one"
    label = "MyCo System One"

    def __init__(self, api_key: str | None, model: str, *, cache=None, transport=None, **kwargs):
        self.api_key = api_key
        self.model = model
        self.cache = cache
        self.transport = transport

    def predict(self, state, question, options) -> PredictResult:
        # Return a probability for every option. Never put secrets in metadata, rationale, or repr.
        probs = {opt: 1.0 / len(options) for opt in options}
        return PredictResult(
            probabilities=probs,
            answer=options[0],
            metadata={"provider": "myco", "model": self.model},
        )
```

Put that module on `PYTHONPATH` and set:

```
EPOCHAL_PILOT_PROVIDER_MODULE=my_package.providers
```

Then run with `--backend myco`. The registry is a stdlib `name → class` map (`register` /
`get_predictor_class`); there is no setuptools entry-point scan.

## Conventions

- Reuse `epochal_pilot.net.post_json`. Never include request headers or API keys in raised errors
  or URLs (Gemini-style keys go in `x-goog-api-key`, not the query string).
- Cache by request **body** under a namespace (see `Cache.key`); never cache the key.
- `__repr__` must show `api_key=set` or `missing`, never the value.
- Tests should mock HTTP. Do not require a live key in CI.
- For a Decisions-style API, prefer the official request shape (typed questions, shared input)
  over prompting a chat model for JSON.

Official `--backend` values stay backward compatible. If you add a new name, also document it in
the README System One table and in `.env.example`.
