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

from . import bibfile, cache
from .normalize import first_author_last_name, fix_pages, norm_title, titles_similar
from .resolve import classify
from .resolve import (
    NotFound,
    Resolved,
    SourcesUnavailable,
    guess_entry_type,
    resolve,
)
from .sources import find_published
from .venues import canonicalize

# Exit codes (part of the agent-facing contract):
#   0 success
#   2 the paper could not be resolved — ask for a stronger identifier
#   3 internal/network failure (sources down, unexpected error) — retry later
EXIT_NOT_FOUND = 2
EXIT_INTERNAL = 3


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

def _resolve_or_none(query: str, require_published: bool) -> tuple[Resolved | None, int]:
    """(result, exit_code). Distinguishes 'not found' (2) from 'tool/source
    failure' (3) so agents know whether to retry with a better identifier or
    just retry later."""
    try:
        return resolve(query, require_published=require_published), 0
    except (NotFound, ValueError) as e:
        _log(f"[bibcite] {e}")
        return None, EXIT_NOT_FOUND
    except SourcesUnavailable as e:
        _log(f"[bibcite] sources unavailable: {e}")
        return None, EXIT_INTERNAL
    except Exception as e:
        _log(f"[bibcite] internal error: {type(e).__name__}: {e}")
        return None, EXIT_INTERNAL


def cmd_get(args) -> int:
    query = " ".join(args.query)
    if args.no_cache:
        cache.DISABLED = True
    res, code = _resolve_or_none(query, args.require_published)
    if res is None:
        return code
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

def _resolve_user_bibtex(text: str) -> Resolved:
    entry = bibfile.parse_bibtex_entry(text)
    raw_venue = entry.get("booktitle", "") or entry.get("journal", "")
    canonical = canonicalize(raw_venue, entry.get("year"))
    if canonical:
        entry.pop("booktitle", None)
        entry.pop("journal", None)
        entry["ENTRYTYPE"] = canonical.entry_type
        entry[canonical.bib_field] = canonical.name
    if entry.get("pages"):
        entry["pages"] = fix_pages(entry["pages"])
    published = not bibfile.is_preprint(entry)
    return Resolved(
        entry,
        "user-bibtex",
        (canonical.name if canonical else raw_venue) if published else "",
        published,
    )


def _identity_mismatch(path: Path, target_key: str, new_entry: dict) -> str:
    """`--key` is a scalpel — warn when the resolved paper does not look like
    the entry it is about to overwrite (no shared arXiv id, DOI, or similar
    title), because the old key would then be citing a different paper."""
    db = bibfile.load_bib_file(path)
    if db is None:
        return ""
    target = next((e for e in db.entries if e.get("ID") == target_key), None)
    if target is None:
        return ""
    old_aid, new_aid = bibfile.entry_arxiv_id(target), bibfile.entry_arxiv_id(new_entry)
    if old_aid and new_aid and old_aid == new_aid:
        return ""
    old_doi, new_doi = target.get("doi", "").lower(), new_entry.get("doi", "").lower()
    if old_doi and new_doi and old_doi == new_doi:
        return ""
    if titles_similar(target.get("title", ""), new_entry.get("title", "")):
        return ""
    return (
        f"replacing '{target_key}' with what looks like a DIFFERENT paper "
        f"('{target.get('title', '')[:50]}' -> '{new_entry.get('title', '')[:50]}'); "
        "the key will no longer describe its contents"
    )


def _local_exists(path: Path, query: str) -> str | None:
    """Local pre-check: if the query is already in the file as a PUBLISHED
    entry, skip the network entirely (makes --from re-runs and repeated adds
    near-instant). Preprints still resolve online — they may be upgradable."""
    db = bibfile.load_bib_file(path)
    if db is None or not db.entries:
        return None
    kind, value = classify(query)
    if kind == "arxiv":
        existing = bibfile.find_existing(db, "", arxiv_id=value)
    elif kind == "doi":
        existing = bibfile.find_existing(db, "", doi=value)
    else:
        existing = bibfile.find_existing(db, value)
    if existing is None:
        return None
    if not bibfile.is_preprint(existing):
        return existing["ID"]
    if existing.get("pubstate", "").strip("{}") == "preprint":
        # Confirmed preprint-only: nothing to upgrade, no reason to go online.
        return existing["ID"]
    return None


