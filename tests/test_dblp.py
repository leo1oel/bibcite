import httpx
import pytest

import bibcite.sources as sources
from bibcite.bibfile import parse_bibtex_entry


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sources, "_LAST_REQUEST", {})
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)


def _hit(title="A Published Paper"):
    return {
        "info": {
            "authors": {
                "author": [
                    {"text": "Ada Lovelace 0001"},
                    {"text": "Grace Hopper"},
                ]
            },
            "title": title,
            "venue": "Test Conference",
            "year": "2025",
            "pages": "12-24",
            "volume": "42",
            "number": "3",
            "doi": "10.1000/test",
            "ee": "https://doi.org/10.1000/test",
            "url": "https://dblp.org/rec/conf/test/Lovelace25",
            "key": "conf/test/Lovelace25",
            "type": "Conference and Workshop Papers",
            "publisher": "Example Press",
            "editor": "Test Editor",
            "isbn": "978-0-00-000000-0",
        }
    }


@pytest.mark.parametrize("fuzzy", [False, True])
def test_dblp_match_uses_search_json_without_bib_download(monkeypatch, fuzzy):
    calls = []
    query_title = (
        "An Information-Theoretic Perspective on Variance-Invariance-Covariance Regularization"
        if fuzzy
        else "A Published Paper"
    )
    hit = _hit(
        "An Information Theory Perspective on Variance-Invariance-Covariance Regularization"
        if fuzzy
        else query_title
    )

    def get(client, url, params=None):
        calls.append((url, params))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"result": {"hits": {"hit": [hit]}}},
        )

    monkeypatch.setattr(sources, "_dblp_get", get)
    match = (
        sources.try_dblp_fuzzy(query_title, "lovelace", "2024")
        if fuzzy
        else sources.try_dblp(query_title)
    )

    assert match is not None
    assert len(calls) == 1
    assert calls[0][0].endswith("/search/publ/api")
    entry = parse_bibtex_entry(match.bibtex)
    assert entry["ENTRYTYPE"] == "inproceedings"
    assert entry["author"] == "Ada Lovelace and Grace Hopper"
    assert entry["title"] == hit["info"]["title"]
    assert entry["booktitle"] == "Test Conference"
    for field in (
        "year",
        "pages",
        "volume",
        "number",
        "doi",
        "url",
        "publisher",
        "editor",
        "isbn",
    ):
        expected = hit["info"]["ee"] if field == "url" else hit["info"][field]
        assert entry[field] == expected


def test_dblp_json_type_distinguishes_journal_articles():
    info = _hit()["info"]
    info.update(
        {
            "type": "Journal Articles",
            "key": "journals/test/Lovelace25",
            "venue": "Test Journal",
        }
    )

    entry = parse_bibtex_entry(sources._dblp_match(info, "dblp").bibtex)

    assert entry["ENTRYTYPE"] == "article"
    assert entry["journal"] == "Test Journal"
    assert "booktitle" not in entry


@pytest.mark.parametrize(
    ("record_type", "key", "expected"),
    [
        ("Books and Theses", "books/example/Test", "book"),
        ("Books and Theses", "phd/example/Test", "phdthesis"),
        ("Parts in Books or Collections", "books/example/TestChapter", "incollection"),
        ("Editorship", "conf/example/2025", "proceedings"),
    ],
)
def test_search_json_preserves_other_record_types(record_type, key, expected):
    info = _hit()["info"]
    info.update(type=record_type, key=key)
    entry = parse_bibtex_entry(sources._dblp_match(info, "dblp").bibtex)
    assert entry["ENTRYTYPE"] == expected
    assert "journal" not in entry
    if expected == "proceedings":
        assert entry["editor"] == "Test Editor"
        assert "author" not in entry


def test_dblp_exact_still_rejects_a_different_title(monkeypatch):
    monkeypatch.setattr(sources, "_dblp_search", lambda *args: [_hit("Other Paper")])

    assert sources.try_dblp("A Published Paper") is None


@pytest.mark.parametrize(
    ("author", "year"),
    [("different", "2024"), ("lovelace", "2010")],
)
def test_dblp_fuzzy_still_requires_author_and_plausible_year(monkeypatch, author, year):
    drifted = _hit(
        "An Information Theory Perspective on Variance-Invariance-Covariance Regularization"
    )
    monkeypatch.setattr(sources, "_dblp_search", lambda *args: [drifted])

    match = sources.try_dblp_fuzzy(
        "An Information-Theoretic Perspective on Variance-Invariance-Covariance Regularization",
        author,
        year,
    )

    assert match is None
