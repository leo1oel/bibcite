"""Exercise the CLI publication cascade without contacting external services."""

import importlib
import json

import httpx
import pytest

from bibcite import cache, cli, sources

resolver = importlib.import_module("bibcite.resolve")


@pytest.mark.parametrize("operation", ["get", "add", "upgrade"])
def test_cli_never_queries_google_scholar(operation, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cache, "DISABLED", True)
    monkeypatch.setattr(sources, "_DISABLED", {})
    monkeypatch.setattr(
        resolver,
        "arxiv_metadata",
        lambda _: sources.ArxivMeta(
            "1706.03762",
            "Attention Is All You Need",
            ["Ashish Vaswani"],
            "2017",
            "https://arxiv.org/abs/1706.03762",
        ),
    )
    visited = []
    for name in ("dblp", "semantic_scholar", "crossref", "unpaywall", "openalex"):

        def miss(*args, source=name):
            visited.append(source)
            return None

        monkeypatch.setattr(sources, f"try_{name}", miss)
    monkeypatch.setattr(sources, "try_dblp_fuzzy", lambda *args: None)
    requests = []

    def unexpected_request(client, url, **kwargs):
        requests.append(url)
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(sources, "_get", unexpected_request)
    path = tmp_path / "references.bib"
    if operation == "get":
        args = ["get", "--json", "1706.03762"]
    elif operation == "add":
        args = ["add", "--no-tidy", str(path), "1706.03762"]
    else:
        path.write_text("""@article{vaswani2017attention,
  title = {Attention Is All You Need},
  author = {Ashish Vaswani},
  year = {2017},
  journal = {arXiv preprint arXiv:1706.03762},
  eprint = {1706.03762}
}
""")
        args = ["upgrade", str(path), "--dry-run"]
    cli.main(args)
    json.loads(capsys.readouterr().out)
    assert set(visited) == {
        "dblp",
        "semantic_scholar",
        "crossref",
        "unpaywall",
        "openalex",
    }
    assert requests == []
