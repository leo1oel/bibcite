"""API clients for the publication-matching cascade.

Order and matching rules ported from PaperMemory's bibMatcher:
DBLP -> Semantic Scholar -> CrossRef -> OpenAlex.
All matchers verify identity via normalized-title equality and reject
preprint venues (arXiv / CoRR / bioRxiv / ...).
"""

import html
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .normalize import (
    clean_title,
    first_author_last_name,
    mini_hash,
    norm_title,
    sig_tokens,
    titles_similar,
)

UA = "bibcite/0.6 (https://github.com/leo1oel/bibcite)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Per-request cap for the publication-matching sources. Kept short so one slow
# or half-down source (DBLP in particular) can't stall an interactive resolve;
# a source that can't answer in time is treated as unavailable → "incomplete",
# never a false "not published". The arXiv metadata fetch sets its own longer
# timeout on the request itself, so this does not affect it.
TIMEOUT = 8.0
# Publication matching is enrichment on top of a valid arXiv citation. Keep
# the entire concurrent cascade, including its DBLP title-drift fallback,
# within an interactive budget instead of letting sequential retries add up.
PUBLICATION_TIMEOUT = 10.0

PREPRINT_VENUES = re.compile(r"arxiv|corr|biorxiv|medrxiv|chemrxiv|ssrn|preprint", re.I)
ARXIV_DOI = re.compile(r"^10\.48550/", re.I)


def _log(msg: str):
    print(msg, file=sys.stderr)


class SourceUnavailable(Exception):
    """Raised when a source rate-limits/blocks us; the cascade skips it."""


class PageUnreadable(Exception):
    """A page refused to be read and will refuse again — a different
    identifier, or a hand-written entry, is the next step rather than a
    retry."""


class TransientSourceError(SourceUnavailable):
    """Raised after request retries are exhausted without tripping the
    process-wide circuit breaker for later batch entries."""


class PublicationTimeout(TransientSourceError):
    """The total publication-matching budget was exhausted."""


class DblpTimeout(TransientSourceError):
    """A DBLP request timed out and may be retried once within its budget."""


_REQUEST_DEADLINE = threading.local()


def _request_timeout(cap: float = TIMEOUT) -> float:
    deadline = getattr(_REQUEST_DEADLINE, "value", None)
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicationTimeout("publication lookup timed out")
    return max(0.001, min(cap, remaining))


