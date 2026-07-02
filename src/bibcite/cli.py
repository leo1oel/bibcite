"""bibcite CLI.

Designed to be called by agents: never hand-edit a .bib file — let
``bibcite add`` resolve, canonicalize, dedupe, write, and tidy, then use the
citation key it prints.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from . import bibfile
from .normalize import first_author_last_name, norm_title
from .resolve import Resolved, guess_entry_type, resolve
from .sources import find_published
from .venues import canonicalize


def _log(msg: str):
    print(msg, file=sys.stderr)


def _emit(payload: dict, as_json: bool = True):
    """File-mutating commands always print one JSON object on stdout — the
    agent-facing contract. Only `get` has a plain mode (BibTeX on stdout for
    previewing/piping)."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for k, v in payload.items():
            if k != "bibtex":
                _log(f"{k}: {v}")
        if payload.get("bibtex"):
            print(payload["bibtex"], end="")
        elif payload.get("key"):
            print(payload["key"])


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def _resolve_or_none(query: str, require_published: bool):
    try:
        return resolve(query, require_published=require_published)
    except (LookupError, ValueError) as e:
        _log(f"[bibcite] {e}")
    except Exception as e:
        _log(f"[bibcite] network error: {type(e).__name__}: {e}")
    return None


def cmd_get(args) -> int:
    query = " ".join(args.query)
    res = _resolve_or_none(query, args.require_published)
    if res is None:
        return 2
    _emit(
        {
            "action": "resolved",
            "key": res.entry["ID"],
            "title": res.entry.get("title", ""),
            "venue": res.venue or "arXiv (preprint, no published venue found)",
            "published": res.published,
            "source": res.source,
            "bibtex": res.bibtex,
        },
        args.json,
    )
    return 0


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def cmd_add(args) -> int:
    path = Path(args.file)
    if args.bibtex:
        text = sys.stdin.read() if args.bibtex == "-" else args.bibtex
        entry = bibfile.parse_bibtex_entry(text)
        raw_venue = entry.get("booktitle", "") or entry.get("journal", "")
        canonical = canonicalize(raw_venue, entry.get("year"))
        if canonical:
            entry.pop("booktitle", None)
            entry.pop("journal", None)
            entry["ENTRYTYPE"] = canonical.entry_type
            entry[canonical.bib_field] = canonical.name
        res = Resolved(entry, "user-bibtex", canonical.name if canonical else raw_venue, True)
    else:
        if not args.query:
            _log("[bibcite] provide a query (arXiv id / DOI / title) or --bibtex")
            return 2
        query = " ".join(args.query)
        res = _resolve_or_none(query, args.require_published)
        if res is None:
            return 2

    action, key = bibfile.upsert_entry(path, res.entry)
    tidied = False
    if action != "exists" and not args.no_tidy:
        tidied = bibfile.run_tidy(path)
        if tidied:
            key = bibfile.key_after_tidy(path, res.entry.get("title", ""), key)

    _emit(
        {
            "action": action,
            "key": key,
            "title": res.entry.get("title", ""),
            "venue": res.venue or "arXiv (preprint)",
            "published": res.published,
            "source": res.source,
            "file": str(path),
            "tidied": tidied,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# upgrade: batch-match arXiv entries in an existing file (bibMatcher, CLI-style)
# ---------------------------------------------------------------------------

def _upgrade_entries(path: Path, dry_run: bool) -> dict:
    """Match every preprint entry in ``path`` to its published version and
    rewrite it in place (unless dry_run). Returns the report; does NOT tidy —
    callers decide."""
    db = bibfile.load_bib_file(path)
    if db is None or not db.entries:
        _log(f"[bibcite] nothing to do in {path}")
        return {"upgraded": 0, "matched": 0, "entries": []}

    report = []
    changed = 0
    processed = 0
    for entry in db.entries:
        if not bibfile.is_preprint(entry):
            continue
        title = entry.get("title", "").replace("{", "").replace("}", "")
        if not title:
            continue
        if processed:
            time.sleep(1)  # be polite to the APIs on batch runs
        processed += 1
        _log(f"[upgrade] matching: {title[:80]}")
        aid = bibfile.entry_arxiv_id(entry)
        hint = (
            first_author_last_name(entry["author"]) if entry.get("author") else ""
        )
        match = find_published(title, entry.get("year", ""), aid, hint)
        if not match:
            report.append({"key": entry["ID"], "title": title, "matched": False})
            continue
        canonical = canonicalize(match.venue, match.year or entry.get("year"))
        venue_name = canonical.name if canonical else match.venue
        if not dry_run:
            entry.pop("journal", None)
            entry.pop("booktitle", None)
            entry.pop("howpublished", None)
            if canonical:
                entry["ENTRYTYPE"] = canonical.entry_type
                entry[canonical.bib_field] = canonical.name
            else:
                entry["ENTRYTYPE"] = guess_entry_type(match.venue)
                field = (
                    "booktitle"
                    if entry["ENTRYTYPE"] == "inproceedings"
                    else "journal"
                )
                entry[field] = match.venue
            if match.year:
                entry["year"] = match.year
            if match.doi and not entry.get("doi"):
                entry["doi"] = match.doi
            changed += 1
        report.append(
            {
                "key": entry["ID"],
                "title": title,
                "matched": True,
                "venue": venue_name,
                "source": match.source,
            }
        )

    if changed and not dry_run:
        bibfile._write_db(path, db)

    matched = sum(1 for r in report if r["matched"])
    for r in report:
        mark = "✓" if r["matched"] else "✗"
        _log(f"{mark} {r['key']}: {r.get('venue', 'no match')}")
    _log(f"[bibcite] {matched} matched, {changed} upgraded{' (dry-run)' if dry_run else ''}")
    return {"upgraded": changed, "matched": matched, "entries": report}


def cmd_upgrade(args) -> int:
    path = Path(args.file)
    result = _upgrade_entries(path, args.dry_run)
    if result["upgraded"] and not args.no_tidy:
        bibfile.run_tidy(path)
    _emit({**result, "dry_run": args.dry_run})
    return 0


# ---------------------------------------------------------------------------
# tidy / check
# ---------------------------------------------------------------------------

def cmd_tidy(args) -> int:
    return 0 if bibfile.run_tidy(Path(args.file)) else 1


def _check_problems(path: Path) -> tuple[int, list] | None:
    """(entry count, problem list) for a .bib file, or None if unparseable."""
    db = bibfile.load_bib_file(path)
    if db is None:
        return None
    problems = []
    seen_titles: dict[str, str] = {}
    for entry in db.entries:
        key = entry.get("ID", "?")
        nt = norm_title(entry.get("title", ""))
        if nt and nt in seen_titles:
            problems.append({"key": key, "issue": f"duplicate title of {seen_titles[nt]}"})
        seen_titles.setdefault(nt, key)
        for f in ("author", "title", "year"):
            if not entry.get(f):
                problems.append({"key": key, "issue": f"missing {f}"})
        if bibfile.is_preprint(entry):
            problems.append({"key": key, "issue": "arXiv preprint (try `bibcite upgrade`)"})
    for p in problems:
        _log(f"{p['key']}: {p['issue']}")
    _log(f"[bibcite] {len(db.entries)} entries, {len(problems)} issues")
    return len(db.entries), problems


def cmd_check(args) -> int:
    checked = _check_problems(Path(args.file))
    if checked is None:
        _log(f"[bibcite] {args.file} could not be parsed")
        return 1
    entries, problems = checked
    _emit({"entries": entries, "problems": problems})
    return 0


def cmd_fix(args) -> int:
    """One-shot cleanup: upgrade preprints, always tidy, then re-lint."""
    path = Path(args.file)
    if not path.exists():
        _log(f"[bibcite] {path} does not exist")
        return 1
    result = _upgrade_entries(path, dry_run=False)
    tidied = bibfile.run_tidy(path)
    checked = _check_problems(path)
    entries, problems = checked if checked else (0, [])
    _emit(
        {
            **result,
            "tidied": tidied,
            "entries_total": entries,
            "remaining_problems": problems,
        }
    )
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="bibcite",
        description="Resolve papers to canonical BibTeX and manage .bib files (agents: use `add`, never hand-edit).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="resolve a query and print BibTeX to stdout")
    g.add_argument("query", nargs="+", help="arXiv id / arXiv URL / DOI / title")
    g.add_argument("--json", action="store_true", help="print a JSON object instead of BibTeX")
    g.add_argument("--require-published", action="store_true", help="fail instead of falling back to an arXiv entry")
    g.set_defaults(fn=cmd_get)

    a = sub.add_parser("add", help="resolve and write into a .bib file, then run bibtex-tidy (prints JSON)")
    a.add_argument("file", help="target .bib file (created if missing)")
    a.add_argument("query", nargs="*", help="arXiv id / arXiv URL / DOI / title")
    a.add_argument("--bibtex", help="raw BibTeX entry to add instead of a query ('-' reads stdin)")
    a.add_argument("--no-tidy", action="store_true")
    a.add_argument("--require-published", action="store_true")
    a.set_defaults(fn=cmd_add)

    u = sub.add_parser("upgrade", help="match all arXiv entries in a file to their published versions (prints JSON)")
    u.add_argument("file")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--no-tidy", action="store_true")
    u.set_defaults(fn=cmd_upgrade)

    t = sub.add_parser("tidy", help="run bibtex-tidy with the canonical flags")
    t.add_argument("file")
    t.set_defaults(fn=cmd_tidy)

    c = sub.add_parser("check", help="offline read-only lint of a .bib file (prints JSON)")
    c.add_argument("file")
    c.set_defaults(fn=cmd_check)

    f = sub.add_parser(
        "fix",
        help="one-shot cleanup: upgrade preprints to published versions, tidy, then lint (prints JSON)",
    )
    f.add_argument("file")
    f.set_defaults(fn=cmd_fix)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
