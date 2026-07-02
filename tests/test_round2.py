"""Regression tests for the second round of field-use bug reports."""

from pathlib import Path

from bibcite.bibfile import MONTH_STRINGS, load_bib_file, upsert_entry, _write_db
from bibcite.normalize import titles_similar

ARXIV_TITLE = "An Information-Theoretic Perspective on Variance-Invariance-Covariance Regularization"
PUBLISHED_TITLE = "An Information Theory Perspective on Variance-Invariance-Covariance Regularization"


def test_titles_similar_catches_camera_ready_drift():
    assert titles_similar(ARXIV_TITLE, PUBLISHED_TITLE)


def test_titles_similar_rejects_different_papers():
    assert not titles_similar(
        "Attention Is All You Need",
        "An Image is Worth 16x16 Words: Transformers for Image Recognition",
    )
    assert not titles_similar("Deep Residual Learning", "")


ENTRY = {
    "ENTRYTYPE": "inproceedings",
    "ID": "k1",
    "title": "Paper One",
    "author": "A B",
    "booktitle": "Some Conference (SC)",
    "year": "2020",
}


def test_replace_without_match_errors_instead_of_adding(tmp_path: Path):
    bib = tmp_path / "r.bib"
    upsert_entry(bib, dict(ENTRY))
    stranger = dict(ENTRY, ID="k2", title="A Totally Different Paper")
    action, key = upsert_entry(bib, stranger, replace=True)
    assert action == "no_match_to_replace"
    assert "Totally Different" not in bib.read_text()  # nothing was written


def test_replace_key_targets_specific_entry(tmp_path: Path):
    bib = tmp_path / "r.bib"
    upsert_entry(bib, dict(ENTRY))
    drifted = dict(ENTRY, ID="whatever", title="Paper One Revised Title")
    action, key = upsert_entry(bib, drifted, replace_key="k1")
    assert (action, key) == ("replaced", "k1")
    assert "Paper One Revised Title" in bib.read_text()
    action, _ = upsert_entry(bib, drifted, replace_key="nonexistent")
    assert action == "no_match_to_replace"


def test_month_strings_never_written_to_file(tmp_path: Path):
    bib = tmp_path / "m.bib"
    # Simulate a file polluted by the old bug: @string month macros present.
    bib.write_text(
        '@string{january = {January}}\n'
        '@article{x, title = {T}, author = {A B}, year = {2000}, month = january }\n'
    )
    db = load_bib_file(bib)
    _write_db(bib, db)
    text = bib.read_text()
    assert "@string" not in text  # scrubbed on write
    assert "title" in text


def test_month_strings_cover_all_months():
    for m in ("january", "may", "june", "december", "jan", "jun", "dec"):
        assert m in MONTH_STRINGS