def _get(
    c: httpx.Client,
    url: str,
    *,
    timeout: float | httpx.Timeout = TIMEOUT,
    **kwargs,
) -> httpx.Response:
    """GET directly or route keyless supported APIs through the public service."""
    public_url = os.environ.get("BIBCITE_PUBLIC_SERVICE_URL")
    target = urlsplit(url)
    providers = {
        "api.openalex.org": "openalex",
        "api.semanticscholar.org": "semanticscholar",
        "api.crossref.org": "crossref",
    }
    provider = providers.get((target.hostname or "").lower())
    personal_access = {
        "openalex": bool(os.environ.get("OPENALEX_API_KEY")),
        "semanticscholar": bool(
            os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        ),
        "crossref": bool(os.environ.get("BIBCITE_MAILTO")),
    }
    if not public_url or not provider or personal_access[provider]:
        effective_timeout = (
            timeout if isinstance(timeout, httpx.Timeout) else _request_timeout(timeout)
        )
        return c.get(url, timeout=effective_timeout, **kwargs)

    service = urlsplit(public_url)
    loopback = service.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        (service.scheme != "https" and not (service.scheme == "http" and loopback))
        or not service.hostname
        or service.username is not None
        or service.password is not None
        or service.query
        or service.fragment
    ):
        raise SourceUnavailable("BIBCITE_PUBLIC_SERVICE_URL is invalid")

    params = kwargs.get("params") or {}
    safe_params = {
        str(key): str(value)
        for key, value in params.items()
        if str(key).lower() not in {"api_key", "mailto"}
    }
    try:
        response = c.post(
            public_url,
            json={"provider": provider, "path": target.path, "params": safe_params},
            timeout=_request_timeout(timeout),
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        raise SourceUnavailable(
            f"public literature service unavailable ({type(e).__name__})"
        ) from e
    if response.status_code == 404:
        return response
    if response.status_code == 429:
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            code = None
        if isinstance(code, str) and code in {
            "queue_busy",
            "daily_quota",
            "upstream_rate_limit",
        }:
            raise SourceUnavailable(f"public literature service {code}")
        raise SourceUnavailable("public literature service rate-limited (429)")
    if response.is_error:
        raise SourceUnavailable(
            f"public literature service error ({response.status_code})"
        )
    return response


def _sleep(delay: float):
    """Sleep for pacing/backoff without crossing the publication deadline."""
    deadline = getattr(_REQUEST_DEADLINE, "value", None)
    if deadline is None:
        time.sleep(delay)
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicationTimeout("publication lookup timed out")
    time.sleep(min(delay, remaining))
    if delay >= remaining:
        raise PublicationTimeout("publication lookup timed out")


def _with_deadline(deadline: float, fn, *args):
    previous = getattr(_REQUEST_DEADLINE, "value", None)
    _REQUEST_DEADLINE.value = deadline
    try:
        return fn(*args)
    finally:
        if previous is None:
            del _REQUEST_DEADLINE.value
        else:
            _REQUEST_DEADLINE.value = previous


def _client(browser: bool = False) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": BROWSER_UA if browser else UA},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def _s2_headers() -> dict:
    """Semantic Scholar's unauthenticated pool is shared globally and 429s
    often; a free API key (https://api.semanticscholar.org) gets a private
    quota. Set S2_API_KEY (or SEMANTIC_SCHOLAR_API_KEY)."""
    key = os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    return {"x-api-key": key} if key else {}


def _s2_batch_status() -> str | None:
    status = os.environ.get("BIBCITE_S2_BATCH_STATUS")
    return status if status in {"checked", "unavailable", "disabled"} else None


def _mailto() -> str:
    """Contact email for the polite pools (CrossRef/OpenAlex).
    Set BIBCITE_MAILTO to use your own."""
    return os.environ.get("BIBCITE_MAILTO") or "bibcite@gmail.com"


def _openalex_params(extra: dict) -> dict:
    """OpenAlex rejects ANONYMOUS search with 503 under heavy load ("use a
    free API key for uninterrupted access"); OPENALEX_API_KEY unlocks it."""
    params = {**extra, "mailto": _mailto()}
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return params


@dataclass
class Match:
    source: str
    venue: str
    title: str = ""
    year: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    bibtex: str = ""  # raw bibtex when the source provides one
    url: str = ""


@dataclass
class ArxivMeta:
    arxiv_id: str
    title: str
    authors: list[str]
    year: str
    abs_url: str
    primary_class: str = ""
    doi: str = ""


def _is_published_venue(venue: str) -> bool:
    return bool(venue) and not PREPRINT_VENUES.search(venue)


# ---------------------------------------------------------------------------
# arXiv metadata
# ---------------------------------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def arxiv_api_get(params: dict) -> httpx.Response:
    """export.arxiv.org allows ~1 request / 3s; retry politely on 429/timeouts."""
    last: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(3 * attempt)
        try:
            with _client() as c:
                r = _get(
                    c,
                    "https://export.arxiv.org/api/query",
                    params=params,
                    timeout=30.0,
                )
                if r.status_code == 429:
                    last = SourceUnavailable("arXiv API rate-limited (429)")
                    continue
                r.raise_for_status()
                return r
        except httpx.HTTPError as e:
            last = e
    raise last if last else SourceUnavailable("arXiv API unavailable")


def arxiv_metadata(arxiv_id: str) -> ArxivMeta:
    r = arxiv_api_get({"id_list": arxiv_id})
    root = ET.fromstring(r.text)
    entry = root.find(f"{ATOM}entry")
    if entry is None or entry.find(f"{ATOM}title") is None:
        raise ValueError(f"arXiv id not found: {arxiv_id}")
    title = clean_title(entry.find(f"{ATOM}title").text or "")
    if title.lower() == "error":
        raise ValueError(f"arXiv id not found: {arxiv_id}")
    authors = [
        (a.find(f"{ATOM}name").text or "").strip()
        for a in entry.findall(f"{ATOM}author")
        if a.find(f"{ATOM}name") is not None
    ]
    authors = [a for a in authors if a]
    published = entry.find(f"{ATOM}published")
    year = (published.text or "")[:4] if published is not None else ""
    primary = entry.find(f"{ARXIV_NS}primary_category")
    primary_class = primary.get("term") if primary is not None else ""
    doi_el = entry.find(f"{ARXIV_NS}doi")
    doi = doi_el.text if doi_el is not None else ""
    return ArxivMeta(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        year=year,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        primary_class=primary_class,
        doi=doi or "",
    )


# ---------------------------------------------------------------------------
# DBLP
# ---------------------------------------------------------------------------

# Process-local pacing for DBLP. Semantic Scholar uses a cross-process SQLite
# dispatch gate below, shared with the Rust batch client.
_LAST_REQUEST: dict[str, float] = {}


# DBLP throttles at roughly 1-2 req/s and escalates to temporary IP bans.
def _dblp_get(c: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    wait = 0.8 - (time.monotonic() - _LAST_REQUEST.get("dblp", 0.0))
    if wait > 0:
        _sleep(wait)
    _LAST_REQUEST["dblp"] = time.monotonic()
    remaining = _request_timeout(2.5)
    timeout = httpx.Timeout(
        connect=min(1.5, remaining),
        read=min(2.5, remaining),
        write=min(1.0, remaining),
        pool=min(1.0, remaining),
    )
    try:
        response = _get(c, url, params=params, timeout=timeout)
    except httpx.TimeoutException as e:
        raise DblpTimeout(f"dblp request timed out ({type(e).__name__})") from e
    except httpx.HTTPError as e:
        raise TransientSourceError(f"dblp unreachable ({type(e).__name__})") from e
    if response.status_code == 429:
        raise SourceUnavailable("dblp rate-limited (429)")
    if response.status_code >= 500:
        raise TransientSourceError(f"dblp server error ({response.status_code})")
    return response


def _dblp_title_search(c: httpx.Client, title: str) -> list:
    """Indexed title candidates, not a full-dataset regex or a retry cascade.

    QLever's literal text index avoids the slow CompleteSearch endpoint.
    The caller still verifies the entire normalized title and author identity.
    Limit publications BEFORE expanding author signatures, otherwise a paper
    with many authors can silently lose authors or crowd out the actual match.
    """
    tokens = sorted(
        set(re.findall(r"[^\W_]+", title.lower())), key=lambda t: (-len(t), t)
    )[:5]
    if not tokens:
        raise TransientSourceError("dblp title has no searchable words")
    words = json.dumps(" ".join(tokens), ensure_ascii=False)
    query = f"""PREFIX dblp: <https://dblp.org/rdf/schema#>
PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>
SELECT ?publ ?subject ?predicate ?value WHERE {{
  {{ SELECT DISTINCT ?publ ?title WHERE {{
    ?text ql:contains-word {words} ; ql:contains-entity ?title .
    ?publ dblp:title ?title .
  }} ORDER BY STRLEN(STR(?title)) ?publ LIMIT 101 }}
  ?publ dblp:hasSignature? ?subject .
  ?subject ?predicate ?value .
}}"""
    response = _dblp_get(
        c,
        "https://sparql.dblp.org/sparql",
        params={"query": query, "action": "sparql_json_export"},
    )
    response.raise_for_status()
    # Do not turn an HTML challenge or malformed/partial response into a clean miss.
    rows = response.json()["results"]["bindings"]
    # Read the bounded publication/signature triples instead of joining every
    # optional field with every author. Those joins made live queries ~5s;
    # this indexed property path returns the same fields without a cross product.
    records: dict[str, dict[str, dict[str, list[str]]]] = {}
    for row in rows:
        values = {key: value["value"] for key, value in row.items()}
        url = values["publ"]
        if not url.startswith("https://dblp.org/rec/"):
            raise TransientSourceError(
                "dblp returned an invalid publication identifier"
            )
        subject = records.setdefault(url, {}).setdefault(values["subject"], {})
        subject.setdefault(values["predicate"].rsplit("#", 1)[-1], []).append(
            values["value"]
        )
    kinds = {
        "Article": "Journal Articles",
        "Inproceedings": "Conference and Workshop Papers",
        "Incollection": "Parts in Books or Collections",
        "Book": "Books and Theses",
        "Editorship": "Editorship",
        "Informal": "Informal and Other Publications",
        "Reference": "Parts in Books or Collections",
    }
    fields = {
        "title": "title",
        "yearOfPublication": "year",
        "publishedIn": "venue",
        "pagination": "pages",
        "publishedInJournalVolume": "volume",
        "publishedInJournalVolumeIssue": "number",
        "publishedBy": "publisher",
        "primaryDocumentPage": "ee",
    }
    hits = []
    for url, subjects in records.items():
        record = subjects[url]
        info = {
            target: record[prop][0]
            for prop, target in fields.items()
            if record.get(prop)
        }
        info.update(url=url, key=url.removeprefix("https://dblp.org/rec/"))
        for kind in record.get("type", []):
            if kind.rsplit("#", 1)[-1] in kinds:
                info["type"] = kinds[kind.rsplit("#", 1)[-1]]
        if record.get("doi"):
            info["doi"] = record["doi"][0].removeprefix("https://doi.org/")
        if record.get("isbn"):
            info["isbn"] = record["isbn"][0].removeprefix("urn:isbn:")
        if not info.get("ee") and record.get("documentPage"):
            info["ee"] = record["documentPage"][0]
        authors, editors = [], []
        for signature in record.get("hasSignature", []):
            data = subjects[signature]
            name = data["signatureDblpName"][0]
            ordinal = int(data["signatureOrdinal"][0])
            target = (
                editors
                if any(t.endswith("#EditorSignature") for t in data.get("type", []))
                else authors
            )
            target.append((ordinal, name))
        info["authors"] = {"author": [{"text": name} for _, name in sorted(authors)]}
        info["editor"] = " and ".join(name for _, name in sorted(editors))
        hits.append({"info": info})
    return hits


def _dblp_bibtex(info: dict, venue: str) -> str:
    """Build the DBLP record from search JSON, avoiding a second .bib request."""
    from .bibfile import entry_to_bibtex

    record_type = str(info.get("type", "")).lower()
    key = str(info.get("key") or "dblp")
    if record_type == "editorship":
        entry_type = "proceedings" if key.startswith("conf/") else "book"
    elif key.startswith("phd/"):
        entry_type = "phdthesis"
    elif record_type == "parts in books or collections":
        entry_type = "incollection"
    elif record_type == "books and theses":
        entry_type = "book"
    else:
        entry_type = (
            "inproceedings"
            if "conference" in record_type or key.startswith("conf/")
            else "article"
        )
    fields = {
        "ID": key,
        "ENTRYTYPE": entry_type,
        "author": " and ".join(_dblp_hit_authors(info)),
        "title": clean_title(html.unescape(str(info.get("title", "")))),
        "year": str(info.get("year", "")),
        "pages": str(info.get("pages", "")),
        "volume": str(info.get("volume", "")),
        "number": str(info.get("number", "")),
        "doi": str(info.get("doi", "")),
        "url": str(info.get("ee") or info.get("url") or ""),
        "publisher": str(info.get("publisher", "")),
        "editor": str(info.get("editor", "")),
        "isbn": str(info.get("isbn", "")),
    }
    if entry_type in {"inproceedings", "incollection"}:
        fields["booktitle"] = venue
    elif entry_type == "article":
        fields["journal"] = venue
    if record_type == "editorship":
        fields["editor"] = fields["editor"] or fields["author"]
        fields.pop("author")
    return entry_to_bibtex(fields)


def _dblp_match(info: dict, source: str) -> Match:
    venue = info["venue"]
    if isinstance(venue, list):
        venue = venue[0]
    title = clean_title(html.unescape(info.get("title", "")))
    return Match(
        source=source,
        venue=str(venue),
        title=title,
        year=str(info.get("year", "")),
        authors=_dblp_hit_authors(info),
        doi=info.get("doi", ""),
        bibtex=_dblp_bibtex(info, str(venue)),
        url=info.get("ee", "") or info.get("url", ""),
    )


def try_dblp(title: str, author_hint: str = "") -> Match | None:
    """One SPARQL title lookup; failures never start another network attempt."""
    with _client() as c:
        hits = _dblp_title_search(c, title)
    # Prefer the original conference publication over later journal extensions.
    hits.sort(key=lambda h: int(h.get("info", {}).get("year", 9999)))
    ref = norm_title(title)
    for hit in hits:
        info = hit.get("info", {})
        if norm_title(html.unescape(info.get("title", ""))) != ref:
            continue
        if not info.get("type") or info["type"] == "Informal and Other Publications":
            continue
        if not _is_published_venue(str(info.get("venue", ""))):
            continue
        authors = _dblp_hit_authors(info)
        if author_hint and not any(
            mini_hash(author_hint) == first_author_last_name(name) for name in authors
        ):
            continue
        match = _dblp_match(info, "dblp")
        _log(f"[dblp] match: {match.venue} {info.get('year', '')}")
        return match
    if len(hits) >= 101:
        raise TransientSourceError("dblp title candidate window exceeded")
    return None


def _dblp_hit_authors(info: dict) -> list[str]:
    authors = (info.get("authors") or {}).get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    return [
        re.sub(r"\s+\d{4}$", "", a.get("text", ""))
        for a in authors
        if isinstance(a, dict) and a.get("text")
    ]


def try_dblp_fuzzy(title: str, author_hint: str, year: str = "") -> Match | None:
    """Title-drift fallback: camera-ready titles often differ from the arXiv
    ones ("Information-Theoretic" -> "Information Theory"), and DBLP's
    token-AND search then misses entirely. Query the most distinctive
    title tokens instead, and accept token-Jaccard-similar
    titles — guarded by author and year so different papers can't sneak in.
    """
    if not author_hint:
        return None
    tokens = sorted(sig_tokens(title), key=lambda t: (-len(t), t))[:3]
    if not tokens:
        return None
    q = " ".join(tokens)
    with _client() as c:
        hits = _dblp_title_search(c, q)
        hits.sort(key=lambda h: int(h.get("info", {}).get("year", 9999)))
        for hit in hits:
            info = hit.get("info", {})
            hit_title = clean_title(html.unescape(info.get("title", "")))
            if (
                not info.get("type")
                or info["type"] == "Informal and Other Publications"
            ):
                continue
            if not _is_published_venue(str(info.get("venue", ""))):
                continue
            if not titles_similar(hit_title, title):
                continue
            if year and info.get("year"):
                if abs(int(info["year"]) - int(year)) > 2:
                    continue
            if not any(
                mini_hash(author_hint) == first_author_last_name(name)
                for name in _dblp_hit_authors(info)
            ):
                continue
            match = _dblp_match(info, "dblp-fuzzy")
            venue = match.venue
            _log(
                f"[dblp-fuzzy] match with title drift: '{hit_title}' "
                f"@ {venue} {info.get('year', '')}"
            )
            return match
    if len(hits) >= 101:
        raise TransientSourceError("dblp title candidate window exceeded")
    return None


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

S2_FIELDS = "title,venue,year,authors,externalIds,url"


def _s2_to_match(data: dict, ref_title: str, ref_year: str) -> Match | None:
    venue = (data.get("venue") or "").strip()
    if not _is_published_venue(venue):
        return None
    if norm_title(data.get("title", "")) != norm_title(ref_title):
        return None
    year = data.get("year")
    if ref_year and year and abs(int(year) - int(ref_year)) >= 3:
        return None
    venue = re.sub(r"^\d{4}\s*", "", venue).strip()
    if " " not in venue:
        venue = venue.upper()
    doi = (data.get("externalIds") or {}).get("DOI", "") or ""
    if ARXIV_DOI.match(doi):
        doi = ""
    _log(f"[semanticscholar] match: {venue} {year}")
    return Match(
        source="semanticscholar",
        venue=venue,
        title=clean_title(data.get("title", "")),
        year=str(year or ""),
        authors=[a["name"] for a in data.get("authors") or []],
        doi=doi,
        url=data.get("url", ""),
    )


def arxiv_abs_metadata(arxiv_id: str) -> ArxivMeta | None:
    """Scrape the arxiv.org abs page's Highwire meta tags — the abs pages stay
    up when the export API throttles."""
    with _client(browser=True) as c:
        r = _get(c, f"https://arxiv.org/abs/{arxiv_id}")
        if r.status_code != 200:
            return None
        page = r.text

    def metas(name: str) -> list[str]:
        return [
            html.unescape(m)
            for m in re.findall(rf'<meta\s+name="{name}"\s+content="([^"]*)"', page)
        ]

    titles = metas("citation_title")
    if not titles:
        return None
    dates = metas("citation_date")
    return ArxivMeta(
        arxiv_id=arxiv_id,
        title=clean_title(titles[0]),
        authors=metas("citation_author"),
        year=dates[0][:4] if dates else "",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def _s2_pacing_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home) if cache_home else Path.home() / ".cache"
    return root / "bibcite" / "requests.sqlite3"


def _s2_gate(key: str) -> None:
    """Reserve one S2 dispatch slot using the Rust-compatible SQLite protocol."""
    started = time.monotonic()
    publication_deadline = getattr(_REQUEST_DEADLINE, "value", None)
    gate_deadline = started + 2.2
    if publication_deadline is not None:
        gate_deadline = min(gate_deadline, publication_deadline)
    remaining = gate_deadline - time.monotonic()
    if remaining <= 0:
        raise PublicationTimeout("publication lookup timed out before S2 pacing")

    path = _s2_pacing_path()
    connection = None
    in_transaction = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=remaining)
        busy_ms = max(1, min(2200, int(remaining * 1000)))
        connection.execute(f"PRAGMA busy_timeout = {busy_ms}")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS s2_pacing "
            "(key TEXT PRIMARY KEY, last_dispatch INTEGER NOT NULL)"
        )
        remaining = gate_deadline - time.monotonic()
        if remaining <= 0:
            raise SourceUnavailable("Semantic Scholar pacing deadline exhausted")
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(remaining * 1000))}")
        connection.execute("BEGIN IMMEDIATE")
        in_transaction = True
        row = connection.execute(
            "SELECT last_dispatch FROM s2_pacing WHERE key = ?", (key,)
        ).fetchone()
        now_ms = int(time.time() * 1000)
        wait = max(0.0, ((row[0] + 1100) - now_ms) / 1000) if row else 0.0
        if time.monotonic() + wait > gate_deadline:
            raise SourceUnavailable("Semantic Scholar pacing deadline exhausted")
        if wait:
            time.sleep(wait)
        if time.monotonic() > gate_deadline:
            raise SourceUnavailable("Semantic Scholar pacing deadline exhausted")
        dispatch_ms = int(time.time() * 1000)
        connection.execute(
            "INSERT INTO s2_pacing(key, last_dispatch) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_dispatch = excluded.last_dispatch",
            (key, dispatch_ms),
        )
        connection.commit()
        in_transaction = False
    except PublicationTimeout:
        raise
    except (OSError, sqlite3.Error) as e:
        raise SourceUnavailable(
            f"Semantic Scholar pacing unavailable ({type(e).__name__})"
        ) from e
    finally:
        if connection is not None:
            if in_transaction:
                connection.rollback()
            connection.close()


