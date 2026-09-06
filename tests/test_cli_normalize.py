import json

from bibcite.cli import main


def test_normalize_preserves_entry_order_offline(tmp_path, capsys):
    source = tmp_path / "s2.bib"
    source.write_text(
        "@inproceedings{a, title={A}, year={2024}, booktitle={CVPR}}\n"
        "@article{b, title={B}, year={2023}, journal={Nature}}\n"
    )
    assert main(["normalize", str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["bibtex"]) == 2
    assert "IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)" in payload["bibtex"][0]
    assert "journal = {Nature}" in payload["bibtex"][1]
