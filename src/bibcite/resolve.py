"""Query classification and entry building.

resolve(query) turns "1706.03762" / an arXiv URL / a DOI / a free-form title
into a normalized BibTeX entry dict, with the venue canonicalized against the
vendored strings.bib table.
"""

import re
import sys
from dataclasses import dataclass

from .bibfile import NOISE_FIELDS, parse_bibtex_entry
from .normalize import clean_title, first_author_last_name, fix_author_caps, make_key


class NotFound(Exception):
    """No source could resolve the query — asking for a better identifier is
    the right next step."""


class SourcesUnavailable(Exception):
    """Resolution failed because sources were down/rate-limited, NOT because
    the paper doesn't exist. Retrying later is the right next step."""
from .sources import (
    ArxivMeta,
    Match,
    arxiv_metadata,
    crossref_by_doi,
    find_published,
)
from .venues import canonicalize

ARXIV_NEW = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$", re.I)
ARXIV_OLD = re.compile(r"^(?:arxiv:)?([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)
ARXIV_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})",
    re.I,
)
DOI_URL = re.compile(r"doi\.org/(10\.\S+)", re.I)
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _log(msg: str):
    print(msg, file=sys.stderr)


def classify(query: str) -> tuple[str, str]:
    q = query.strip().rstrip(".")
    m = ARXIV_URL.search(q)
    if m:
        return "arxiv", m.group(1)
    m = ARXIV_NEW.match(q) or ARXIV_OLD.match(q)
    if m:
        return "arxiv", m.group(1)
    m = DOI_URL.search(q)
    if m:
        return "doi", m.group(1)
    if DOI_RE.match(q):
        return "doi", q
    return "title", query.strip()


@dataclass
class Resolved:
    entry: dict  # bibtexparser-style: fields + ID + ENTRYTYPE
    source: str  # where the publication info came from
    venue: str  # final venue string ("" if preprint)
    published: bool

    @property
    def bibtex(self) -> str:
        from .bibfile import entry_to_bibtex

        return entry_to_bibtex(self.entry)


def guess_entry_type(venue: str) -> str:
    """Entry type for a venue that is NOT in the canonical table.

    Sources without bibtex only give us a venue string; a conference-sounding
    name must become @inproceedings, not a sloppy @article.
    """
    v = venue.lower()
    # NOTE: "proceedings" alone is NOT conclusive — PNAS and Proceedings of
    # the IEEE are journals. Real conference names carry one of these words.
    conference_words = ("conference", "workshop", "symposium", "meeting", "congress")
    return "inproceedings" if any(w in v for w in conference_words) else "article"


def _entry_from_match(match: Match, meta: ArxivMeta | None) -> dict:
    """Best available entry for a published match: parse the source's bibtex
    when there is one, else construct from structured fields."""
    entry: dict = {}
    if match.bibtex:
        try:
            entry = parse_bibtex_entry(match.bibtex)
        except Exception as e:
            # Bad source bibtex must degrade to field construction, never
            # abort the resolution.
            _log(f"[{match.source}] could not parse its BibTeX ({e}); building from fields")
            entry = {}
    if not entry:
        authors = match.authors or (meta.authors if meta else [])
        entry_type = guess_entry_type(match.venue)
        entry = {
            "ENTRYTYPE": entry_type,
            "author": " and ".join(authors),
            "title": match.title or (meta.title if meta else ""),
            ("booktitle" if entry_type == "inproceedings" else "journal"): match.venue,
            "year": match.year or (meta.year if meta else ""),
        }
    for f in NOISE_FIELDS:
        entry.pop(f, None)

    entry["title"] = clean_title(entry.get("title", ""))
    if entry.get("author"):
        entry["author"] = fix_author_caps(entry["author"])
    if match.doi and not entry.get("doi"):
        entry["doi"] = match.doi

    # Canonicalize the venue against the strings.bib table.
    raw_venue = match.venue or entry.get("booktitle", "") or entry.get("journal", "")
    year = entry.get("year", "") or match.year
    canonical = canonicalize(raw_venue, year) or canonicalize(
        entry.get("booktitle", "") or entry.get("journal", ""), year
    )
    if canonical:
        entry.pop("booktitle", None)
        entry.pop("journal", None)
        entry["ENTRYTYPE"] = canonical.entry_type
        entry[canonical.bib_field] = canonical.name
        venue_str = canonical.name
        _log(f"[venues] '{raw_venue}' -> {canonical.macro} ({canonical.name})")
    else:
        venue_str = raw_venue
        _log(f"[venues] no canonical mapping for '{raw_venue}' (kept as-is)")

    entry["__venue"] = venue_str
    return entry


