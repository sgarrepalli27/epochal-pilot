from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenarios_dir() -> Path:
    return ROOT / "scenarios"


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    """Tests run offline: no real keys, and no stray .env from the developer's checkout."""
    for k in ("EPOCHAL_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
