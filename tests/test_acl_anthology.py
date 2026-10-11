"""ACL Anthology DOIs resolve from the Anthology's own export, not Crossref."""

import importlib
import json

import httpx
import pytest

from bibcite import cache, cli, sources

resolver = importlib.import_module("bibcite.resolve")

POPE_DOI = "10.18653/v1/2023.emnlp-main.20"
POPE_BIB = """@inproceedings{li-etal-2023-evaluating,
    title = "Evaluating Object Hallucination in Large Vision-Language Models",
    author = "Li, Yifan  and
      Du, Yifan  and
      Zhou, Kun  and
      Wang, Jinpeng  and
      Zhao, Wayne Xin  and
      Wen, Ji-Rong",
    editor = "Bouamor, Houda  and
      Pino, Juan  and
      Bali, Kalika",
    booktitle = "Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing",
    month = dec,
    year = "2023",
    address = "Singapore",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2023.emnlp-main.20/",
    doi = "10.18653/v1/2023.emnlp-main.20",
    pages = "292--305"
}
"""

# Crossref's deposit for the same DOI drops "Wayne".
CROSSREF = sources.Match(
    source="crossref",
    venue="Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing",
    title="Evaluating Object Hallucination in Large Vision-Language Models",
    year="2023",
    authors=["Yifan Li", "Xin Zhao"],
    doi=POPE_DOI,
    bibtex="""@inproceedings{Li_2023,
  author = {Li, Yifan and Zhao, Xin},
  title = {Evaluating Object Hallucination in Large Vision-Language Models},
  booktitle = {Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing},
  year = {2023},
  pages = {292--305},
  doi = {10.18653/v1/2023.emnlp-main.20}
}
""",
)


@pytest.fixture
def anthology(monkeypatch):
    """Serve Anthology exports by URL; record every request."""
    served: dict[str, httpx.Response | Exception] = {}
    requested: list[str] = []

    def get(client, url, **kwargs):
        requested.append(url)
        answer = served.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        answer.request = httpx.Request("GET", url)
        return answer

    monkeypatch.setattr(cache, "DISABLED", True)
    monkeypatch.setattr(sources, "_get", get)
    monkeypatch.setattr(resolver, "crossref_by_doi", lambda doi: CROSSREF)
    return served, requested


def _get_json(capsys, query: str) -> dict:
    assert cli.main(["get", "--json", query]) == 0
    return json.loads(capsys.readouterr().out)


def test_anthology_doi_keeps_full_author_names(anthology, capsys):
    served, _ = anthology
    served["https://aclanthology.org/2023.emnlp-main.20.bib"] = httpx.Response(
        200, text=POPE_BIB
    )
    payload = _get_json(capsys, POPE_DOI)
    assert payload["source"] == "acl-anthology"
    assert "Zhao, Wayne Xin" in payload["bibtex"]
    assert "pages = {292--305}" in payload["bibtex"]
    assert "EMNLP" in payload["venue"]
    # The export's wrapped name lists come out on one line.
    assert "editor = {Bouamor, Houda and Pino, Juan and Bali, Kalika}" in payload["bibtex"]


def test_legacy_anthology_ids_are_requested_in_upper_case(anthology, capsys):
    served, requested = anthology
    served["https://aclanthology.org/P19-1001.bib"] = httpx.Response(
        200,
        text=POPE_BIB.replace(POPE_DOI, "10.18653/v1/P19-1001"),
    )
    payload = _get_json(capsys, "10.18653/v1/p19-1001")
    assert payload["source"] == "acl-anthology"
    assert requested == ["https://aclanthology.org/P19-1001.bib"]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("unreachable"),
        httpx.Response(503),
        httpx.Response(404),
        # An export for some other DOI is not this paper's record.
        httpx.Response(200, text=POPE_BIB.replace(POPE_DOI, "10.18653/v1/2023.emnlp-main.21")),
        httpx.Response(200, text="<html>maintenance</html>"),
    ],
)
def test_crossref_is_the_fallback_when_the_anthology_cannot_answer(
    anthology, capsys, answer
):
    served, _ = anthology
    served["https://aclanthology.org/2023.emnlp-main.20.bib"] = answer
    payload = _get_json(capsys, POPE_DOI)
    assert payload["source"] == "crossref"


def test_other_dois_never_ask_the_anthology(anthology, capsys, monkeypatch):
    _, requested = anthology
    monkeypatch.setattr(
        resolver,
        "crossref_by_doi",
        lambda doi: sources.Match(
            source="crossref",
            venue="Nature",
            title="Some Paper",
            year="2020",
            authors=["A B"],
            doi=doi,
        ),
    )
    payload = _get_json(capsys, "10.1038/s41586-020-0000-0")
    assert payload["source"] == "crossref"
    assert requested == []
