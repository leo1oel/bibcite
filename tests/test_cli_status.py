from bibcite import bibfile
from bibcite.cli import main


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
