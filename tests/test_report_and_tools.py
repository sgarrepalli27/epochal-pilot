import csv
import json

from epochal_pilot.cli import main
from epochal_pilot.planner import choose
from epochal_pilot.validate import validate_scenario, wording_issues


def _run(scenarios_dir, tmp_path, out, *extra):
    return main(
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
            "--out",
            str(out),
            *extra,
        ]
    )


def test_public_context_run_spends_nothing_and_needs_no_keys(scenarios_dir, tmp_path, capsys):
    out = tmp_path / "pub"
    assert _run(scenarios_dir, tmp_path, out, "--context", "public", "--limit", "4") == 0
    text = capsys.readouterr().out
    assert "Epochal requests: 0" in text
    meta = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert meta["context"] == "public" and meta["epochal_requests_this_run"] == 0
    assert meta["hashes"]["public_context_index_sha256"]
    with open(out / "predictions.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert {r["context"] for r in rows} == {"public"} and all(r["n_docs"] == "4" for r in rows)


def test_report_compare_and_share(scenarios_dir, tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    _run(scenarios_dir, tmp_path, a, "--context", "public", "--limit", "5")
    _run(scenarios_dir, tmp_path, b, "--limit", "5")
    capsys.readouterr()
    assert main(["--scenarios-dir", str(scenarios_dir), "report", str(b), "--share"]) == 0
    html = (b / "report.html").read_text(encoding="utf-8")
    assert "<svg" in html and "Cost ledger" in html and "<script" not in html
    share = b / "share"
    assert {p.name for p in share.iterdir()} >= {
        "predictions.csv",
        "scores.csv",
        "report.html",
        "run.json",
        "NOTICE.md",
    }
    blob = "".join(p.read_text(encoding="utf-8") for p in share.iterdir())
    assert "example.com" not in blob and "example.org" not in blob  # fixture URLs never leave
    assert "rationale" not in (share / "predictions.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "request_ids" not in (share / "run.json").read_text(encoding="utf-8")
    out = tmp_path / "cmp"
    assert main(["--scenarios-dir", str(scenarios_dir), "compare", str(a), str(b), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Leaderboard" in text and "gated[public]" in text and "gated[epochal]" in text
    assert "Paired comparisons" in text
    assert (out / "compare.html").is_file()


def test_report_on_v01_run_rebuilds_baselines(scenarios_dir, tmp_path, capsys):
    run = tmp_path / "old"
    run.mkdir()
    (run / "run.json").write_text('{"scenario": "fed-rates", "seed": 0, "modes": ["gated"]}', encoding="utf-8")
    (run / "predictions.csv").write_text(
        "model,mode,event,event_date,as_of,outcome,answer,answer_letter,truth_letter,correct,p_outcome,brier,"
        "abstained,probabilities\n"
        'm,gated,fomc_2024_07,2024-07-31,2024-07-24T00:00:00Z,Hold,Hold,B,B,1,1.0,0.0,0,"{""Hold"": 1.0, '
        '""Cut 25 bp"": 0.0, ""Cut 50 bp"": 0.0, ""Cut 75 bp"": 0.0, ""Hike 25 bp"": 0.0}"\n',
        encoding="utf-8",
    )
    assert main(["--scenarios-dir", str(scenarios_dir), "report", str(run)]) == 0
    assert "baseline:no-change" in (run / "report.md").read_text(encoding="utf-8")


def test_plan_prefers_signal_and_respects_budget(scenarios_dir, tmp_path, capsys):
    rc = main(
        [
            "--scenarios-dir",
            str(scenarios_dir),
            "--cache-dir",
            str(tmp_path / "c"),
            "--env-file",
            str(tmp_path / "none"),
            "plan",
            "fed-rates",
            "--requests",
            "8",
            "--model",
            "openai/gpt-4.1-mini",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "fomc_2026_09" in out and "--only" in out
    cands = [
        {"event": "a", "date": "2024-01-01", "lead": 7, "cost": 2.0, "score": 1.0},
        {"event": "b", "date": "2025-01-01", "lead": 7, "cost": 2.0, "score": 6.0},
        {"event": "c", "date": "2025-02-01", "lead": 7, "cost": 0.0, "score": 1.0},
    ]
    chosen, spent = choose(cands, 2)
    assert {c["event"] for c in chosen} == {"b", "c"} and spent == 2


def test_validate_shipped_scenarios_pass(scenarios_dir):
    for name in ("fed-rates", "future-leak"):
        errors, warnings, info = validate_scenario(scenarios_dir, name)
        assert errors == [] and warnings == [], (name, errors, warnings)
    assert info["cutoffs"]["openai/gpt-4.1-mini"]["post_cutoff"] > 0


def test_wording_checks():
    assert wording_issues("Who won the 2024 election?", "X", ["X", "Y"])
    assert wording_issues("Did Nvidia beat estimates?", "Beat", ["Beat", "Miss"])
    assert not wording_issues("Who will win the 2024 election?", "X", ["X", "Y"])
    assert wording_issues("Will Apple Intelligence be the name?", "Apple Intelligence", ["Apple Intelligence", "Siri"])


def test_new_scaffold_validates_and_dry_runs(tmp_path, capsys):
    d = tmp_path / "sc"
    assert main(["--scenarios-dir", str(d), "new", "my-test"]) == 0
    assert main(["--scenarios-dir", str(d), "new", "my-test"]) == 2  # no overwrite
    assert main(["--scenarios-dir", str(d), "new", "Bad Name"]) == 2
    assert main(["--scenarios-dir", str(d), "validate", "my-test"]) == 0
    assert (
        main(
            [
                "--scenarios-dir",
                str(d),
                "--cache-dir",
                str(tmp_path / "c"),
                "--env-file",
                str(tmp_path / "n"),
                "run",
                "my-test",
                "--dry-run",
                "--out",
                str(tmp_path / "o"),
            ]
        )
        == 0
    )


def test_validate_flags_errors(tmp_path, capsys):
    d = tmp_path / "sc"
    main(["--scenarios-dir", str(d), "new", "bad"])
    (d / "bad" / "events.csv").write_text(
        "id,event_date,question,choices,outcome,search_query,source_url\nx,2099-01-01,Who won it?,A|B,A,q,not-a-url\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(["--scenarios-dir", str(d), "validate", "bad"]) == 1
    out = capsys.readouterr().out
    assert "not in the past" in out and "source_url" in out and "past-tense" in out
