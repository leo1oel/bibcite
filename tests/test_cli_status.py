import json

from bibcite import bibfile, cli
from bibcite.cli import main
from bibcite.sources import Match


def test_check_fails_for_missing_file(tmp_path):
    assert main(["check", str(tmp_path / "missing.bib")]) == 1


def test_check_fails_when_lint_finds_problems(tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text("@misc{x, title={T}}\n")

    assert main(["check", str(bib)]) == 1


def test_check_succeeds_for_clean_file(tmp_path):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{x, author={Doe, Jane}, title={T}, journal={Nature}, year={2024}}\n"
    )

    assert main(["check", str(bib)]) == 0


def test_upgrade_fails_for_missing_file(tmp_path):
    assert main(["upgrade", str(tmp_path / "missing.bib")]) == 1


def test_upgrade_opt_in_audits_published_arxiv_entry_through_real_cli(
    tmp_path, monkeypatch, capsys
):
    bib = tmp_path / "refs.bib"
    original_venue = (
        "IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)"
    )
    bib.write_text(
        "@inproceedings{ranzinger2024amradio,\n"
        "  author={Mike Ranzinger and Greg Heinrich and Jan Kautz and Pavlo Molchanov},\n"
        f"  booktitle={{{original_venue}}},\n"
        "  eprint={2312.06709},\n"
        "  title={{AM-RADIO:} Agglomerative Vision Foundation Model Reduce All Domains Into One},\n"
        "  year={2024}\n}\n"
    )
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: True)
    monkeypatch.setattr(
        cli,
        "find_published",
        lambda *args: (
            Match(
                venue=original_venue,
                year="2024",
                doi="10.1109/CVPR52733.2024.01219",
                source="dblp",
            ),
            "found",
        ),
    )

    assert main(["upgrade", str(bib), "--include-published-arxiv"]) == 0

    result = json.loads(capsys.readouterr().out)
    updated = bibfile.load_bib_file(bib).entries[0]
    assert result["upgraded"] == 1
    assert updated["booktitle"] == original_venue
    assert updated["doi"] == "10.1109/CVPR52733.2024.01219"


def test_upgrade_opt_in_preserves_published_venue_on_no_match(
    tmp_path, monkeypatch, capsys
):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@inproceedings{x, title={T}, booktitle={Existing Venue}, "
        "eprint={2312.06709}, year={2024}}\n"
    )
    before = bib.read_text()
    monkeypatch.setattr(cli, "find_published", lambda *args: (None, "not_found"))

    assert main(["upgrade", str(bib), "--include-published-arxiv", "--no-tidy"]) == 0

    assert json.loads(capsys.readouterr().out)["upgraded"] == 0
    assert bib.read_text() == before


def test_add_fails_when_automatic_tidy_fails(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "refs.bib"
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: False)

    exit_code = main(
        [
            "add",
            str(bib),
            "--bibtex",
            "@article{x, author={Doe, Jane}, title={T}, journal={Nature}, year={2024}}",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["action"] == "added"
    assert result["tidied"] is False


def test_add_no_tidy_is_an_explicit_success(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "refs.bib"

    def fail_tidy(path):
        raise AssertionError("tidy should not run")

    monkeypatch.setattr(bibfile, "run_tidy", fail_tidy)

    exit_code = main(
        [
            "add",
            str(bib),
            "--bibtex",
            "@article{x, author={Doe, Jane}, title={T}, journal={Nature}, year={2024}}",
            "--no-tidy",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["action"] == "added"
    assert result["tidied"] is False


def test_remove_fails_when_automatic_tidy_fails(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{x, author={Doe, Jane}, title={T}, journal={Nature}, year={2024}}\n"
    )
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: False)

    exit_code = main(["remove", str(bib), "x"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["action"] == "removed"
    assert result["tidied"] is False


def test_upgrade_fails_when_automatic_tidy_fails(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "refs.bib"
    bib.write_text("@article{x, title={T}}\n")
    monkeypatch.setattr(
        cli,
        "_upgrade_entries",
        lambda path, dry_run, include_published_arxiv: {
            "upgraded": 1,
            "matched": 1,
            "entries": [],
        },
    )
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: False)

    exit_code = main(["upgrade", str(bib)])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["upgraded"] == 1
    assert result["tidied"] is False


def test_fix_reports_remaining_lint_problems(tmp_path, monkeypatch):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{x, title={T}, journal={Nature}, year={2024}}\n"
    )
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: True)

    assert main(["fix", str(bib)]) == 1


def test_fix_succeeds_when_file_is_clean(tmp_path, monkeypatch):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        "@article{x, author={Doe, Jane}, title={T}, journal={Nature}, year={2024}}\n"
    )
    monkeypatch.setattr(bibfile, "run_tidy", lambda path: True)

    assert main(["fix", str(bib)]) == 0
