"""Chat backends: OpenRouter (default), Anthropic Claude API, and Ollama (local). Responses are cached."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from epochal_pilot.cache import Cache
from epochal_pilot.net import post_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def num_ctx() -> int:
    """Ollama context window. 10 search results need more than 8k tokens; lower it on small PCs."""
    try:
        return int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
    except ValueError:
        return 16384


Transport = Callable[[str, str], str]


class LLM:
    def __init__(
        self,
        cache: Cache,
        backend: str = "openrouter",
        api_key: str | None = None,
        transport: Transport | None = None,
        max_tokens: int = 600,
    ):
        self.cache, self.backend, self._key, self.max_tokens = cache, backend, api_key, max_tokens
        self.ollama_url = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
        backends = {
            "ollama": self._ollama,
            "anthropic": self._anthropic,
            "openrouter": self._openrouter,
            "claude-cli": self._claude_cli,
        }
        self.transport = transport or backends.get(backend, self._openrouter)

    def __repr__(self) -> str:
        return f"LLM(backend={self.backend}, key={'set' if self._key else 'not set'})"

    def params(self) -> dict:
        """Generation settings recorded in the run manifest."""
        if self.backend == "anthropic":
            return {"max_tokens": max(self.max_tokens, 4000), "temperature": "default (adaptive thinking)"}
        if self.backend == "ollama":
            return {"temperature": 0, "seed": 0, "num_ctx": num_ctx(), "num_predict": self.max_tokens}
        if self.backend == "claude-cli":
            return {"cli": "claude -p", "args": claude_cli_args("<model>")[1:], "timeout_s": claude_timeout()}
        if self.backend == "mock":
            return {"deterministic": True}
        return {"temperature": 0, "max_tokens": self.max_tokens}

    def _openrouter(self, model: str, prompt: str) -> str:
        r = post_json(
            OPENROUTER_URL,
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self.max_tokens,
            },
            headers={
                "Authorization": f"Bearer {self._key}",
                "X-Title": "epochal-pilot agent backtest harness",
            },
            timeout=180,
        )
        return (r.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    def _anthropic(self, model: str, prompt: str) -> str:
        # Current Claude models think adaptively; thinking uses output tokens, so allow headroom.
        r = post_json(
            ANTHROPIC_URL,
            {
                "model": model,
                "max_tokens": max(self.max_tokens, 4000),
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={"x-api-key": self._key or "", "anthropic-version": ANTHROPIC_VERSION},
            timeout=300,
        )
        blocks = r.get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")

    def _ollama(self, model: str, prompt: str) -> str:
        r = post_json(
            f"{self.ollama_url}/api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0, "seed": 0, "num_ctx": num_ctx(), "num_predict": self.max_tokens},
            },
            timeout=900,
        )
        return r.get("message", {}).get("content") or ""

    def _claude_cli(self, model: str, prompt: str) -> str:
        return run_claude_cli(model, prompt)

    def ask(self, model: str, prompt: str) -> str:
        key = Cache.key(self.backend, model, prompt, self.max_tokens)
        hit = self.cache.get("llm", key)
        if hit is not None:
            return hit["text"]
        text = self.transport(model, prompt)
        if text.strip():  # never cache an empty reply
            self.cache.put("llm", key, {"model": model, "text": text})
        return text


# ------------------------------------------------------------------------------------------ claude-cli
# Local Claude Code (https://code.claude.com/docs/en/cli-reference) in print mode, logged in with the
# user's own Claude account, so no API key is needed. Every tool is removed so the model cannot search
# the web (or read files) and pull in post-as-of information: the answer must come from the prompt.

CLAUDE_DEFAULT_MODEL = "default"  # "default" = whatever the user's Claude Code is configured to use


class ClaudeCLIError(RuntimeError):
    pass


def find_claude() -> str | None:
    """Path to the claude executable (claude.exe / claude.cmd on Windows), or None."""
    return shutil.which(os.environ.get("CLAUDE_CLI", "claude"))


def claude_timeout() -> int:
    try:
        return int(os.environ.get("CLAUDE_CLI_TIMEOUT", "300"))
    except ValueError:
        return 300


def claude_cli_args(model: str | None) -> list[str]:
    """Arguments after the executable. The prompt goes on stdin (no command-line length limits)."""
    args = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--strict-mcp-config",  # ignore every configured MCP server (none are passed)
    ]
    if model and model != CLAUDE_DEFAULT_MODEL:
        args += ["--model", model]
    # Last, because the flag takes a list: "*" removes every built-in and MCP tool (WebSearch, WebFetch,
    # Bash, Read, ...). Non-empty on purpose: an empty "" argument can be dropped by Windows .cmd shims.
    args += ["--disallowedTools", "*"]
    return args


def parse_claude_json(stdout: str) -> str:
    """The answer text from ``--output-format json`` (a result object, or a list of events)."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCLIError(f"claude returned non-JSON output: {stdout[:200]!r}") from e
    if isinstance(data, list):  # some versions print the whole event stream
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        data = results[-1] if results else {}
    if not isinstance(data, dict):
        raise ClaudeCLIError("claude JSON output has an unexpected shape")
    if data.get("is_error"):
        raise ClaudeCLIError(f"claude reported an error: {str(data.get('result') or data.get('subtype'))[:300]}")
    return str(data.get("result") or "")


def run_claude_cli(model: str, prompt: str, runner=None) -> str:
    runner = runner or subprocess.run
    exe = find_claude()
    if not exe:
        raise ClaudeCLIError(
            "claude (Claude Code) is not on PATH. Install it from https://code.claude.com and run `claude` "
            "once to log in, or pick another --backend."
        )
    args = [exe, *claude_cli_args(model)[1:]]
    # Run from an empty folder so no project CLAUDE.md, .mcp.json or hooks are picked up.
    with tempfile.TemporaryDirectory(prefix="epilot-claude-") as cwd:
        try:
            proc = runner(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=claude_timeout(),
                cwd=cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCLIError(f"claude timed out after {claude_timeout()}s (set CLAUDE_CLI_TIMEOUT)") from e
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise ClaudeCLIError(f"claude exited with code {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return parse_claude_json(proc.stdout)


def mock_transport(model: str, prompt: str) -> str:
    """Deterministic offline model for --dry-run: picks option A with 60% confidence."""
    import re

    letters = re.findall(r"^([A-H])\. ", prompt, re.MULTILINE)
    letters = letters or ["A", "B"]
    rest = (1 - 0.6) / max(1, len(letters) - 1)
    probs = ", ".join(f'"{x}": {0.6 if x == letters[0] else round(rest, 4)}' for x in letters)
    url = re.search(r"\) (https?://\S+)", prompt)
    cite = f'"{url.group(1)}"' if url else ""
    return f'{{"answer": "{letters[0]}", "probabilities": {{{probs}}}, "citations": [{cite}], "rationale": "mock"}}'
