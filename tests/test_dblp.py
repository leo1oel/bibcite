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
    monkeypatch.setattr(sources, "_dblp_title_search", lambda *args: [hit])
    match = (
        sources.try_dblp_fuzzy(query_title, "lovelace", "2024")
        if fuzzy
        else sources.try_dblp(query_title)
    )

    assert match is not None
    assert calls == []
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
    monkeypatch.setattr(
        sources, "_dblp_title_search", lambda *args: [_hit("Other Paper")]
    )

    assert sources.try_dblp("A Published Paper") is None


@pytest.mark.parametrize(
    ("author", "year"),
    [("different", "2024"), ("lovelace", "2010")],
)
def test_dblp_fuzzy_still_requires_author_and_plausible_year(monkeypatch, author, year):
    drifted = _hit(
        "An Information Theory Perspective on Variance-Invariance-Covariance Regularization"
    )
    monkeypatch.setattr(sources, "_dblp_title_search", lambda *args: [drifted])

    match = sources.try_dblp_fuzzy(
        "An Information-Theoretic Perspective on Variance-Invariance-Covariance Regularization",
        author,
        year,
    )

    assert match is None


def _triples(key="conf/test/Lovelace25", title="A Published Paper."):
    prefix = "https://dblp.org/rdf/schema#"
    url = "https://dblp.org/rec/" + key
    triples = [
        (url, "title", title),
        (url, "yearOfPublication", "2025"),
        (url, "publishedIn", "Test Conference"),
        (url, "type", prefix + "Inproceedings"),
        (url, "type", prefix + "Publication"),
        (url, "pagination", "12-24"),
        (url, "primaryDocumentPage", "https://example.org/paper"),
        (url, "publishedInJournalVolume", "42"),
        (url, "publishedInJournalVolumeIssue", "3"),
        (url, "publishedBy", "Example Press"),
        (url, "doi", "https://doi.org/10.1000/test"),
        (url, "hasSignature", "s2"),
        (url, "hasSignature", "s1"),
        ("s2", "signatureDblpName", "Grace Hopper"),
        ("s2", "signatureOrdinal", "2"),
        ("s2", "type", prefix + "AuthorSignature"),
        ("s1", "signatureDblpName", "Ada Lovelace 0001"),
        ("s1", "signatureOrdinal", "1"),
        ("s1", "type", prefix + "AuthorSignature"),
    ]
    return [
        {
            k: {"type": "literal", "value": v}
            for k, v in {
                "publ": url,
                "subject": subject,
                "predicate": prefix + predicate,
                "value": value,
            }.items()
        }
        for subject, predicate, value in triples
    ]


def test_sparql_returns_complete_ordered_authors_in_one_request(monkeypatch):
    calls = []

    def get(client, url, params=None):
        calls.append((url, params))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"results": {"bindings": _triples()}},
        )

    monkeypatch.setattr(sources, "_dblp_get", get)
    match = sources.try_dblp("A Published Paper", "lovelace")
    assert match is not None
    assert match.authors == ["Ada Lovelace", "Grace Hopper"]
    assert match.doi == "10.1000/test"
    entry = parse_bibtex_entry(match.bibtex)
    assert entry["pages"] == "12-24"
    assert entry["url"] == "https://example.org/paper"
    assert entry["volume"] == "42"
    assert entry["number"] == "3"
    assert entry["publisher"] == "Example Press"
    assert entry["ENTRYTYPE"] == "inproceedings"
    assert len(calls) == 1
    assert calls[0][0] == "https://sparql.dblp.org/sparql"
    assert "LIMIT 101" in calls[0][1]["query"]
    assert "dblp:hasSignature?" in calls[0][1]["query"]


@pytest.mark.parametrize("author", ["different", "lace"])
def test_sparql_exact_checks_author_not_substring(monkeypatch, author):
    monkeypatch.setattr(sources, "_dblp_title_search", lambda *args: [_hit()])
    assert sources.try_dblp("A Published Paper", author) is None


def test_sparql_overflow_is_incomplete_not_a_clean_miss(monkeypatch):
    monkeypatch.setattr(
        sources, "_dblp_title_search", lambda *args: [_hit("Other")] * 101
    )
    with pytest.raises(sources.TransientSourceError, match="window exceeded"):
        sources.try_dblp("A Published Paper")


@pytest.mark.parametrize(
    "payload",
    [{}, {"results": {}}, {"results": {"bindings": [{"publ": {"value": "bad"}}]}}],
)
def test_sparql_malformed_data_does_not_become_a_clean_miss(monkeypatch, payload):
    monkeypatch.setattr(
        sources,
        "_dblp_get",
        lambda c, url, params=None: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=payload,
        ),
    )
    with pytest.raises((KeyError, sources.TransientSourceError)):
        sources.try_dblp("A Published Paper")


def test_sparql_timeout_does_not_retry_search_endpoint(monkeypatch):
    calls = []

    def get(client, url, params=None):
        calls.append(url)
        raise sources.TransientSourceError("timeout")

    monkeypatch.setattr(sources, "_dblp_get", get)
    with pytest.raises(sources.TransientSourceError):
        sources.try_dblp("A Published Paper")
    assert calls == ["https://sparql.dblp.org/sparql"]
