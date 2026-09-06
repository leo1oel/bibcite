"""API clients for the publication-matching cascade.

Order and matching rules ported from PaperMemory's bibMatcher:
DBLP -> Semantic Scholar -> CrossRef -> OpenAlex.
All matchers verify identity via normalized-title equality and reject
preprint venues (arXiv / CoRR / bioRxiv / ...).
"""

import html
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .normalize import clean_title, mini_hash, norm_title, sig_tokens, titles_similar

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
            os.environ.get("S2_API_KEY")
            or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
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

# Client-side pacing + backoff-retry, shared by the throttle-prone sources.
# Pacing prevents the 429 in the first place; on a 429 we back off (honoring
# Retry-After) instead of instantly poisoning the rest of a batch run — only
# repeated failure raises SourceUnavailable (which disables the source).
_LAST_REQUEST: dict[str, float] = {}


def _paced_get(
    c: httpx.Client,
    url: str,
    source: str,
    min_interval: float,
    params: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    for attempt in range(2):
        wait = min_interval - (time.monotonic() - _LAST_REQUEST.get(source, 0.0))
        if wait > 0:
            _sleep(wait)
        _LAST_REQUEST[source] = time.monotonic()
        try:
            r = _get(c, url, params=params, headers=headers)
        except httpx.HTTPError as e:  # Retry transport errors once before failing.
            if attempt < 1:
                _sleep(1)
                continue
            raise TransientSourceError(
                f"{source} unreachable ({type(e).__name__})"
            ) from e
        if r.status_code == 429:
            # A 429 is usually a persistent rate-limit (e.g. the shared
            # unauthenticated Semantic Scholar pool), not a transient blip, so a
            # long client-side backoff rarely clears it and just stalls an
            # interactive resolve. Take at most one quick retry when the server
            # asks for a short wait, then give up and let the circuit breaker
            # skip this source for the rest of the run.
            retry_after = int(r.headers.get("Retry-After") or 0)
            if attempt < 1 and retry_after <= 2:
                _sleep(max(retry_after, 1))
                continue
            raise SourceUnavailable(f"{source} rate-limited (429)")
        return r
    raise SourceUnavailable(f"{source} unavailable")


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
    except httpx.HTTPError as e:
        raise TransientSourceError(f"dblp unreachable ({type(e).__name__})") from e
    if response.status_code == 429:
        raise SourceUnavailable("dblp rate-limited (429)")
    if response.status_code >= 500:
        raise TransientSourceError(f"dblp server error ({response.status_code})")
    return response


def _dblp_sanitize(q: str) -> str:
    """DBLP's search parser 500s deterministically on queries containing its
    syntax characters (':' in subtitled papers, '?' in question titles).
    They tokenize on punctuation anyway, so replacing with spaces loses
    nothing."""
    return re.sub(r"[^\w\s.-]", " ", q)


def _dblp_search(c: httpx.Client, q: str, h: int = 100) -> list:
    """Run one DBLP search while retaining the full result window."""
    url = "https://dblp.org/search/publ/api"
    q = _dblp_sanitize(q)
    r = _dblp_get(c, url, params={"q": q, "format": "json", "h": h})
    r.raise_for_status()
    return r.json().get("result", {}).get("hits", {}).get("hit", []) or []


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
    """DBLP search. Generic titles ("X is all you need") drown in DBLP's
    ranking, so when we know the first author we query with their last name
    first, then fall back to the bare title."""
    queries = []
    if author_hint:
        queries.append(f"{title} {author_hint}")
    queries.append(title)
    with _client() as c:
        for q in queries:
            hits = _dblp_search(c, q)
            # Earliest year first: prefer the original conference publication
            # over later journal extensions (same heuristic as PaperMemory).
            hits.sort(key=lambda h: int(h.get("info", {}).get("year", 9999)))
            ref = norm_title(title)
            for hit in hits:
                info = hit.get("info", {})
                if norm_title(html.unescape(info.get("title", ""))) != ref:
                    continue
                if info.get("venue") == "CoRR" or not info.get("venue"):
                    continue
                match = _dblp_match(info, "dblp")
                venue = match.venue
                _log(f"[dblp] match: {venue} {info.get('year', '')}")
                return match
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
    token-AND search then misses entirely. Query author + the most
    distinctive title tokens instead, and accept token-Jaccard-similar
    titles — guarded by author and year so different papers can't sneak in.
    """
    if not author_hint:
        return None
    tokens = sorted(sig_tokens(title), key=len, reverse=True)[:3]
    if not tokens:
        return None
    q = " ".join([author_hint] + tokens)
    with _client() as c:
        hits = _dblp_search(c, q)
        hits.sort(key=lambda h: int(h.get("info", {}).get("year", 9999)))
        for hit in hits:
            info = hit.get("info", {})
            hit_title = clean_title(html.unescape(info.get("title", "")))
            if info.get("venue") == "CoRR" or not info.get("venue"):
                continue
            if not titles_similar(hit_title, title):
                continue
            if year and info.get("year"):
                if abs(int(info["year"]) - int(year)) > 2:
                    continue
            hit_authors = mini_hash(" ".join(_dblp_hit_authors(info)))
            if author_hint not in hit_authors:
                continue
            match = _dblp_match(info, "dblp-fuzzy")
            venue = match.venue
            _log(
                f"[dblp-fuzzy] match with title drift: '{hit_title}' "
                f"@ {venue} {info.get('year', '')}"
            )
            return match
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
            for m in re.findall(
                rf'<meta\s+name="{name}"\s+content="([^"]*)"', page
            )
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


def _s2_get(c: httpx.Client, url: str, params: dict) -> httpx.Response:
    # With an API key S2 allows ~1 req/s on a private quota; unauthenticated
    # requests share a global pool where backoff still beats instant defeat.
    return _paced_get(
        c, url, "semanticscholar", 1.0, params=params, headers=_s2_headers()
    )


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
            m = re.search(
                r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", loc.get(f) or ""
            )
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
        os.environ.get("BIBCITE_CORE_SOURCES") or "dblp,semanticscholar,crossref,openalex"
    ).split(",")
    if s.strip()
)


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
    # Query every still-viable source concurrently. A preprint with no published
    # version (the common case) misses everywhere, and used to pay the *sum* of
    # each source's latency; now the wall-clock is the slowest single source.
    # The first verified hit by CASCADE priority still wins.
    started = time.monotonic()
    deadline = started + PUBLICATION_TIMEOUT
    dblp_deadline = min(deadline, started + 4.0)
    active = [(name, fn) for name, fn in CASCADE if name not in _DISABLED]
    outcomes: dict[str, tuple] = {}
    if active:
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = {
                pool.submit(
                    _with_deadline,
                    dblp_deadline if name == "dblp" else deadline,
                    fn,
                    title,
                    year,
                    arxiv_id,
                    author_hint,
                ): name
                for name, fn in active
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    m = future.result()
                    if m:
                        outcomes[name] = ("found", m)
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
    if (
        author_hint
        and dblp_outcome is not None
        and dblp_outcome[0] == "miss"
    ):
        if time.monotonic() >= dblp_deadline:
            incomplete = True
            _log("[dblp-fuzzy] skipped: DBLP lookup budget exhausted")
        else:
            try:
                m = _with_deadline(
                    dblp_deadline, try_dblp_fuzzy, title, author_hint, year
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

    title = (
        _meta_content(body, "citation_title", "DC.title", "og:title", "twitter:title")
        or _title_tag(body)
    )
    authors = [
        author
        for author in (
            _meta_content(body, "citation_author", "DC.creator", "author", "article:author"),
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
    return WebPage(url=str(response.url), title=title, authors=authors, year=year, site=site)


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
    generic = {"github", "io", "com", "org", "net", "edu", "gov", "ai", "dev",
               "co", "uk", "cn", "blog", "www", "docs", "pages", "medium"}
    named = [label for label in labels if label not in generic]
    return (named[0] if named else labels[0]).capitalize()
