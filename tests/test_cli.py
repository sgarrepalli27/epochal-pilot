import csv
import json
from pathlib import Path

from epochal_pilot.cli import main


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_list(scenarios_dir, capsys):
    assert main(["--scenarios-dir", str(scenarios_dir), "list"]) == 0
    out = capsys.readouterr().out
    assert "fed-rates" in out and "future-leak" in out


def test_missing_keys_exit_cleanly(scenarios_dir, tmp_path, capsys):
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
            "--limit",
            "1",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2 and "EPOCHAL_API_KEY is not set" in err


def test_estimate_needs_no_keys(scenarios_dir, tmp_path, capsys):
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
            "--estimate",
        ]
    )
    assert rc == 0 and "13 new needed" in capsys.readouterr().out


def test_dry_run_end_to_end(scenarios_dir, tmp_path, capsys):
    out = tmp_path / "out"
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
            "--dry-run",
            "--limit",
            "3",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    preds = _read(out / "predictions.csv")
    assert len(preds) == 3
    assert all(p["dropped_future"] == "1" for p in preds)  # fixture's 2099 article is filtered
    assert all("RESULT ANNOUNCED" not in p["rationale"] for p in preds)
    scores = {r["model"]: r for r in _read(out / "scores.csv")}
    assert {"mock-model", "baseline:no-change", "baseline:persistence", "baseline:uniform"} <= set(scores)
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["dry_run"] and meta["lead_days"] == 7
    assert "key" not in json.dumps(meta).lower()


def test_dry_run_future_leak_both_modes(scenarios_dir, tmp_path):
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
            "future-leak",
            "--dry-run",
            "--limit",
            "2",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    modes = {r["mode"] for r in _read(out / "predictions.csv")}
    assert modes == {"gated", "ungated"}


def test_budget_stops_run_cleanly(scenarios_dir, tmp_path, monkeypatch, capsys):
    """A live-looking run with a mocked HTTP layer stops at --budget and still writes results."""
    import epochal_pilot.epochal as ep_mod
    import epochal_pilot.llm as llm_mod

    monkeypatch.setenv("EPOCHAL_API_KEY", "test-not-a-real-key")
    calls = []
    monkeypatch.setattr(
        ep_mod.Epochal, "_http", lambda self, e, a, b: calls.append(e) or ep_mod.fixture_transport(e, a, b)
    )
    monkeypatch.setattr(llm_mod.LLM, "_ollama", lambda self, m, p: llm_mod.mock_transport(m, p))
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
            "ollama",
            "--budget",
            "3",
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0 and len(calls) == 3
    assert "Stopped early" in captured.err
    assert "test-not-a-real-key" not in captured.out + captured.err