def cmd_add(args) -> int:
    path = Path(args.file)
    if args.no_cache:
        cache.DISABLED = True
    targeting = args.replace or bool(args.key)
    if args.key and args.from_file:
        _log("[bibcite] --key targets one entry; it cannot be combined with --from")
        return EXIT_NOT_FOUND

    # Collect the queries for this invocation (single, --bibtex, or --from).
    # Each item: (query, resolved_or_None, exit_code, local_exists_key).
    items: list[tuple[str, Resolved | None, int, str]] = []
    if args.bibtex:
        text = sys.stdin.read() if args.bibtex == "-" else args.bibtex
        try:
            items.append(("<bibtex>", _resolve_user_bibtex(text), 0, ""))
        except ValueError as e:
            _log(f"[bibcite] {e}")
            return EXIT_NOT_FOUND
    elif args.from_file:
        lines = Path(args.from_file).read_text().splitlines()
        queries = [q.strip() for q in lines if q.strip() and not q.strip().startswith("#")]
        resolved_any = False
        for i, q in enumerate(queries):
            local = None if targeting else _local_exists(path, q)
            if local:
                _log(f"[bibcite] ({i + 1}/{len(queries)}) {q} — already in file: {local}")
                items.append((q, None, 0, local))
                continue
            if resolved_any:
                time.sleep(1)  # one process shares the rate-limit breaker; stay polite
            resolved_any = True
            _log(f"[bibcite] ({i + 1}/{len(queries)}) {q}")
            res, code = _resolve_or_none(q, args.require_published)
            items.append((q, res, code, ""))
    else:
        if not args.query:
            _log("[bibcite] provide a query (arXiv id / DOI / title), --bibtex, or --from")
            return EXIT_NOT_FOUND
        query = " ".join(args.query)
        local = None if targeting else _local_exists(path, query)
        if local:
            _log(f"[bibcite] already in file (matched locally, no network): {local}")
            _emit({"action": "exists", "key": local, "file": str(path), "tidied": False})
            return 0
        res, code = _resolve_or_none(query, args.require_published)
        if res is None:
            return code
        items.append((query, res, 0, ""))

    # Write all entries first, tidy once, then read back the final keys.
    results = []
    wrote = False
    for query, res, code, local_key in items:
        if local_key:
            results.append({"query": query, "action": "exists", "key": local_key})
            continue
        if res is None:
            results.append({"query": query, "action": "failed", "exit_code": code})
            continue
        warning = ""
        if args.key:
            warning = _identity_mismatch(path, args.key, res.entry)
            if warning:
                _log(f"[bibcite] warning: {warning}")
        action, key = bibfile.upsert_entry(
            path, res.entry, replace=args.replace, replace_key=args.key or ""
        )
        if action == "no_match_to_replace":
            # A replace that matches nothing must fail loudly, never silently
            # add a duplicate entry.
            _log(
                f"[bibcite] no matching entry to replace for '{query}'"
                + (f" (key: {args.key})" if args.key else "")
                + " — nothing written. Use `bibcite add --key <existing-key>` to target one."
            )
            results.append({"query": query, "action": action, "exit_code": EXIT_NOT_FOUND})
            continue
        wrote = wrote or action != "exists"
        result = {
            "query": query,
            "action": action,
            "key": key,
            "title": res.entry.get("title", ""),
            "venue": res.venue or "arXiv (preprint)",
            "published": res.published,
            "source": res.source,
        }
        if warning:
            result["warning"] = warning
        results.append(result)

    tidied = False
    if wrote and not args.no_tidy:
        tidied = bibfile.run_tidy(path)
        if tidied:
            for r in results:
                if r.get("title"):
                    r["key"] = bibfile.key_after_tidy(path, r["title"], r["key"])

    exit_code = max((r.get("exit_code", 0) for r in results), default=0)
    if len(results) == 1 and not args.from_file:
        _emit({**results[0], "file": str(path), "tidied": tidied})
    else:
        _emit({"file": str(path), "tidied": tidied, "results": results})
    return exit_code


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
        if entry.get("pubstate", "").strip("{}") == "preprint":
            # User-confirmed preprint-only (e.g. never-to-be-published arXiv
            # reports): muted from upgrade and check.
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
        match, status = find_published(title, entry.get("year", ""), aid, hint)
        if not match:
            # "no_published_version" is a trustworthy miss; "sources_unavailable"
            # means the sources were down — do not conclude anything.
            reason = (
                "sources_unavailable" if status == "unavailable" else "no_published_version"
            )
            report.append(
                {"key": entry["ID"], "title": title, "matched": False, "reason": reason}
            )
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
            if match.title:
                # Camera-ready titles drift from arXiv ones; the published
                # title is the correct one to cite.
                entry["title"] = match.title
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
        _log(f"{mark} {r['key']}: {r.get('venue') or r.get('reason', 'no match')}")
    _log(f"[bibcite] {matched} matched, {changed} upgraded{' (dry-run)' if dry_run else ''}")
    return {"upgraded": changed, "matched": matched, "entries": report}


