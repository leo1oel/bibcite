import json
import shutil

import pytest

from bibcite import bibfile, cli
from bibcite.resolve import _finalize
from bibcite.sources import ArxivMeta, Match


DOI = "10.1109/CVPR52733.2024.01187"
PUBLISHED = (
    "@inproceedings{paper, author={A}, title={Paper}, booktitle={CVPR}, "
    "year={2024}, doi={" + DOI + "}, eprint={2102.08981}, "
    "archiveprefix={arXiv}, primaryclass={cs.CV}, "
    "howpublished={arXiv preprint arXiv:2102.08981}}\n"
)


@pytest.mark.parametrize("operation", ["normalize", "upgrade", "resolve", "tidy"])
def test_field_policy_across_entry_paths(operation, tmp_path, monkeypatch, capsys):
    path = tmp_path / "refs.bib"
    path.write_text(PUBLISHED)
    if operation == "normalize":
        assert cli.main(["normalize", str(path)]) == 0
        entry = bibfile.parse_bibtex_entry(json.loads(capsys.readouterr().out)["bibtex"][0])
    elif operation == "upgrade":
        monkeypatch.setattr(cli, "find_published", lambda *args: (
            Match(source="dblp", venue="CVPR", year="2024", doi=DOI), "found"
        ))
        cli._upgrade_entries(path, dry_run=False, include_published_arxiv=True)
        entry = bibfile.load_bib_file(path).entries[0]
    elif operation == "resolve":
        entry = cli._resolve_user_bibtex(PUBLISHED).entry
    else:
        executable = shutil.which("bibtex-tidy")
        if not executable:
            pytest.skip("optional installed bibtex-tidy integration")
        monkeypatch.setattr(bibfile, "tidy_command", lambda: [executable])
        assert bibfile.run_tidy(path)
        entry = bibfile.load_bib_file(path).entries[0]
    assert entry["doi"] == DOI
    assert entry["eprint"] == "2102.08981"
    assert entry["archiveprefix"] == "arXiv"
    assert "primaryclass" not in entry
    assert "howpublished" not in entry


@pytest.mark.parametrize("description", ["arXiv preprint arXiv:2102.08981", r"\url{https://example.org}"])
def test_keep_preprint_and_web_publishing_descriptions(description):
    entry = {"ENTRYTYPE": "misc", "howpublished": description, "primaryclass": "cs.CV", "eprint": "2102.08981"}
    bibfile.clean_publication_fields(entry)
    assert entry["howpublished"] == description
    assert entry["eprint"] == "2102.08981"
    assert "primaryclass" not in entry


def test_resolving_arxiv_does_not_reintroduce_primaryclass():
    meta = ArxivMeta(arxiv_id="2102.08981", title="Paper", authors=["A"], year="2024", abs_url="https://arxiv.org/abs/2102.08981", primary_class="cs.CV")
    entry = _finalize({"title": "Paper", "author": "A", "year": "2024", "doi": DOI}, meta)
    assert entry["doi"] == DOI
    assert entry["eprint"] == meta.arxiv_id
    assert "primaryclass" not in entry


def test_explicit_preprint_keeps_its_publication_description():
    entry = {"booktitle": "CVPR", "pubstate": "preprint", "howpublished": "arXiv preprint arXiv:2102.08981", "primaryclass": "cs.CV"}
    bibfile.clean_publication_fields(entry)
    assert "howpublished" in entry
    assert "primaryclass" not in entry


def test_tidy_preserves_user_macros_during_cleanup(tmp_path, monkeypatch):
    executable = shutil.which("bibtex-tidy")
    if not executable:
        pytest.skip("optional installed bibtex-tidy integration")
    monkeypatch.setattr(bibfile, "tidy_command", lambda: [executable])
    path = tmp_path / "refs.bib"
    path.write_text('@string{customname = "Keep this"}\n' + PUBLISHED.replace("primaryclass=", "custom=customname, primaryclass="))
    assert bibfile.run_tidy(path)
    assert "customname" in path.read_text().split("@inproceedings", 1)[1]
    assert "primaryclass" not in path.read_text()