def _finalize(entry: dict, meta: ArxivMeta | None) -> dict:
    """URL / eprint fields, key, cleanup."""
    if meta and meta.arxiv_id:
        entry["url"] = meta.abs_url  # prefer the arXiv link for access
        entry["eprint"] = meta.arxiv_id
        entry["archiveprefix"] = "arXiv"
        if meta.primary_class:
            entry["primaryclass"] = meta.primary_class
    elif entry.get("doi"):
        url = entry.get("url", "")
        # Modernize legacy resolver links (http://dx.doi.org/...) and fill in
        # a missing url from the DOI.
        if not url or "dx.doi.org" in url:
            entry["url"] = f"https://doi.org/{entry['doi']}"
    author = entry.get("author", "") or "anonymous"
    year = entry.get("year", "") or "XXXX"
    entry["ID"] = make_key(author, year, entry.get("title", ""))
    entry.pop("__venue", None)
    return entry


def _arxiv_only_entry(meta: ArxivMeta) -> dict:
    """Unpublished preprint: @misc per arXiv's own recommendation — never
    @article with a fake journal. howpublished keeps the arXiv pointer
    visible under classic BibTeX styles that ignore eprint fields."""
    return {
        "ENTRYTYPE": "misc",
        "author": " and ".join(meta.authors),
        "title": meta.title,
        "howpublished": f"arXiv preprint arXiv:{meta.arxiv_id}",
        "year": meta.year,
    }


