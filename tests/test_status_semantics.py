"""The batch-429 poisoning scenario: a disabled core source must taint the
verdict ('incomplete'), never masquerade as a trustworthy 'not_found'."""

import pytest

import bibcite.sources as sources
from bibcite import cache
from bibcite.sources import SourceUnavailable, find_published


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "DISABLED", True)
    monkeypatch.setattr(sources, "_DISABLED", {})
    # No fuzzy fallback network calls in these tests.
    monkeypatch.setattr(sources, "try_dblp_fuzzy", lambda *a, **k: None)


def _cascade(**outcomes):
    """Build a fake CASCADE; outcome per source: None=clean miss, 'raise'=429."""

    def make(o):
        def fn(t, y, a, au):
            if o == "raise":
                raise SourceUnavailable("simulated 429")
            return o

        return fn

    return tuple((name, make(o)) for name, o in outcomes.items())


def test_all_clean_misses_is_trustworthy(monkeypatch):
    monkeypatch.setattr(
        sources, "CASCADE", _cascade(dblp=None, semanticscholar=None, crossref=None)
    )
    match, status = find_published("Some Title", author_hint="smith")
    assert (match, status) == (None, "not_found")


def test_core_source_429_taints_verdict(monkeypatch):
    # DBLP 429s, others answer cleanly — the user's exact failure chain.
    monkeypatch.setattr(
        sources,
        "CASCADE",
        _cascade(dblp="raise", googlescholar=None, crossref=None),
    )
    match, status = find_published("Some Title", author_hint="smith")
    assert (match, status) == (None, "incomplete")


def test_previously_disabled_core_source_taints_next_queries(monkeypatch):
    # Query N tripped DBLP; queries N+1... in the same run inherit the taint.
    monkeypatch.setattr(sources, "_DISABLED", {"dblp": "429"})
    monkeypatch.setattr(
        sources, "CASCADE", _cascade(dblp=None, crossref=None)  # dblp skipped anyway
    )
    match, status = find_published("Another Title", author_hint="smith")
    assert (match, status) == (None, "incomplete")


def test_noncore_outage_does_not_taint(monkeypatch):
    # Google Scholar captcha is routine; a miss stays trustworthy.
    monkeypatch.setattr(
        sources,
        "CASCADE",
        _cascade(dblp=None, googlescholar="raise", crossref=None),
    )
    match, status = find_published("Some Title", author_hint="smith")
    assert (match, status) == (None, "not_found")


def test_everything_down_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        sources, "CASCADE", _cascade(dblp="raise", semanticscholar="raise")
    )
    match, status = find_published("Some Title", author_hint="smith")
    assert (match, status) == (None, "unavailable")
