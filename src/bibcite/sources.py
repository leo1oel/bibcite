"""API clients for the publication-matching cascade.

Order and matching rules ported from PaperMemory's bibMatcher:
DBLP -> Semantic Scholar -> Google Scholar -> CrossRef -> Unpaywall.
All matchers verify identity via normalized-title equality and reject
preprint venues (arXiv / CoRR / bioRxiv / ...).
"""

import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from .normalize import clean_title, mini_hash, norm_title, sig_tokens, titles_similar

UA = "bibcite/0.5 (https://github.com/leo1oel/bibcite)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0

PREPRINT_VENUES = re.compile(r"arxiv|corr|biorxiv|medrxiv|chemrxiv|ssrn|preprint", re.I)
ARXIV_DOI = re.compile(r"^10\.48550/", re.I)


def _log(msg: str):
    print(msg, file=sys.stderr)


class SourceUnavailable(Exception):
    """Raised when a source rate-limits/blocks us; the cascade skips it."""


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
    """Contact email for the polite pools (CrossRef/OpenAlex/Unpaywall).
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
                r = c.get(
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
    for attempt in range(3):
        wait = min_interval - (time.monotonic() - _LAST_REQUEST.get(source, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[source] = time.monotonic()
        try:
            r = c.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:  # TCP reset = temporary ban; retrying fast makes it worse
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise SourceUnavailable(f"{source} unreachable ({type(e).__name__})")
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After") or 0)
            if retry_after > 30:
                raise SourceUnavailable(f"{source} rate-limited (Retry-After {retry_after}s)")
            if attempt < 2:
                delay = max(retry_after, 4 * (attempt + 1))
                _log(f"[{source}] 429 — backing off {delay}s")
                time.sleep(delay)
                continue
            raise SourceUnavailable(f"{source} rate-limited (429) after backoff retries")
        return r
    raise SourceUnavailable(f"{source} unavailable")


# DBLP throttles at roughly 1-2 req/s and escalates to temporary IP bans.
def _dblp_get(c: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    return _paced_get(c, url, "dblp", 0.8, params=params)


def _dblp_sanitize(q: str) -> str:
    """DBLP's search parser 500s deterministically on queries containing its
    syntax characters (':' in subtitled papers, '?' in question titles).
    They tokenize on punctuation anyway, so replacing with spaces loses
    nothing."""
    return re.sub(r"[^\w\s.-]", " ", q)


def _dblp_search(c: httpx.Client, q: str, h: int = 100) -> list:
    """One search request; a 500 (broad all-common-words query × large h
    times out their backend) is retried once with a small result window
    before giving up on this query variant."""
    url = "https://dblp.org/search/publ/api"
    q = _dblp_sanitize(q)
    r = _dblp_get(c, url, params={"q": q, "format": "json", "h": h})
    if r.status_code == 500 and h > 10:
        _log("[dblp] 500 on broad query — retrying with h=10")
        r = _dblp_get(c, url, params={"q": q, "format": "json", "h": 10})
    r.raise_for_status()
    return r.json().get("result", {}).get("hits", {}).get("hit", []) or []


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
                venue = info["venue"]
                if isinstance(venue, list):
                    venue = venue[0]
                bibtex = ""
                if info.get("url"):
                    try:
                        br = _dblp_get(c, info["url"] + ".bib")
                        if br.status_code == 200:
                            bibtex = br.text
                    except SourceUnavailable:
                        pass  # keep the match; construct from fields
                _log(f"[dblp] match: {venue} {info.get('year', '')}")
                return Match(
                    source="dblp",
                    venue=str(venue),
                    title=clean_title(html.unescape(info.get("title", ""))),
                    year=str(info.get("year", "")),
                    doi=info.get("doi", ""),
                    bibtex=bibtex,
                    url=info.get("ee", "") or info.get("url", ""),
                )
    return None


def _dblp_hit_authors(info: dict) -> list[str]:
    authors = (info.get("authors") or {}).get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    return [a.get("text", "") for a in authors if isinstance(a, dict)]


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
            venue = info["venue"]
            if isinstance(venue, list):
                venue = venue[0]
            bibtex = ""
            if info.get("url"):
                try:
                    br = _dblp_get(c, info["url"] + ".bib")
                    if br.status_code == 200:
                        bibtex = br.text
                except SourceUnavailable:
                    pass  # keep the match; construct from fields
            _log(
                f"[dblp-fuzzy] match with title drift: '{hit_title}' "
                f"@ {venue} {info.get('year', '')}"
            )
            return Match(
                source="dblp-fuzzy",
                venue=str(venue),
                title=hit_title,
                year=str(info.get("year", "")),
                doi=info.get("doi", ""),
                bibtex=bibtex,
                url=info.get("ee", "") or info.get("url", ""),
            )
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
        r = c.get(f"https://arxiv.org/abs/{arxiv_id}")
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
# Google Scholar (port of PaperMemory's background fetchGSData)
# ---------------------------------------------------------------------------

def try_google_scholar(title: str) -> Match | None:
    with _client(browser=True) as c:
        r = c.get(
            "https://scholar.google.com/scholar",
            params={"q": title, "hl": "en"},
        )
        if r.status_code == 429 or "captcha" in r.text.lower()[:5000]:
            raise SourceUnavailable("Google Scholar is blocking requests (captcha/429)")
        r.raise_for_status()
        parts = r.text.split("gs_res_ccl_mid")
        if len(parts) < 2:
            return None
        page = parts[1]
        # Each result title anchor looks like <a id="DATAID" href=...>Title</a>
        # (the title may contain <b> highlights and HTML entities).
        data_id = ""
        for am in re.finditer(
            r'<a[^>]*\bid="([\w-]{6,40})"[^>]*>(.*?)</a>', page, re.S
        ):
            text = html.unescape(re.sub(r"<[^>]+>", "", am.group(2)))
            if norm_title(text) == norm_title(title):
                data_id = am.group(1)
                break
        if not data_id:
            return None
        cite_url = (
            "https://scholar.google.com/scholar?q=info:"
            f"{data_id}:scholar.google.com/&output=cite&scirp=0&hl=en"
        )
        cite_html = c.get(cite_url).text
        bm = re.search(r'<a[^>]*href="([^">]+)"[^>]*>BibTex</a>', cite_html, re.I)
        if not bm:
            return None
        bib_url = re.sub(r"\s+", "", bm.group(1).replace("&amp;", "&"))
        bibtex = c.get(bib_url).text
    from .bibfile import parse_bibtex_entry  # local import to avoid cycle

    entry = parse_bibtex_entry(bibtex)
    venue = entry.get("journal", "") or entry.get("booktitle", "")
    if venue and not venue.lower().endswith("xiv") and "preprint" not in venue.lower():
        _log(f"[googlescholar] match: {venue}")
        return Match(
            source="googlescholar",
            venue=venue,
            title=clean_title(entry.get("title", title)),
            year=entry.get("year", ""),
            bibtex=bibtex,
        )
    return None


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------

def try_crossref(title: str) -> Match | None:
    with _client() as c:
        r = c.get(
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
            # A dead endpoint (Unpaywall search 500s for days at a time)
            # gets benched for the run instead of adding latency + noise
            # to every remaining query.
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
                br = c.get(
                    f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex"
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
# Unpaywall
# ---------------------------------------------------------------------------

def try_unpaywall(title: str) -> Match | None:
    with _client() as c:
        r = c.get(
            "https://api.unpaywall.org/v2/search",
            params={"query": title, "is_oa": "true", "email": _mailto()},
        )
        if r.status_code == 429:
            raise SourceUnavailable("Unpaywall rate-limited (429)")
        if r.status_code >= 500:
            # A dead endpoint (Unpaywall search 500s for days at a time)
            # gets benched for the run instead of adding latency + noise
            # to every remaining query.
            raise SourceUnavailable(f"Unpaywall server error ({r.status_code})")
        r.raise_for_status()
        ref = norm_title(title)
        for res in r.json().get("results") or []:
            resp = res.get("response", {})
            if norm_title(resp.get("title", "")) != ref:
                continue
            venue = (resp.get("journal_name") or "").strip()
            if not _is_published_venue(venue):
                continue
            doi = resp.get("doi", "")
            if ARXIV_DOI.match(doi):
                continue
            authors = [
                " ".join(filter(None, [a.get("given"), a.get("family")]))
                for a in resp.get("z_authors") or []
            ]
            _log(f"[unpaywall] match: {venue} {resp.get('year', '')}")
            return Match(
                source="unpaywall",
                venue=venue,
                title=clean_title(resp.get("title", "")),
                year=str(resp.get("year") or ""),
                authors=[a for a in authors if a],
                doi=doi,
            )
    return None


# ---------------------------------------------------------------------------
# OpenAlex (not in PaperMemory; unauthenticated with generous rate limits, so
# it doubles as the metadata fallback when the arXiv API / S2 are throttled)
# ---------------------------------------------------------------------------

def openalex_search(title: str) -> dict | None:
    """OpenAlex work with an exactly-matching normalized title, or None."""
    with _client() as c:
        r = c.get(
            "https://api.openalex.org/works",
            params=_openalex_params({"search": title, "per-page": 5}),
        )
        if r.status_code == 429:
            raise SourceUnavailable("OpenAlex rate-limited (429)")
        if r.status_code >= 500:
            # A dead endpoint (Unpaywall search 500s for days at a time)
            # gets benched for the run instead of adding latency + noise
            # to every remaining query.
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
        r = c.get(f"https://api.crossref.org/works/{doi}", params={"mailto": _mailto()})
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
        br = c.get(f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex")
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
    ("googlescholar", lambda t, y, a, au: try_google_scholar(t)),
    ("crossref", lambda t, y, a, au: try_crossref(t)),
    ("unpaywall", lambda t, y, a, au: try_unpaywall(t)),
    ("openalex", lambda t, y, a, au: try_openalex(t)),
)

# Sources that rate-limited/blocked us this process: skip them for the rest of
# the run instead of hammering them once per entry during batch `upgrade`
# (PaperMemory's DISABLE_MATCH, ported).
_DISABLED: dict[str, str] = {}

# Only these sources are authoritative enough that losing one taints a miss
# into "incomplete". Google Scholar captchas and Unpaywall flakiness are
# routine and must not stop "not_found" from ever being trustworthy.
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

    clean_misses = 0
    # Core sources lost earlier in this run taint this query's verdict too.
    incomplete = any(n in CORE_SOURCES for n in _DISABLED)
    for name, fn in CASCADE:
        if name in _DISABLED:
            continue
        try:
            m = fn(title, year, arxiv_id, author_hint)
            if m:
                cache.put(cache_key, m.__dict__)
                return m, "found"
            clean_misses += 1
            _log(f"[{name}] no publication found")
        except SourceUnavailable as e:
            _DISABLED[name] = str(e)
            incomplete = incomplete or name in CORE_SOURCES
            _log(f"[{name}] disabled for the rest of this run: {e}")
        except Exception as e:  # network hiccup on one source must not kill the run
            incomplete = incomplete or name in CORE_SOURCES
            _log(f"[{name}] error: {type(e).__name__}: {e}")

    # Exact-title search missed everywhere. Before concluding "no published
    # version", try the title-drift fallback — camera-ready titles frequently
    # differ from the arXiv ones, which is precisely the upgrade scenario.
    if author_hint and "dblp" not in _DISABLED:
        try:
            m = try_dblp_fuzzy(title, author_hint, year)
            if m:
                cache.put(cache_key, m.__dict__)
                return m, "found"
            clean_misses += 1
        except SourceUnavailable as e:
            _DISABLED["dblp"] = str(e)
            incomplete = True
        except Exception as e:
            incomplete = True
            _log(f"[dblp-fuzzy] error: {type(e).__name__}: {e}")
    if not clean_misses:
        return None, "unavailable"
    return None, ("incomplete" if incomplete else "not_found")
