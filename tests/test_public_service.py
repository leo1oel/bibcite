import json

import httpx
import pytest

from bibcite import sources


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "BIBCITE_PUBLIC_SERVICE_URL",
        "BIBCITE_MAILTO",
        "OPENALEX_API_KEY",
        "S2_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sources, "_LAST_REQUEST", {})
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("url", "provider", "path"),
    (
        ("https://api.openalex.org/works", "openalex", "/works"),
        (
            "https://api.semanticscholar.org/graph/v1/paper/search",
            "semanticscholar",
            "/graph/v1/paper/search",
        ),
        (
            "https://api.crossref.org/works/10.1234/example/transform/application/x-bibtex",
            "crossref",
            "/works/10.1234/example/transform/application/x-bibtex",
        ),
    ),
)
def test_keyless_supported_requests_route_to_public_service(
    url, provider, path, monkeypatch
):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", "https://literature.test/v1/query")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"unchanged": True})

    with _client(handler) as client:
        response = sources._get(
            client,
            url,
            params={
                "query": "paper",
                "limit": 5,
                "api_key": "secret",
                "mailto": "secret@example.com",
            },
            headers={"x-api-key": "secret"},
        )

    assert response.json() == {"unchanged": True}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "https://literature.test/v1/query"
    assert "x-api-key" not in request.headers
    payload = json.loads(request.content)
    assert payload == {
        "provider": provider,
        "path": path,
        "params": {"query": "paper", "limit": "5"},
    }
    assert "secret" not in request.content.decode()


@pytest.mark.parametrize(
    ("variable", "url", "params"),
    (
        ("OPENALEX_API_KEY", "https://api.openalex.org/works", {"api_key": "mine"}),
        ("S2_API_KEY", "https://api.semanticscholar.org/graph/v1/paper/search", {}),
        (
            "BIBCITE_MAILTO",
            "https://api.crossref.org/works",
            {"mailto": "me@example.com"},
        ),
    ),
)
def test_personal_access_overrides_public_service(variable, url, params, monkeypatch):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", "https://literature.test/v1/query")
    monkeypatch.setenv(variable, "mine")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with _client(handler) as client:
        sources._get(client, url, params=params)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host != "literature.test"


def test_without_public_service_requests_remain_direct():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with _client(handler) as client:
        sources._get(client, "https://api.openalex.org/works", params={"search": "x"})

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "api.openalex.org"


@pytest.mark.parametrize("status", [429, 500])
def test_public_service_http_failure_is_not_retried(status, monkeypatch):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", "https://literature.test/v1/query")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with _client(handler) as client, pytest.raises(sources.SourceUnavailable):
        sources._paced_get(
            client,
            "https://api.semanticscholar.org/graph/v1/paper/search",
            "semanticscholar",
            0,
        )

    assert calls == 1


def test_public_service_timeout_is_not_retried(monkeypatch):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", "https://literature.test/v1/query")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(handler) as client, pytest.raises(sources.SourceUnavailable):
        sources._paced_get(
            client,
            "https://api.semanticscholar.org/graph/v1/paper/search",
            "semanticscholar",
            0,
        )

    assert calls == 1


def test_public_service_404_is_preserved(monkeypatch):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", "https://literature.test/v1/query")

    with _client(lambda request: httpx.Response(404)) as client:
        response = sources._get(client, "https://api.openalex.org/works/W1")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "value",
    (
        "http://literature.test/v1/query",
        "https://user:pass@literature.test/v1/query",
        "https://literature.test/v1/query?secret=x",
        "https://literature.test/v1/query#fragment",
    ),
)
def test_invalid_public_service_url_is_rejected(value, monkeypatch):
    monkeypatch.setenv("BIBCITE_PUBLIC_SERVICE_URL", value)
    with _client(lambda request: httpx.Response(200)) as client:
        with pytest.raises(sources.SourceUnavailable):
            sources._get(client, "https://api.openalex.org/works")
