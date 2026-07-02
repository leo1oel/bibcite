"""Regression tests for the third round of field-use reports."""

from pathlib import Path

from bibcite.bibfile import (
    _scrub_month_strings,
    find_existing,
    load_bib_file,
    upsert_entry,
)
from bibcite.normalize import fix_pages


def test_fix_pages_dashes():
    assert fix_pages("411–430") == "411--430"  # en-dash
    assert fix_pages("411-430") == "411--430"  # single hyphen
    assert fix_pages("411 -- 430") == "411--430"
    assert fix_pages("723—726") == "723--726"  # em-dash
    assert fix_pages("e123") == "e123"  # no range untouched


PREPRINT = {
    "ENTRYTYPE": "misc",
    "ID": "old",
    "title": "An Information-Theoretic Perspective on VICReg",
    "author": "Ravid Shwartz-Ziv and Yann LeCun",
    "howpublished": "arXiv preprint arXiv:2303.00633",
    "eprint": "2303.00633",
    "year": "2023",
}


def test_find_existing_by_doi_in_url(tmp_path: Path):
    bib = tmp_path / "d.bib"
    upsert_entry(
        bib,
        {
            "ENTRYTYPE": "article",
            "ID": "k",
            "title": "T",
            "author": "A B",
            "journal": "J",
            "year": "2000",
            "url": "https://doi.org/10.1093/biomet/70.3.723",
        },
    )
    db = load_bib_file(bib)
    # No doi field on the entry — matched via the url (pre-0.4.0 files).
    assert find_existing(db, "", doi="10.1093/biomet/70.3.723") is not None


def test_dedupe_catches_title_drift_pair(tmp_path: Path):
    bib = tmp_path / "p.bib"
    upsert_entry(bib, dict(PREPRINT))
    published = {
        "ENTRYTYPE": "inproceedings",
        "ID": "new",
        "title": "An Information Theory Perspective on VICReg",  # drifted
        "author": "Ravid Shwartz-Ziv and Yann LeCun",
        "booktitle": "Advances in Neural Information Processing Systems (NeurIPS)",
        "year": "2023",
    }
    action, key = upsert_entry(bib, published)
    # Fuzzy same-author dedupe: upgraded in place, NOT added as a duplicate.
    assert (action, key) == ("upgraded", "old")
    assert bib.read_text().count("@") == 1


def test_scrub_orphan_month_strings(tmp_path: Path):
    bib = tmp_path / "m.bib"
    bib.write_text(
        "@string{january = {January}}\n@string{june = {June}}\n"
        "@article{x, title = {T}, author = {A B}, year = {2000} }\n"
    )
    _scrub_month_strings(bib)
    text = bib.read_text()
    assert "@string" not in text
    assert "title" in text


def test_scrub_leaves_clean_files_alone(tmp_path: Path):
    bib = tmp_path / "c.bib"
    original = "@article{x,\n  title = {T},\n  author = {A B},\n  year = {2000},\n}\n"
    bib.write_text(original)
    _scrub_month_strings(bib)
    assert bib.read_text() == original  # untouched, not even rewritten