def resolve(query: str, require_published: bool = False) -> Resolved:
    kind, value = classify(query)
    _log(f"[bibcite] query understood as {kind}: {value}")

    if kind == "arxiv":
        try:
            meta = arxiv_metadata(value)
        except ValueError:
            raise
        except Exception as e:
            _log(f"[arxiv] API unavailable ({e}); trying fallback metadata sources")
            from .sources import arxiv_abs_metadata, s2_arxiv_metadata

            meta = None
            for fallback in (s2_arxiv_metadata, arxiv_abs_metadata):
                try:
                    meta = fallback(value)
                except Exception as fe:
                    _log(f"[arxiv-fallback] {fallback.__name__}: {fe}")
                if meta is not None:
                    break
            if meta is None:
                raise SourcesUnavailable(
                    f"Could not fetch metadata for arXiv:{value} "
                    "(arXiv API, Semantic Scholar, and arxiv.org all unavailable)"
                )
        _log(f"[arxiv] {meta.title} ({meta.year})")
        hint = first_author_last_name(meta.authors[0]) if meta.authors else ""
        match, status = find_published(meta.title, meta.year, meta.arxiv_id, hint)
        if match:
            entry = _entry_from_match(match, meta)
            venue = entry.pop("__venue", match.venue)
            return Resolved(_finalize(entry, meta), match.source, venue, True)
        if require_published:
            if status == "unavailable":
                raise SourcesUnavailable(
                    f"Could not check publication status for arXiv:{value} (sources down)"
                )
            raise NotFound(f"No published version found for arXiv:{value}")
        _log("[bibcite] no published version found; using arXiv preprint entry")
        entry = _arxiv_only_entry(meta)
        return Resolved(_finalize(entry, meta), "arxiv", "", False)

    if kind == "doi":
        match = crossref_by_doi(value)
        if not match or not match.title:
            raise NotFound(f"DOI not found on CrossRef: {value}")
        entry = _entry_from_match(match, None)
        venue = entry.pop("__venue", match.venue)
        return Resolved(_finalize(entry, None), match.source, venue, True)

    # Free-form title: locate it on arXiv first — the authors sharpen the
    # DBLP query (generic titles drown in DBLP's ranking) and we gain the
    # eprint/url fields; papers not on arXiv still go through the cascade.
    meta = _arxiv_search_title(value)
    if meta:
        _log(f"[arxiv] found on arXiv: {meta.arxiv_id} ({meta.year})")
    else:
        meta = _openalex_meta(value)  # arXiv API throttled/paper not found
        if meta:
            _log(f"[openalex] metadata: arXiv {meta.arxiv_id or '?'} ({meta.year})")
    hint = first_author_last_name(meta.authors[0]) if meta and meta.authors else ""
    match, status = find_published(
        meta.title if meta else value,
        meta.year if meta else "",
        meta.arxiv_id if meta else "",
        hint,
    )
    if match:
        entry = _entry_from_match(match, meta)
        venue = entry.pop("__venue", match.venue)
        return Resolved(_finalize(entry, meta), match.source, venue, True)
    if meta and meta.arxiv_id:
        if require_published:
            raise NotFound(f"Only an arXiv preprint was found for: {value}")
        _log("[bibcite] no published version found; using arXiv preprint entry")
        entry = _arxiv_only_entry(meta)
        return Resolved(_finalize(entry, meta), "arxiv", "", False)
    if status == "unavailable":
        raise SourcesUnavailable(
            f"All sources were rate-limited or down while resolving: {value}"
        )
    raise NotFound(f"No match found anywhere for: {value}")


def _openalex_meta(title: str) -> ArxivMeta | None:
    """Author/year/arXiv-id metadata via OpenAlex when the arXiv API is down."""
    from .sources import openalex_arxiv_id, openalex_authors, openalex_search

    try:
        work = openalex_search(title)
    except Exception as e:
        _log(f"[openalex] unavailable: {e}")
        return None
    if not work:
        return None
    aid = openalex_arxiv_id(work)
    return ArxivMeta(
        arxiv_id=aid,
        title=clean_title(work.get("title") or title),
        authors=openalex_authors(work),
        year=str(work.get("publication_year") or ""),
        abs_url=f"https://arxiv.org/abs/{aid}" if aid else "",
    )


def _arxiv_search_title(title: str) -> ArxivMeta | None:
    from .normalize import norm_title
    from .sources import ATOM, ARXIV_NS, arxiv_api_get

    try:
        r = arxiv_api_get({"search_query": f'ti:"{title}"', "max_results": 5})
    except Exception as e:
        _log(f"[arxiv-search] unavailable: {e}")
        return None
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(r.text)
        for e in root.findall(f"{ATOM}entry"):
            t = clean_title(e.findtext(f"{ATOM}title") or "")
            if norm_title(t) != norm_title(title):
                continue
            aid = (e.findtext(f"{ATOM}id") or "").split("/abs/")[-1]
            aid = re.sub(r"v\d+$", "", aid)
            primary = e.find(f"{ARXIV_NS}primary_category")
            return ArxivMeta(
                arxiv_id=aid,
                title=t,
                authors=[
                    a.findtext(f"{ATOM}name").strip()
                    for a in e.findall(f"{ATOM}author")
                    if (a.findtext(f"{ATOM}name") or "").strip()
                ],
                year=(e.findtext(f"{ATOM}published") or "")[:4],
                abs_url=f"https://arxiv.org/abs/{aid}",
                primary_class=primary.get("term") if primary is not None else "",
            )
    except Exception as e:
        _log(f"[arxiv-search] error: {e}")
    return None
