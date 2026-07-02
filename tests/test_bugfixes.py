"""Regression tests for the first round of real-world bug reports."""

from pathlib import Path

from bibcite import cache
from bibcite.bibfile import parse_bibtex_entry, remove_entry, upsert_entry
from bibcite.normalize import fix_author_caps

# CrossRef's transform endpoint emits bare month macros (month=June) that are
# not in bibtexparser's common_strings — this used to KeyError('june').
CROSSREF_STYLE = (
    " @article{Hyv_rinen_2000, title={Independent component analysis}, "
    "volume={13}, url={http://dx.doi.org/10.1016/x}, DOI={10.1016/x}, "
    "journal={Neural Networks}, author={Hyvärinen, A. and Oja, E.}, "
    "year={2000}, month=June, pages={411–430} }"
)


def test_month_macro_full_name_parses():
    entry = parse_bibtex_entry(CROSSREF_STYLE)
    assert entry["title"] == "Independent component analysis"
    assert "month" not in entry  # month is a noise field, dropped


def test_month_macro_abbrev_parses():
    entry = parse_bibtex_entry("@article{x, title={T}, year={2000}, month=jun }")
    assert entry["title"] == "T"


def test_unknown_macro_raises_value_error_not_keyerror():
    import pytest

    with pytest.raises(ValueError, match="BibTeX parse failed"):
        parse_bibtex_entry("@article{x, title = somemacro }")


def test_fix_author_caps():
    assert (
        fix_author_caps("EPPS, T. W. and PULLEY, LAWRENCE B.")
        == "Epps, T. W. and Pulley, Lawrence B."
    )
    # Mixed-case names are never touched.
    assert fix_author_caps("McDonald, J. and van der Berg, A.") == (
        "McDonald, J. and van der Berg, A."
    )
    assert fix_author_caps("Ashish Vaswani") == "Ashish Vaswani"


PUB = {
    "ENTRYTYPE": "inproceedings",
    "ID": "k1",
    "title": "Paper One",
    "author": "A B",
    "booktitle": "Some Conference (SC)",
    "year": "2020",
}


def test_remove_entry(tmp_path: Path):
    bib = tmp_path / "r.bib"
    upsert_entry(bib, dict(PUB))
    assert remove_entry(bib, "k1") is True
    assert remove_entry(bib, "k1") is False  # already gone
    assert "Paper One" not in bib.read_text()


def test_upsert_replace_keeps_key(tmp_path: Path):
    bib = tmp_path / "r.bib"
    upsert_entry(bib, dict(PUB))
    newer = dict(PUB, ID="differentkey", author="Fixed Author")
    action, key = upsert_entry(bib, newer, replace=True)
    assert (action, key) == ("replaced", "k1")
    assert "Fixed Author" in bib.read_text()
    # Without --replace, a published duplicate stays untouched.
    action, key = upsert_entry(bib, newer)
    assert (action, key) == ("exists", "k1")


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(cache, "DISABLED", False)
    assert cache.get("somekey") is None
    cache.put("somekey", {"source": "dblp", "venue": "SC"})
    assert cache.get("somekey")["venue"] == "SC"


def test_cache_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(cache, "DISABLED", True)
    cache.put("k", {"venue": "X"})
    assert cache.get("k") is None