def _s2_get(c: httpx.Client, url: str, params: dict) -> httpx.Response:
    if _s2_batch_status() is not None:
        raise SourceUnavailable("Semantic Scholar batch status already supplied")

    key = os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    # A keyless public-service request is paced by the service itself. Personal
    # and direct anonymous traffic coordinate with the Rust app by key hash.
    if key or not os.environ.get("BIBCITE_PUBLIC_SERVICE_URL"):
        pacing_key = hashlib.sha256(key.encode()).hexdigest() if key else "anonymous"
        _s2_gate(pacing_key)
    try:
        response = _get(c, url, params=params, headers=_s2_headers())
    except httpx.HTTPError as e:
        raise TransientSourceError(
            f"semanticscholar unreachable ({type(e).__name__})"
        ) from e
    if response.status_code == 429:
        raise SourceUnavailable("semanticscholar rate-limited (429)")
    if response.status_code >= 500:
        raise TransientSourceError(
            f"semanticscholar server error ({response.status_code})"
        )
    return response


def s2_arxiv_metadata(arxiv_id: str) -> ArxivMeta | None:
    """Metadata (title/authors/year) for an arXiv id via Semantic Scholar —
    the fallback when export.arxiv.org itself is throttled."""
    with _client() as c:
        r = _s2_get(
            c,
            f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}",
            params={"fields": "title,year,authors"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
    if not data.get("title"):
        return None
    return ArxivMeta(
        arxiv_id=arxiv_id,
        title=clean_title(data["title"]),
        authors=[a["name"] for a in data.get("authors") or []],
        year=str(data.get("year") or ""),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def try_semantic_scholar(
    title: str, year: str = "", arxiv_id: str = ""
) -> Match | None:
    with _client() as c:
        # Direct id lookup first: unambiguous, no title-search needed.
        if arxiv_id:
            r = _s2_get(
                c,
                f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}",
                params={"fields": S2_FIELDS},
            )
            if r.status_code == 200:
                m = _s2_to_match(r.json(), title, year)
                if m:
                    return m
        r = _s2_get(
            c,
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": title, "fields": S2_FIELDS, "limit": 5},
        )
        r.raise_for_status()
        for item in r.json().get("data") or []:
            m = _s2_to_match(item, title, year)
            if m:
                return m
    return None


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------


def try_crossref(title: str) -> Match | None:
    with _client() as c:
        r = _get(
            c,
            "https://api.crossref.org/works",
            params={
                "rows": 3,
                "query.title": title,
                "select": "title,event,container-title,DOI,issued",
                "mailto": _mailto(),
            },
        )
        if r.status_code == 429:
            raise SourceUnavailable("CrossRef rate-limited (429)")
        if r.status_code >= 500:
            # A dead endpoint gets benched for the run instead of adding
            # latency and noise to every remaining query.
            raise SourceUnavailable(f"CrossRef server error ({r.status_code})")
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "ok":
            return None
        ref = norm_title(title)
        for item in payload["message"].get("items", []):
            titles = item.get("title") or []
            if not titles or norm_title(titles[0]) != ref:
                continue
            event = (item.get("event") or {}).get("name", "")
            container = (item.get("container-title") or [""])[0]
            venue = (event or container).strip()
            if not _is_published_venue(venue):
                continue
            doi = item.get("DOI", "")
            if ARXIV_DOI.match(doi):
                continue
            year = ""
            parts = (item.get("issued") or {}).get("date-parts") or []
            if parts and parts[0]:
                year = str(parts[0][0])
            bibtex = ""
            if doi:
                br = _get(
                    c,
                    f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex",
                )
                if br.status_code == 200:
                    bibtex = br.text
            _log(f"[crossref] match: {venue} {year}")
            return Match(
                source="crossref",
                venue=venue,
                title=clean_title(titles[0]),
                year=year,
                doi=doi,
                bibtex=bibtex,
            )
    return None


# ---------------------------------------------------------------------------
# OpenAlex (not in PaperMemory; unauthenticated with generous rate limits, so
# it doubles as the metadata fallback when the arXiv API / S2 are throttled)
# ---------------------------------------------------------------------------


def openalex_search(title: str) -> dict | None:
    """OpenAlex work with an exactly-matching normalized title, or None."""
    with _client() as c:
        r = _get(
            c,
            "https://api.openalex.org/works",
            params=_openalex_params({"search": title, "per-page": 5}),
        )
        if r.status_code == 429:
            raise SourceUnavailable("OpenAlex rate-limited (429)")
        if r.status_code >= 500:
            # A dead endpoint gets benched for the run instead of adding
            # latency and noise to every remaining query.
            raise SourceUnavailable(f"OpenAlex server error ({r.status_code})")
        r.raise_for_status()
        ref = norm_title(title)
        for w in r.json().get("results") or []:
            if norm_title(w.get("title") or "") == ref:
                return w
    return None


def openalex_arxiv_id(work: dict) -> str:
    for loc in work.get("locations") or []:
        for f in ("landing_page_url", "pdf_url"):
            m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", loc.get(f) or "")
            if m:
                return m.group(1)
    return ""


def openalex_authors(work: dict) -> list[str]:
    return [
        a["author"]["display_name"]
        for a in work.get("authorships") or []
        if a.get("author", {}).get("display_name")
    ]


def try_openalex(title: str) -> Match | None:
    work = openalex_search(title)
    if not work:
        return None
    venue = ""
    locations = [work.get("primary_location") or {}] + (work.get("locations") or [])
    for loc in locations:
        src = loc.get("source") or {}
        name = (src.get("display_name") or "").strip()
        if src.get("type") != "repository" and _is_published_venue(name):
            venue = name
            break
    if not venue:
        return None
    doi = re.sub(r"^https://doi\.org/", "", work.get("doi") or "")
    if ARXIV_DOI.match(doi):
        doi = ""
    _log(f"[openalex] match: {venue} {work.get('publication_year', '')}")
    return Match(
        source="openalex",
        venue=venue,
        title=clean_title(work.get("title") or ""),
        year=str(work.get("publication_year") or ""),
        authors=openalex_authors(work),
        doi=doi,
    )


# ---------------------------------------------------------------------------
# CrossRef by DOI (for `bibcite add file 10.xxxx/yyy`)
# ---------------------------------------------------------------------------


def crossref_by_doi(doi: str) -> Match | None:
    with _client() as c:
        r = _get(
            c, f"https://api.crossref.org/works/{doi}", params={"mailto": _mailto()}
        )
        if r.status_code != 200:
            return None
        data = r.json().get("message", {})
        titles = data.get("title") or []
        event = (data.get("event") or {}).get("name", "")
        container = (data.get("container-title") or [""])[0]
        year = ""
        parts = (data.get("issued") or {}).get("date-parts") or []
        if parts and parts[0]:
            year = str(parts[0][0])
        bibtex = ""
        br = _get(
            c, f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex"
        )
        if br.status_code == 200:
            bibtex = br.text
        authors = [
            " ".join(filter(None, [a.get("given"), a.get("family")]))
            for a in data.get("author") or []
        ]
        return Match(
            source="crossref",
            venue=(event or container).strip(),
            title=clean_title(titles[0]) if titles else "",
            year=year,
            authors=[a for a in authors if a],
            doi=doi,
            bibtex=bibtex,
        )


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------

CASCADE = (
    ("dblp", lambda t, y, a, au: try_dblp(t, au)),
    ("semanticscholar", lambda t, y, a, au: try_semantic_scholar(t, y, a)),
    ("crossref", lambda t, y, a, au: try_crossref(t)),
    ("openalex", lambda t, y, a, au: try_openalex(t)),
)

# Sources that rate-limited or blocked us in this process: skip them for the
# rest of the run instead of hammering them once per entry during batch
# `upgrade` (PaperMemory's DISABLE_MATCH, ported). Exhausted transport retries
# do not enter this circuit breaker because the next entry may succeed.
_DISABLED: dict[str, str] = {}

# Only these sources are authoritative enough that losing one taints a miss
# into "incomplete".
# Override with BIBCITE_CORE_SOURCES="dblp,semanticscholar" if one of these
# is down for days and keeps every verdict incomplete.
CORE_SOURCES = frozenset(
    s.strip()
    for s in (
        os.environ.get("BIBCITE_CORE_SOURCES")
        or "dblp,semanticscholar,crossref,openalex"
    ).split(",")
    if s.strip()
)


def _try_dblp_stage(deadline: float, retry_state: dict, stage: str, fn, *args):
    """Run a DBLP stage, sharing one timeout retry across the whole lookup."""
    # Reserve time for the retry instead of letting the first timeout consume
    # the entire lookup budget and making the second attempt a no-op.
    first_deadline = deadline if retry_state["used"] else min(
        deadline, time.monotonic() + 2.0
    )
    try:
        return _with_deadline(first_deadline, fn, *args)
    except (DblpTimeout, PublicationTimeout) as e:
        if retry_state["used"]:
            raise TransientSourceError(
                f"dblp {stage} timed out (timeout retry already used)"
            ) from e
        retry_state["used"] = True
        _log(f"[dblp-{stage}] request timed out; retrying once")
        try:
            return _with_deadline(deadline, fn, *args)
        except (DblpTimeout, PublicationTimeout) as e:
            raise TransientSourceError(
                f"dblp {stage} timed out after 2 attempts"
            ) from e


def find_published(
    title: str, year: str = "", arxiv_id: str = "", author_hint: str = ""
) -> tuple[Match | None, str]:
    """Try each source in order; first verified hit wins.

    Returns (match, status):
      "found"       — verified publication match
      "not_found"   — EVERY source answered cleanly with no hit; trustworthy
      "incomplete"  — some sources answered (no hit) but others were
                      disabled/erroring; a batch run that tripped DBLP's rate
                      limit lands here — do NOT conclude "unpublished"
      "unavailable" — no source answered at all
    """
    from . import cache

    cache_key = norm_title(title)
    cached = cache.get(cache_key)
    if cached:
        _log(f"[cache] hit: {cached.get('venue', '')} ({cached.get('source', '')})")
        return Match(**cached), "found"

    # Core sources lost earlier in this run taint this query's verdict too.
    incomplete = any(n in CORE_SOURCES for n in _DISABLED)
    batch_status = _s2_batch_status()
    if batch_status == "checked":
        _log("[semanticscholar] batch result reused")
        incomplete = True
    elif batch_status == "unavailable":
        _log("[semanticscholar] batch unavailable")
        incomplete = True
    elif batch_status == "disabled":
        # The caller intentionally excluded S2. It is neither an attempted
        # source nor a failed one, so clean misses from the enabled core
        # sources remain authoritative.
        _log("[semanticscholar] disabled by caller")
    # Query every still-viable source concurrently. A preprint with no published
    # version (the common case) misses everywhere, and used to pay the *sum* of
    # each source's latency; now the wall-clock is the slowest single source.
    # The first verified hit by CASCADE priority still wins.
    started = time.monotonic()
    deadline = started + PUBLICATION_TIMEOUT
    dblp_deadline = min(deadline, started + 4.0)
    dblp_retry = {"used": False}
    active = [
        (name, fn)
        for name, fn in CASCADE
        if name not in _DISABLED and not (name == "semanticscholar" and batch_status)
    ]
    outcomes: dict[str, tuple] = {}
    if active:
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = {
                pool.submit(
                    _try_dblp_stage if name == "dblp" else _with_deadline,
                    dblp_deadline if name == "dblp" else deadline,
                    *(
                        (dblp_retry, "exact", fn, title, year, arxiv_id, author_hint)
                        if name == "dblp"
                        else (fn, title, year, arxiv_id, author_hint)
                    ),
                ): name
                for name, fn in active
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    m = future.result()
                    if m:
                        outcomes[name] = ("found", m)
                        _log(f"[{name}] publication matched")
                    else:
                        outcomes[name] = ("miss", None)
                        _log(f"[{name}] no publication found")
                except TransientSourceError as e:
                    outcomes[name] = ("fail", name in CORE_SOURCES)
                    _log(f"[{name}] transient failure for this entry: {e}")
                except SourceUnavailable as e:
                    _DISABLED[name] = str(e)
                    outcomes[name] = ("fail", name in CORE_SOURCES)
                    _log(f"[{name}] disabled for the rest of this run: {e}")
                except Exception as e:  # a hiccup on one source must not kill the run
                    outcomes[name] = ("fail", name in CORE_SOURCES)
                    _log(f"[{name}] error: {type(e).__name__}: {e}")

    # Prefer the highest-priority source that verified a match.
    for name, _ in CASCADE:
        outcome = outcomes.get(name)
        if outcome and outcome[0] == "found":
            cache.put(cache_key, outcome[1].__dict__)
            return outcome[1], "found"

    clean_misses = sum(1 for outcome in outcomes.values() if outcome[0] == "miss")
    incomplete = incomplete or any(
        outcome[0] == "fail" and outcome[1] for outcome in outcomes.values()
    )

    # Exact-title search missed everywhere. Before concluding "no published
    # version", try the title-drift fallback — camera-ready titles frequently
    # differ from the arXiv ones, which is precisely the upgrade scenario.
    # Only a clean exact DBLP miss justifies another query. A timeout or other
    # failure has already spent its chance for this entry, and retrying the
    # fuzzy form was doubling the worst-case interactive latency.
    dblp_outcome = outcomes.get("dblp")
    if author_hint and dblp_outcome is not None and dblp_outcome[0] == "miss":
        if time.monotonic() >= dblp_deadline:
            incomplete = True
            _log("[dblp-fuzzy] skipped: DBLP lookup budget exhausted")
        else:
            try:
                m = _try_dblp_stage(
                    dblp_deadline,
                    dblp_retry,
                    "fuzzy",
                    try_dblp_fuzzy,
                    title,
                    author_hint,
                    year,
                )
                if m:
                    cache.put(cache_key, m.__dict__)
                    return m, "found"
                clean_misses += 1
            except TransientSourceError as e:
                incomplete = True
                _log(f"[dblp-fuzzy] transient failure for this entry: {e}")
            except SourceUnavailable as e:
                _DISABLED["dblp"] = str(e)
                incomplete = True
            except Exception as e:
                incomplete = True
                _log(f"[dblp-fuzzy] error: {type(e).__name__}: {e}")
    if not clean_misses:
        return None, "unavailable"
    return None, ("incomplete" if incomplete else "not_found")


@dataclass
class WebPage:
    """What a web page can say about itself, for citing it as an @misc."""

    url: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    site: str = ""


def _meta_content(html_text: str, *keys: str) -> str:
    """The content of the first <meta> whose name/property matches a key.

    Attribute order varies between generators, so both orders are tried rather
    than assuming content comes last.
    """
    for key in keys:
        for pattern in (
            rf'<meta[^>]+(?:name|property)=["\']{re.escape(key)}["\'][^>]*'
            rf'content=["\'](.*?)["\']',
            rf'<meta[^>]+content=["\'](.*?)["\'][^>]*'
            rf'(?:name|property)=["\']{re.escape(key)}["\']',
        ):
            m = re.search(pattern, html_text, re.I | re.S)
            if m and m.group(1).strip():
                return html.unescape(m.group(1).strip())
    return ""


def fetch_web_page(url: str) -> WebPage:
    """Read a page's own description of itself.

    Blogs, documentation and standards pages are cited constantly and are in
    none of the academic indexes, so there is nothing to look them up in — the
    page itself is the only source. Highwire and Dublin Core tags come first
    because sites that carry them mean them; Open Graph and <title> are the
    fallback every site has.
    """
    try:
        with _client(browser=True) as client:
            response = _get(client, url)
            response.raise_for_status()
            body = response.text[:400_000]
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        # 403 and 404 are settled answers: the page will not become readable on
        # a retry, so say so rather than sending the caller back to wait.
        if status in (401, 403, 404, 410) or 400 <= status < 500:
            raise PageUnreadable(
                f"the page answered {status} — cite it with --bibtex, or use its DOI if it has one"
            ) from e
        raise SourceUnavailable(f"could not fetch {url}: {e}") from e
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"could not fetch {url}: {e}") from e

    title = _meta_content(
        body, "citation_title", "DC.title", "og:title", "twitter:title"
    ) or _title_tag(body)
    authors = [
        author
        for author in (
            _meta_content(
                body, "citation_author", "DC.creator", "author", "article:author"
            ),
        )
        if author
    ]
    date = _meta_content(
        body,
        "citation_publication_date",
        "citation_date",
        "DC.date",
        "article:published_time",
        "og:updated_time",
        "date",
    )
    year = _year_in(date) or _year_in_path(url)
    site = _meta_content(body, "og:site_name") or _site_author(_host(url))
    return WebPage(
        url=str(response.url), title=title, authors=authors, year=year, site=site
    )


def _year_in(text: str) -> str:
    m = re.search(r"(?:19|20)\d{2}", text)
    return m.group(0) if m else ""


def _year_in_path(url: str) -> str:
    """The year a dateless page puts in its own URL.

    Blog engines write `/2015/05/21/title`, and a citation key of
    `karpathyXXXXunreasonable` is worse than one carrying the year the post
    announces about itself. Only the path is read: a query string can hold any
    number at all.
    """
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0]
    for segment in path.split("/"):
        if re.fullmatch(r"(?:19|20)\d{2}", segment):
            return segment
    return ""


def _title_tag(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if not m:
        return ""
    # Strip the trailing " — Site Name" many templates append; the site name is
    # recorded separately, and repeating it in the title reads badly in a
    # bibliography.
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return re.sub(r"\s*[|·—–-]\s*[^|·—–-]{1,40}$", "", title).strip() or title


def _host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/:]+)", url, re.I)
    return m.group(1) if m else ""


def _site_author(host: str) -> str:
    """A readable stand-in author for a page with no byline.

    The bare host makes an unreadable key — `karpathygithubioXXXXunreasonable`
    — so the hosting suffix goes and the name that identifies the site stays:
    `karpathy.github.io` reads as Karpathy, `docs.python.org` as Python.
    """
    labels = [label for label in host.lower().split(".") if label]
    if not labels:
        return host
    generic = {
        "github",
        "io",
        "com",
        "org",
        "net",
        "edu",
        "gov",
        "ai",
        "dev",
        "co",
        "uk",
        "cn",
        "blog",
        "www",
        "docs",
        "pages",
        "medium",
    }
    named = [label for label in labels if label not in generic]
    return (named[0] if named else labels[0]).capitalize()