def cmd_upgrade(args) -> int:
    path = Path(args.file)
    if args.no_cache:
        cache.DISABLED = True
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
    by_author: dict[str, list[tuple[str, str]]] = {}  # lastname -> [(key, title)]
    for entry in db.entries:
        key = entry.get("ID", "?")
        nt = norm_title(entry.get("title", ""))
        if nt and nt in seen_titles:
            problems.append({"key": key, "issue": f"duplicate title of {seen_titles[nt]}"})
        elif nt:
            # Near-duplicates (title drift: same first author, similar title)
            # slip past exact matching — exactly how a failed replace plus a
            # re-add pollutes a file.
            last = (
                first_author_last_name(entry["author"]) if entry.get("author") else ""
            )
            for other_key, other_title in by_author.get(last, []):
                if titles_similar(entry.get("title", ""), other_title):
                    problems.append(
                        {
                            "key": key,
                            "issue": f"near-duplicate of {other_key} (title drift?)",
                        }
                    )
                    break
            if last:
                by_author.setdefault(last, []).append((key, entry.get("title", "")))
        seen_titles.setdefault(nt, key)
        for f in ("author", "title", "year"):
            if not entry.get(f):
                problems.append({"key": key, "issue": f"missing {f}"})
        if bibfile.is_preprint(entry) and entry.get("pubstate", "").strip("{}") != "preprint":
            problems.append({"key": key, "issue": "arXiv preprint (try `bibcite upgrade`, or set pubstate = {preprint} to mute)"})
        author = entry.get("author", "")
        letters = "".join(c for c in author if c.isalpha())
        if letters and letters.isupper():
            problems.append({"key": key, "issue": "author names are ALL CAPS"})
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


def cmd_remove(args) -> int:
    """Delete an entry by citation key — the sanctioned way to drop a bad
    entry without hand-editing the file."""
    path = Path(args.file)
    removed = bibfile.remove_entry(path, args.key)
    tidied = False
    if removed and not args.no_tidy:
        tidied = bibfile.run_tidy(path)
    _emit(
        {
            "action": "removed" if removed else "not_found",
            "key": args.key,
            "file": str(path),
            "tidied": tidied,
        }
    )
    return 0 if removed else EXIT_NOT_FOUND


def cmd_fix(args) -> int:
    """One-shot cleanup: upgrade preprints, always tidy, then re-lint."""
    path = Path(args.file)
    if args.no_cache:
        cache.DISABLED = True
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
    g.add_argument("--no-cache", action="store_true", help="bypass the local match cache")
    g.set_defaults(fn=cmd_get)

    a = sub.add_parser("add", help="resolve and write into a .bib file, then run bibtex-tidy (prints JSON)")
    a.add_argument("file", help="target .bib file (created if missing)")
    a.add_argument("query", nargs="*", help="arXiv id / arXiv URL / DOI / title")
    a.add_argument("--bibtex", help="raw BibTeX entry to add instead of a query ('-' reads stdin)")
    a.add_argument("--from", dest="from_file", metavar="FILE", help="batch mode: one query per line (shares rate-limit state, tidies once)")
    a.add_argument("--replace", action="store_true", help="overwrite an existing matching entry (keeps its citation key); errors if nothing matches")
    a.add_argument("--key", metavar="KEY", help="replace exactly the entry with this citation key (for title drift)")
    a.add_argument("--no-tidy", action="store_true")
    a.add_argument("--no-cache", action="store_true", help="bypass the local match cache")
    a.add_argument("--require-published", action="store_true")
    a.set_defaults(fn=cmd_add)

    rm = sub.add_parser("remove", help="delete an entry by citation key (prints JSON)")
    rm.add_argument("file")
    rm.add_argument("key", help="citation key of the entry to remove")
    rm.add_argument("--no-tidy", action="store_true")
    rm.set_defaults(fn=cmd_remove)

    u = sub.add_parser("upgrade", help="match all arXiv entries in a file to their published versions (prints JSON)")
    u.add_argument("file")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--no-tidy", action="store_true")
    u.add_argument("--no-cache", action="store_true", help="bypass the local match cache")
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
    f.add_argument("--no-cache", action="store_true", help="bypass the local match cache")
    f.set_defaults(fn=cmd_fix)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
