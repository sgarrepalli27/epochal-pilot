import json
import subprocess
from types import SimpleNamespace

import pytest

from epochal_pilot import llm as llm_mod
from epochal_pilot.cache import Cache
from epochal_pilot.cli import main
from epochal_pilot.llm import LLM, ClaudeCLIError, claude_cli_args, parse_claude_json, run_claude_cli


def test_args_disable_all_tools_and_mcp():
    args = claude_cli_args("sonnet")
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--output-format") + 1] == "json"
    assert args[-2:] == ["--disallowedTools", "*"]  # last: the flag takes a list
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--max-turns") + 1] == "1"
    assert args[args.index("--model") + 1] == "sonnet"
    assert "--model" not in claude_cli_args("default")
    assert all(a != "" for a in args)  # empty args get lost by Windows .cmd shims


def test_parse_claude_json():
    assert parse_claude_json(json.dumps({"type": "result", "result": "hi", "is_error": False})) == "hi"
    events = [{"type": "system"}, {"type": "result", "result": "last"}]
    assert parse_claude_json(json.dumps(events)) == "last"
    with pytest.raises(ClaudeCLIError, match="error"):
        parse_claude_json(json.dumps({"type": "result", "is_error": True, "result": "auth"}))
    with pytest.raises(ClaudeCLIError, match="non-JSON"):
        parse_claude_json("Not logged in")


def test_run_passes_prompt_on_stdin_without_shell(monkeypatch):
    monkeypatch.setattr(llm_mod, "find_claude", lambda: r"C:\Users\me\AppData\Roaming\npm\claude.cmd")
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw, args=args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": '{"answer": "A"}'}), stderr="")

    out = run_claude_cli("default", "PROMPT TEXT", runner=fake_run)
    assert out == '{"answer": "A"}'
    assert seen["args"][0].endswith("claude.cmd") and "PROMPT TEXT" not in seen["args"]
    assert seen["input"] == "PROMPT TEXT" and "shell" not in seen and seen["timeout"] > 0
    assert seen["encoding"] == "utf-8"


def test_run_errors(monkeypatch):
    monkeypatch.setattr(llm_mod, "find_claude", lambda: None)
    with pytest.raises(ClaudeCLIError, match="not on PATH"):
        run_claude_cli("default", "p")
    monkeypatch.setattr(llm_mod, "find_claude", lambda: "/usr/bin/claude")

    def timeout(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    with pytest.raises(ClaudeCLIError, match="timed out"):
        run_claude_cli("default", "p", runner=timeout)
    fail = lambda args, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom")
    with pytest.raises(ClaudeCLIError, match="code 1"):
        run_claude_cli("default", "p", runner=fail)


def test_backend_uses_cli_and_caches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_mod, "run_claude_cli", lambda m, p: calls.append((m, p)) or "reply")
    llm = LLM(Cache(tmp_path), backend="claude-cli")
    assert llm.ask("default", "q") == "reply" and llm.ask("default", "q") == "reply"
    assert len(calls) == 1
    assert llm.params()["args"][-2:] == ["--disallowedTools", "*"]


def test_end_to_end_public_context_with_mocked_claude(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("epochal_pilot.runner.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(llm_mod, "find_claude", lambda: "/usr/bin/claude")

    real = subprocess.run

    def fake_run(args, **kw):
        if args[0] != "/usr/bin/claude":  # e.g. git rev-parse for the run manifest
            return real(args, **kw)
        assert args[-2:] == ["--disallowedTools", "*"]
        reply = llm_mod.mock_transport("m", kw["input"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "result", "result": reply}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
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
            "claude-cli",
            "--context",
            "public",
            "--limit",
            "2",
            "--out",
            str(out),
        ]
    )
    assert rc == 0, capsys.readouterr().err
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "claude-cli" and meta["models"] == ["default"] and meta["epochal_requests_this_run"] == 0


def test_missing_claude_exits_cleanly(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("epochal_pilot.runner.find_claude", lambda: None)
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
            "claude-cli",
            "--context",
            "public",
        ]
    )
    assert rc == 2 and "not on PATH" in capsys.readouterr().err


def test_check_reports_claude(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("epochal_pilot.cli.find_claude", lambda: "/opt/bin/claude")
    assert (
        main(
            [
                "--scenarios-dir",
                str(scenarios_dir),
                "--cache-dir",
                str(tmp_path),
                "--env-file",
                str(tmp_path / "n"),
                "check",
            ]
        )
        == 0
    )
    assert "on PATH: /opt/bin/claude" in capsys.readouterr().out
