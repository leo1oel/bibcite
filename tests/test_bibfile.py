from pathlib import Path

from bibcite import bibfile as bibfile_module
from bibcite.bibfile import (
    entry_arxiv_id,
    entry_to_bibtex,
    find_existing,
    is_preprint,
    load_bib_file,
    parse_bibtex_entry,
    upsert_entry,
)

ARXIV_ENTRY = {
    "ENTRYTYPE": "article",
    "ID": "vaswani2017attention",
    "title": "Attention Is All You Need",
    "author": "Ashish Vaswani and Noam Shazeer",
    "journal": "arXiv preprint arXiv:1706.03762",
    "year": "2017",
    "eprint": "1706.03762",
    "url": "https://arxiv.org/abs/1706.03762",
}

PUBLISHED_ENTRY = {
    "ENTRYTYPE": "inproceedings",
    "ID": "vaswani2017attention",
    "title": "Attention Is All You Need",
    "author": "Ashish Vaswani and Noam Shazeer",
    "booktitle": "Advances in Neural Information Processing Systems (NIPS)",
    "year": "2017",
    "eprint": "1706.03762",
    "url": "https://arxiv.org/abs/1706.03762",
}


def test_roundtrip_entry():
    text = entry_to_bibtex(ARXIV_ENTRY)
    entry = parse_bibtex_entry(text)
    assert entry["title"] == "Attention Is All You Need"
    assert entry["ID"] == "vaswani2017attention"


def test_entry_arxiv_id():
    assert entry_arxiv_id(ARXIV_ENTRY) == "1706.03762"
    assert entry_arxiv_id({"journal": "arXiv preprint arXiv: 2103.14030"}) == "2103.14030"
    assert entry_arxiv_id({"journal": "NeurIPS"}) == ""


def test_is_preprint():
    assert is_preprint(ARXIV_ENTRY)
    assert not is_preprint(PUBLISHED_ENTRY)


def test_upsert_add_then_exists(tmp_path: Path):
    bib = tmp_path / "refs.bib"
    action, key = upsert_entry(bib, dict(PUBLISHED_ENTRY))
    assert (action, key) == ("added", "vaswani2017attention")
    action, key = upsert_entry(bib, dict(PUBLISHED_ENTRY))
    assert (action, key) == ("exists", "vaswani2017attention")


def test_upsert_upgrades_preprint_keeping_key(tmp_path: Path):
    bib = tmp_path / "refs.bib"
    old = dict(ARXIV_ENTRY, ID="myOldKey")
    upsert_entry(bib, old)
    action, key = upsert_entry(bib, dict(PUBLISHED_ENTRY))
    assert action == "upgraded"
    assert key == "myOldKey"  # the key the user may already \cite is kept
    db = load_bib_file(bib)
    assert db.entries[0]["booktitle"].startswith("Advances in Neural")


def test_find_existing_by_title_and_id(tmp_path: Path):
    bib = tmp_path / "refs.bib"
    upsert_entry(bib, dict(PUBLISHED_ENTRY))
    db = load_bib_file(bib)
    assert find_existing(db, "attention is all you need") is not None
    assert find_existing(db, "", arxiv_id="1706.03762") is not None
    assert find_existing(db, "some other paper") is None


def test_tidy_command_downloads_through_npx_when_not_installed(monkeypatch):
    def which(command):
        return "/usr/local/bin/npx" if command == "npx" else None

    monkeypatch.setattr(bibfile_module.shutil, "which", which)

    assert bibfile_module.tidy_command() == ["npx", "--yes", "bibtex-tidy"]
