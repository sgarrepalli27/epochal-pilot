"""Default predictor resolution and the System One plugin registry."""

from epochal_pilot.cli import build_parser
from epochal_pilot.providers import (
    ALL_BACKENDS,
    SYSTEM_ONE_BACKENDS,
    get_predictor_class,
    resolve_backend,
    system_two_notice,
    uses_rules_analysis,
)


def test_default_is_jev_rules_when_typesafe_key_is_set(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    backend, notice = resolve_backend(None)
    assert backend == "jev-rules"
    assert notice and "jev-rules" in notice


def test_default_is_rules_without_typesafe_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    backend, notice = resolve_backend(None)
    assert backend == "rules"
    assert "TYPESAFE_API_KEY" in notice
    assert "openai-decisions" in notice and "gemini-decisions" in notice
    assert "azure" not in notice.lower()


def test_explicit_backend_wins(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    backend, notice = resolve_backend("openai-decisions")
    assert backend == "openai-decisions" and notice is None


def test_system_two_notice_recommends_decisions_apis():
    note = system_two_notice("openrouter")
    assert note and "System Two" in note
    assert "openai-decisions" in note and "gemini-decisions" in note
    assert system_two_notice("jev-rules") is None


def test_uses_rules_analysis_flags():
    assert uses_rules_analysis("jev-rules", False)
    assert uses_rules_analysis("rules", False)
    assert uses_rules_analysis("openai-decisions", True)
    assert not uses_rules_analysis("openai-decisions", False)
    assert not uses_rules_analysis("jev", False)


def test_official_system_one_providers_are_registered():
    for name in ("jev", "jev-rules", "rules", "openai-decisions", "gemini-decisions", "openai", "gemini"):
        assert name in SYSTEM_ONE_BACKENDS
        get_predictor_class(name)
    assert "azure-openai" not in SYSTEM_ONE_BACKENDS
    assert "azure-openai-decisions" not in ALL_BACKENDS


def test_cli_backend_choices_exclude_azure():
    parser = build_parser()
    # walk subparsers to the run command
    run = None
    for action in parser._subparsers._group_actions:
        run = action.choices.get("run")
        if run:
            break
    assert run is not None
    dest = {a.dest: a for a in run._actions}
    choices = set(dest["backend"].choices)
    assert "openai-decisions" in choices and "gemini-decisions" in choices
    assert "jev-rules" in choices and "openrouter" in choices
    assert not any("azure" in c for c in choices)


def test_extra_provider_module(monkeypatch, tmp_path):
    mod = tmp_path / "my_provider.py"
    mod.write_text(
        "from epochal_pilot.providers import Predictor, PredictResult, register\n"
        "@register('toy')\n"
        "class Toy(Predictor):\n"
        "    name = 'toy'\n"
        "    def predict(self, state, question, options):\n"
        "        pick = options[0]\n"
        "        return PredictResult(probabilities={o: (1.0 if o == pick else 0.0) for o in options}, answer=pick)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("EPOCHAL_PILOT_PROVIDER_MODULE", "my_provider")
    # allow reload if a previous test loaded a module
    import epochal_pilot.providers as prov

    if getattr(prov.load_extra_providers, "_loaded", None):
        prov.load_extra_providers._loaded = None  # type: ignore[attr-defined]
    cls = get_predictor_class("toy")
    result = cls().predict({}, "q", ["A", "B"])
    assert result.answer == "A" and result.probabilities["A"] == 1.0
