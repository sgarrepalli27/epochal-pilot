"""Date masking rewrites calendar years relative to T and leaves no raw years behind."""

from epochal_pilot.masking import mask_text, mask_value, year_label


def test_year_labels_are_relative_to_as_of():
    assert year_label(2022, 2022) == "Year 0"
    assert year_label(2021, 2022) == "Year -1"
    assert year_label(2023, 2022) == "Year +1"


def test_mask_text_strips_iso_compact_and_long_dates():
    as_of = "2022-02-15"
    text = (
        "On 15 February 2022 the FOMC met, after 15-16 December 2021 and the "
        "January 26, 2022 statement (monetary20220126a). CPI for January 2022. "
        "A decline not seen since February 1982. Published 2022-01-12 and 20211215."
    )
    out = mask_text(text, as_of)
    for year in ("2022", "2021", "1982"):
        assert year not in out, out
    assert "Year 0" in out
    assert "Year -1" in out
    assert "Year -40" in out
    # policy numbers and ordinary integers stay
    assert "0-0.25" not in text  # sanity of fixture
    leftover = mask_text("The range was 0-0.25% with a 2% goal in 2022.", as_of)
    assert "0-0.25%" in leftover and "2%" in leftover
    assert "2022" not in leftover
    url = mask_text("https://www.bls.gov/news.release/archives/cpi_02102022.htm", as_of)
    assert "2022" not in url and "Year 0" in url


def test_mask_value_walks_nested_state():
    state = {
        "as_of": "2022-02-15",
        "documents": [{"title": "Statement, 26 January 2022", "text": "Released 2021-12-15."}],
        "n": 7,
    }
    out = mask_value(state, "2022-02-15")
    blob = str(out)
    assert "2022" not in blob and "2021" not in blob
    assert out["n"] == 7
    assert "Year 0" in out["as_of"]
    assert "Year -1" in out["documents"][0]["text"]
