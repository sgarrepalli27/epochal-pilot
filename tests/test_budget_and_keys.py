import pytest

from epochal_pilot import config
from epochal_pilot.cache import Budget, BudgetExceeded, Cache
from epochal_pilot.epochal import Epochal


def test_budget_per_run_and_persistence(tmp_path):
    calls = []
    t = lambda e, a, b: calls.append(b) or {"results": []}
    path = tmp_path / "budget.json"
    ep = Epochal(Cache(tmp_path), Budget(path, per_run=2, max_total=100), "k", transport=t)
    ep.search("a", "2024-01-01T00:00:00Z")
    ep.search("a", "2024-01-01T00:00:00Z")  # cached: free
    ep.search("b", "2024-01-01T00:00:00Z")
    with pytest.raises(BudgetExceeded, match="--budget"):
        ep.search("c", "2024-01-01T00:00:00Z")
    assert len(calls) == 2
    assert Budget(path, per_run=5, max_total=100).total == 2  # persisted


def test_budget_lifetime_cap(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text('{"used": 90}', encoding="utf-8")
    b = Budget(path, per_run=10, max_total=90)
    assert b.remaining == 0
    with pytest.raises(BudgetExceeded, match="lifetime"):
        b.spend()


def test_missing_key_message_names_variable_only(monkeypatch):
    with pytest.raises(config.MissingKeyError) as e:
        config.require_key("OPENROUTER_API_KEY", "hint")
    assert "OPENROUTER_API_KEY" in str(e.value) and "--dry-run" in str(e.value)


def test_dotenv_loader(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# c\nEPOCHAL_API_KEY="abc"\nexport OPENROUTER_API_KEY=xyz # comment\nEMPTY=\n', encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "already")
    loaded = config.load_dotenv(p)
    import os

    assert loaded == ["EPOCHAL_API_KEY"]
    assert os.environ["EPOCHAL_API_KEY"] == "abc"
    assert os.environ["OPENROUTER_API_KEY"] == "already"  # env wins


def test_reprs_hide_keys(tmp_path):
    from epochal_pilot.llm import LLM

    secret = "sk-super-secret-value"
    ep = Epochal(Cache(tmp_path), Budget(tmp_path / "b", 1, 1), secret)
    llm = LLM(Cache(tmp_path), api_key=secret)
    assert secret not in repr(ep) and secret not in repr(llm)
    assert config.mask(secret) == "set"
