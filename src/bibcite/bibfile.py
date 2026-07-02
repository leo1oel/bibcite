"""Reading/writing .bib files, deduplication, and the bibtex-tidy runner."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter

from .normalize import norm_title

# The exact bibtex-tidy invocation requested by the user; keep in sync with
# their LaTeX workflow. NOTE: no --generate-keys — bibcite owns key
# generation (make_key ASCII-folds names, so Hyvärinen -> hyvarinen2000...,
# where tidy would emit hyv_arinen2000...), and stable keys keep existing
# \cite{} commands valid.
TIDY_ARGS = [
    "--modify",
    "--omit=pages,publisher,doi,timestamp,biburl,bibsource,abstract,month,series,volume,editor,note,date,number,address,issn,isbn",
    "--curly",
    "--blank-lines",
    "--trailing-commas",
    "--sort=-year",
    "--duplicates=citation",
    "--merge=first",
    "--sort-fields=author,title,booktitle,journal,year,url,pdf",
    "--strip-enclosing-braces",
    "--tidy-comments",
]

NOISE_FIELDS = ("timestamp", "biburl", "bibsource", "crossref", "month")

# BibTeX month macros. bibtexparser's common_strings only defines jan..dec;
# CrossRef's transform endpoint emits bare full names (month=June), which
# otherwise KeyError during string interpolation.
MONTH_STRINGS = {
    m[:3]: m.capitalize()
    for m in (
        "january february march april may june july august september "
        "october november december"
    ).split()
} | {
    m: m.capitalize()
    for m in (
        "january february march april may june july august september "
        "october november december"
    ).split()
}

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _log(msg: str):
    print(msg, file=sys.stderr)


def _parser() -> BibTexParser:
    p = BibTexParser(common_strings=True)
    p.ignore_nonstandard_types = False
    p.bib_database.strings.update(MONTH_STRINGS)
    return p


def parse_bib(text: str) -> BibDatabase:
    try:
        return bibtexparser.loads(text, parser=_parser())
    except Exception as e:
        # Undefined @string macros raise bare KeyError('macro'); rewrap so
        # callers see a real message and KeyError never masquerades as a
        # LookupError "not found" upstream.
        raise ValueError(f"BibTeX parse failed: {type(e).__name__}: {e}") from e


def parse_bibtex_entry(text: str) -> dict:
    """First entry of a bibtex string as a dict (fields + ID + ENTRYTYPE)."""
    db = parse_bib(text)
    if not db.entries:
        raise ValueError("No BibTeX entry could be parsed")
    entry = dict(db.entries[0])
    for f in NOISE_FIELDS:
        entry.pop(f, None)
    return entry


def entry_to_bibtex(entry: dict) -> str:
    db = BibDatabase()
    db.entries = [{k: str(v) for k, v in entry.items() if v}]
    writer = BibTexWriter()
    writer.indent = "  "
    return bibtexparser.dumps(db, writer).strip() + "\n"


def entry_arxiv_id(entry: dict) -> str:
    """Extract an arXiv id from eprint/url/journal/note fields, if any."""
    for f in ("eprint", "url", "journal", "note", "doi"):
        v = entry.get(f, "")
        if "arxiv" in v.lower() or f == "eprint":
            m = ARXIV_ID_RE.search(v)
            if m:
                return m.group(1)
    return ""


def is_preprint(entry: dict) -> bool:
    """Preprint = the venue fields say arXiv/preprint, or there is no venue.

    eprint/archiveprefix/url fields do NOT count: published entries keep
    their arXiv pointers.
    """
    venue = " ".join(
        str(entry.get(f, "")) for f in ("journal", "booktitle", "howpublished")
    ).lower()
    if "arxiv" in venue or "preprint" in venue or "corr" in venue.split():
        return True
    return not entry.get("journal") and not entry.get("booktitle")


def load_bib_file(path: Path) -> BibDatabase | None:
    """Parse an existing .bib file; None when it cannot be parsed (we then
    degrade to append-only mode)."""
    if not path.exists() or not path.read_text().strip():
        return BibDatabase()
    try:
        return parse_bib(path.read_text())
    except Exception as e:
        _log(f"[bibcite] warning: could not parse {path} ({e}); appending without dedup")
        return None


def find_existing(
    db: BibDatabase,
    title: str,
    arxiv_id: str = "",
    doi: str = "",
    author: str = "",
) -> dict | None:
    from .normalize import first_author_last_name, titles_similar

    ref = norm_title(title)
    for entry in db.entries:
        if arxiv_id and entry_arxiv_id(entry) == arxiv_id:
            return entry
        if doi:
            d = doi.lower()
            # Older entries may lack a doi field but carry it in the url.
            if entry.get("doi", "").lower() == d or d in entry.get("url", "").lower():
                return entry
        if ref and norm_title(entry.get("title", "")) == ref:
            return entry
    # Fuzzy pass: title drift (arXiv vs camera-ready) with the same first
    # author is the same paper — catch it BEFORE writing a duplicate pair.
    if title and author:
        last = first_author_last_name(author)
        for entry in db.entries:
            if not entry.get("author"):
                continue
            if first_author_last_name(entry["author"]) != last:
                continue
            if titles_similar(title, entry.get("title", "")):
                return entry
    return None


def upsert_entry(
    path: Path, entry: dict, replace: bool = False, replace_key: str = ""
) -> tuple[str, str]:
    """Insert or upgrade ``entry`` in ``path``.

    Returns (action, key), action in "added" | "upgraded" | "exists" |
    "replaced" | "no_match_to_replace". With ``replace``, an existing
    matching entry is overwritten; ``replace_key`` targets a specific entry
    by citation key (for when title drift defeats the automatic match). The
    existing key is always kept so \\cite{} commands stay valid. A replace
    that matches nothing is an ERROR, not a silent add — that is how
    duplicate entries sneak into a file.
    """
    db = load_bib_file(path)
    if db is None:  # unparseable file: append blindly
        if replace or replace_key:
            return "no_match_to_replace", replace_key or entry["ID"]
        with path.open("a") as f:
            f.write("\n" + entry_to_bibtex(entry))
        return "added", entry["ID"]

    if replace_key:
        existing = next((e for e in db.entries if e.get("ID") == replace_key), None)
    else:
        existing = find_existing(
            db,
            entry.get("title", ""),
            entry_arxiv_id(entry),
            entry.get("doi", ""),
            entry.get("author", ""),
        )

    if existing is not None:
        upgrade = is_preprint(existing) and not is_preprint(entry)
        if replace or replace_key or upgrade:
            key = existing["ID"]
            existing.clear()
            existing.update({k: str(v) for k, v in entry.items() if v})
            existing["ID"] = key  # keep the key the user may already \cite
            _write_db(path, db)
            return ("replaced" if (replace or replace_key) else "upgraded"), key
        return "exists", existing["ID"]

    if replace or replace_key:
        return "no_match_to_replace", replace_key or entry["ID"]

    db.entries.append({k: str(v) for k, v in entry.items() if v})
    _write_db(path, db)
    return "added", entry["ID"]


def remove_entry(path: Path, key: str) -> bool:
    """Delete the entry with citation key ``key``. True if something was
    removed."""
    db = load_bib_file(path)
    if db is None:
        return False
    before = len(db.entries)
    db.entries = [e for e in db.entries if e.get("ID") != key]
    if len(db.entries) == before:
        return False
    _write_db(path, db)
    return True


def _write_db(path: Path, db: BibDatabase):
    # Never write our injected month macros back out as @string blocks (they
    # exist only so parsing month=June doesn't crash); this also scrubs any
    # that leaked into a file before this guard existed. User-defined
    # @strings are untouched.
    for k in MONTH_STRINGS:
        db.strings.pop(k, None)
    writer = BibTexWriter()
    writer.indent = "  "
    writer.order_entries_by = None  # preserve file order; tidy re-sorts anyway
    path.write_text(bibtexparser.dumps(db, writer))


# ---------------------------------------------------------------------------
# bibtex-tidy
# ---------------------------------------------------------------------------

def tidy_command() -> list[str] | None:
    exe = shutil.which("bibtex-tidy")
    if exe:
        return [exe]
    if shutil.which("npx"):
        return ["npx", "--yes", "bibtex-tidy"]
    return None


_MONTH_STRING_BLOCK = re.compile(
    r"@string\s*\{\s*(?:" + "|".join(MONTH_STRINGS) + r")\s*=",
    re.IGNORECASE,
)


def _scrub_month_strings(path: Path):
    """Remove orphan month @string blocks left by the pre-0.4 leak.
    bibtex-tidy itself preserves @strings, so tidy alone never cleans them."""
    try:
        if not _MONTH_STRING_BLOCK.search(path.read_text()):
            return
        db = load_bib_file(path)
        if db is not None:
            _write_db(path, db)  # _write_db drops the injected month macros
            _log("[bibcite] scrubbed leftover month @string blocks")
    except Exception as e:
        _log(f"[bibcite] month-string scrub skipped: {e}")


def run_tidy(path: Path) -> bool:
    _scrub_month_strings(path)
    cmd = tidy_command()
    if cmd is None:
        _log("[bibcite] bibtex-tidy not found (npm i -g bibtex-tidy); skipping tidy")
        return False
    proc = subprocess.run(
        cmd + [str(path)] + TIDY_ARGS, capture_output=True, text=True
    )
    if proc.returncode != 0:
        _log(f"[bibcite] bibtex-tidy failed:\n{proc.stderr.strip()}")
        return False
    _log(f"[bibcite] bibtex-tidy: {proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else 'ok'}")
    return True


def key_after_tidy(path: Path, title: str, fallback_key: str) -> str:
    """bibtex-tidy --generate-keys rewrites keys; re-read the file to report
    the final key for the entry with this title."""
    db = load_bib_file(path)
    if db is None:
        return fallback_key
    ref = norm_title(title)
    for entry in db.entries:
        if norm_title(entry.get("title", "")) == ref:
            return entry["ID"]
    return fallback_key
