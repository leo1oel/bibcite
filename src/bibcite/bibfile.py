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
# their LaTeX workflow.
TIDY_ARGS = [
    "--modify",
    "--omit=pages,publisher,doi,timestamp,biburl,bibsource,abstract,month,series,volume,editor,note,date,number,address",
    "--curly",
    "--blank-lines",
    "--trailing-commas",
    "--sort=-year",
    "--duplicates=citation",
    "--merge=first",
    "--sort-fields=author,title,booktitle,journal,year,url,pdf",
    "--strip-enclosing-braces",
    "--tidy-comments",
    "--generate-keys",
]

NOISE_FIELDS = ("timestamp", "biburl", "bibsource", "crossref")

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _log(msg: str):
    print(msg, file=sys.stderr)


def _parser() -> BibTexParser:
    p = BibTexParser(common_strings=True)
    p.ignore_nonstandard_types = False
    return p


def parse_bib(text: str) -> BibDatabase:
    return bibtexparser.loads(text, parser=_parser())


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


def find_existing(db: BibDatabase, title: str, arxiv_id: str = "", doi: str = "") -> dict | None:
    ref = norm_title(title)
    for entry in db.entries:
        if arxiv_id and entry_arxiv_id(entry) == arxiv_id:
            return entry
        if doi and entry.get("doi", "").lower() == doi.lower():
            return entry
        if ref and norm_title(entry.get("title", "")) == ref:
            return entry
    return None


def upsert_entry(path: Path, entry: dict) -> tuple[str, str]:
    """Insert or upgrade ``entry`` in ``path``.

    Returns (action, key) where action is "added" | "upgraded" | "exists".
    """
    db = load_bib_file(path)
    if db is None:  # unparseable file: append blindly
        with path.open("a") as f:
            f.write("\n" + entry_to_bibtex(entry))
        return "added", entry["ID"]

    existing = find_existing(
        db, entry.get("title", ""), entry_arxiv_id(entry), entry.get("doi", "")
    )
    if existing is not None:
        if is_preprint(existing) and not is_preprint(entry):
            key = existing["ID"]
            existing.clear()
            existing.update(entry)
            existing["ID"] = key  # keep the key the user may already \cite
            _write_db(path, db)
            return "upgraded", key
        return "exists", existing["ID"]

    db.entries.append({k: str(v) for k, v in entry.items() if v})
    _write_db(path, db)
    return "added", entry["ID"]


def _write_db(path: Path, db: BibDatabase):
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


def run_tidy(path: Path) -> bool:
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
