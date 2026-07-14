import httpx
import pytest

import bibcite.sources as sources
from bibcite import cache
from bibcite.bibfile import load_bib_file
from bibcite.cli import _upgrade_entries
from bibcite.sources import Match, SourceUnavailable, find_published


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(cache, "DISABLED", True)
    monkeypatch.setattr(sources, "_DISABLED", {})
    monkeypatch.setattr(sources, "_LAST_REQUEST", {})
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)
    monkeypatch.setattr(sources, "try_dblp_fuzzy", lambda *args, **kwargs: None)


class _ReadErrorClient:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        request = httpx.Request("GET", url, params=params, headers=headers)
        if self.calls <= self.failures:
            raise httpx.ReadError("simulated transient read error", request=request)
        return httpx.Response(200, request=request, json={})


def test_paced_get_retries_a_single_read_error():
    client = _ReadErrorClient(failures=1)

    response = sources._paced_get(client, "https://dblp.org/test", "dblp", 0)

    assert response.status_code == 200
    assert client.calls == 2


def test_dblp_read_failures_do_not_disable_later_batch_entries(monkeypatch):
    def dblp(title, year, arxiv_id, author_hint):
        if title == "First paper":
            return sources._paced_get(
                _ReadErrorClient(failures=3),
                "https://dblp.org/test",
                "dblp",
                0,
            )
        return Match(source="dblp", venue="TMLR", title=title, year="2025")

    monkeypatch.setattr(
        sources,
        "CASCADE",
        (
            ("dblp", dblp),
            ("crossref", lambda *args: None),
        ),
    )

    first_match, first_status = find_published("First paper")
    second_match, second_status = find_published("Second paper")

    assert first_match is None
    assert first_status == "incomplete"
    assert "dblp" not in sources._DISABLED
    assert second_status == "found"
    assert second_match is not None
    assert second_match.venue == "TMLR"


def test_upgrade_retries_dblp_after_previous_entry_read_failures(
    tmp_path, monkeypatch
):
    bib = tmp_path / "refs.bib"
    bib.write_text(
        """@misc{first,
  author = {Doe, Jane},
  title = {First paper},
  howpublished = {arXiv preprint arXiv:2501.00001},
  year = {2025},
}

@misc{second,
  author = {Roe, Richard},
  title = {Second paper},
  howpublished = {arXiv preprint arXiv:2501.00002},
  year = {2025},
}
"""
    )

    def dblp(title, year, arxiv_id, author_hint):
        if title == "First paper":
            return sources._paced_get(
                _ReadErrorClient(failures=3),
                "https://dblp.org/test",
                "dblp",
                0,
            )
        return Match(source="dblp", venue="TMLR", title=title, year="2025")

    monkeypatch.setattr(
        sources,
        "CASCADE",
        (
            ("dblp", dblp),
            ("crossref", lambda *args: None),
        ),
    )

    result = _upgrade_entries(bib, dry_run=False)
    entries = {entry["ID"]: entry for entry in load_bib_file(bib).entries}

    assert result["matched"] == 1
    assert result["upgraded"] == 1
    assert "howpublished" in entries["first"]
    assert entries["second"]["journal"] == "Transactions on Machine Learning Research (TMLR)"


def test_explicit_dblp_rate_limit_still_disables_later_entries(monkeypatch):
    calls = 0

    def dblp(*args):
        nonlocal calls
        calls += 1
        raise SourceUnavailable("simulated 429")

    monkeypatch.setattr(
        sources,
        "CASCADE",
        (
            ("dblp", dblp),
            ("crossref", lambda *args: None),
        ),
    )

    find_published("First paper")
    find_published("Second paper")

    assert calls == 1
    assert sources._DISABLED["dblp"] == "simulated 429"
