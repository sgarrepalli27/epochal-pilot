from datetime import date

from epochal_pilot.public_context import PublicContext, load_public_context
from epochal_pilot.scenario import load_scenario


def _pack(scenarios_dir) -> PublicContext:
    sc = load_scenario(scenarios_dir, "fed-rates")
    pack = load_public_context(sc.path, sc.public_context)
    assert pack is not None
    return pack


def test_pack_ships_four_kinds_with_sources(scenarios_dir):
    pack = _pack(scenarios_dir)
    assert set(pack.kinds) == {"fomc_statement", "fomc_minutes", "bls_cpi", "bls_jobs"}
    assert len(pack.docs) >= 100
    for d in pack.docs:
        assert d.path.is_file(), d.id
        assert d.source_url.startswith(("https://www.federalreserve.gov/", "https://www.bls.gov/")), d.id
        assert len(d.text()) > 300, d.id
    assert "public domain" in pack.meta["license"].lower()


def test_every_event_gets_only_docs_before_as_of(scenarios_dir):
    """The leak check: for every event and several lead times, every doc predates the as-of date."""
    sc = load_scenario(scenarios_dir, "fed-rates")
    pack = _pack(scenarios_dir)
    for lead in (1, 7, 28):
        problems = pack.leak_check({e.id: e.as_of(lead) for e in sc.events})
        assert not [p for p in problems if "not before" in p]  # completeness is only required at the default lead
        for e in sc.events:
            gate = date.fromisoformat(e.as_of(lead)[:10])
            for d in pack.select(e.as_of(lead)):
                assert d.published < gate, (e.id, d.id)
                assert d.published < e.event_date
            # The meeting's own statement (the answer) is never in its context.
            assert all(e.event_date.strftime("%Y%m%d") not in d.id for d in pack.select(e.as_of(lead)))


def test_default_lead_has_all_kinds(scenarios_dir):
    sc = load_scenario(scenarios_dir, "fed-rates")
    pack = _pack(scenarios_dir)
    assert pack.leak_check({e.id: e.as_of(sc.lead_days) for e in sc.events}) == []


def test_previous_statement_is_selected(scenarios_dir):
    pack = _pack(scenarios_dir)
    ids = {d.id for d in pack.select("2024-09-11T00:00:00Z")}
    assert "fomc_statement_20240731" in ids
    assert "bls_cpi_20240814" in ids and "bls_jobs_20240906" in ids
    assert "fomc_minutes_20240731" in ids  # released 2024-08-21


def test_retrieval_carries_source_and_date(scenarios_dir):
    ret = _pack(scenarios_dir).retrieval("2024-09-11T00:00:00Z")
    assert len(ret.docs) == 4
    ctx = ret.context()
    assert "https://www.federalreserve.gov/" in ctx and "2024-08-21" in ctx


def test_leak_check_catches_bad_doc(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x" * 400, encoding="utf-8")
    (tmp_path / "index.json").write_text(
        '{"kinds": {"k": ""}, "docs": [{"id": "a", "kind": "k", "published": "2024-05-01", "title": "t", '
        '"source_url": "http://insecure", "file": "docs/a.md"}]}',
        encoding="utf-8",
    )
    pack = PublicContext(tmp_path)
    problems = pack.leak_check({"e1": "2024-04-01T00:00:00Z"})
    assert any("https" in p for p in problems)
    assert any("no document before" in p for p in problems)
