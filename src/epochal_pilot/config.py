"""Environment and secrets handling. Secret values are never printed or logged."""

from __future__ import annotations

import os
from pathlib import Path

EPOCHAL_KEY_ENV = "EPOCHAL_API_KEY"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"


class MissingKeyError(RuntimeError):
    """Raised when a required API key is not set. The message names the variable, never a value."""


def load_dotenv(path: Path | str = ".env") -> list[str]:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win.

    Returns the names (not values) of variables it set.
    """
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    # utf-8-sig: Notepad on Windows may save .env with a BOM.
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if name and value and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def require_key(name: str, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingKeyError(
            f"{name} is not set. {hint}\n"
            f"  cp .env.example .env   # then put your key after {name}=\n"
            "  or run with --dry-run to try everything offline."
        )
    return value


def mask(value: str | None) -> str:
    """Safe representation of a secret for logs: only whether it is set."""
    return "set" if value else "not set"
