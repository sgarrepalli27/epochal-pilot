import json

import pytest

import epochal_pilot.llm as llm_mod
from epochal_pilot.cache import Cache
from epochal_pilot.cli import main
from epochal_pilot.llm import LLM


def _capture(monkeypatch, reply):
    sent = {}

    def fake_post(url, body, headers=None, timeout=60):
        sent.update(url=url, body=body, headers=headers or {})
        return reply

    monkeypatch.setattr(llm_mod, "post_json", fake_post)
    return sent


def test_anthropic_request_and_text_blocks(tmp_path, monkeypatch):
    reply = {
        "content": [
            {"type": "thinking", "thinking": "hidden reasoning"},
            {"type": "text", "text": '{"answer": "A", '},
            {"type": "text", "text": '"probabilities": {"A": 1}}'},
        ]
    }
    sent = _capture(monkeypatch, reply)
    llm = LLM(Cache(tmp_path), backend="anthropic", api_key="test-key")
    out = llm.ask("claude-haiku-5-5", "hello")
    assert out == '{"answer": "A", "probabilities": {"A": 1}}'
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "test-key"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["model"] == "claude-haiku-5-5"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["body"]["max_tokens"] >= 4000
    assert "hidden reasoning" not in out


def test_openrouter_request(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, {"choices": [{"message": {"content": "ANSWER: B"}}]})
    out = LLM(Cache(tmp_path), backend="openrouter", api_key="k").ask("openai/gpt-4.1-mini", "q")
    assert out == "ANSWER: B"
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["body"]["temperature"] == 0


def test_ollama_request(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, {"message": {"content": "ANSWER: C"}})
    out = LLM(Cache(tmp_path), backend="ollama").ask("qwen2.5:3b", "q")
    assert out == "ANSWER: C" and sent["url"].endswith("/api/chat")
    assert "Authorization" not in sent["headers"]


def test_replies_are_cached_per_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm_mod, "post_json", lambda *a, **k: calls.append(1) or {"content": [{"type": "text", "text": "x"}]}
    )
    llm = LLM(Cache(tmp_path), backend="anthropic", api_key="k")
    llm.ask("m", "p")
    llm.ask("m", "p")
    assert len(calls) == 1


def test_anthropic_missing_key(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EPOCHAL_API_KEY", "x")
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-rates",
            "--backend",
            "anthropic",
            "--limit",
            "1",
        ]
    )
    assert rc == 2 and "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_anthropic_end_to_end_mocked(scenarios_dir, tmp_path, monkeypatch, capsys):
    import epochal_pilot.epochal as ep_mod

    monkeypatch.setenv("EPOCHAL_API_KEY", "epochal-test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-secret")
    monkeypatch.setattr(ep_mod.Epochal, "_http", lambda self, e, a, b: ep_mod.fixture_transport(e, a, b))
    monkeypatch.setattr(
        llm_mod,
        "post_json",
        lambda url, body, headers=None, timeout=60: {
            "content": [{"type": "text", "text": llm_mod.mock_transport("m", body["messages"][0]["content"])}]
        },
    )
    out = tmp_path / "o"
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "run",
            "fed-rates",
            "--backend",
            "anthropic",
            "--limit",
            "2",
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["models"] == ["claude-haiku-5-5"] and meta["backend"] == "anthropic"
    for secret in ("epochal-test-secret", "anthropic-test-secret"):
        assert secret not in captured.out + captured.err
        for f in out.iterdir():
            assert secret not in f.read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["EPOCHAL_API_KEY=abc\r\n", "\ufeffEPOCHAL_API_KEY=abc\n"])
def test_dotenv_windows_line_endings_and_bom(tmp_path, monkeypatch, text):
    from epochal_pilot import config

    p = tmp_path / ".env"
    p.write_bytes(text.encode("utf-8"))
    config.load_dotenv(p)
    import os

    assert os.environ["EPOCHAL_API_KEY"] == "abc"
