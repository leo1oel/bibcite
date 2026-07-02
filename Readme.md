# bibcite

Resolve papers (arXiv id / DOI / title) to canonical, normalized BibTeX, and manage `.bib` files so agents never hand-edit them.

The publication-matching cascade is ported from [PaperMemory](https://github.com/vict0rsch/PaperMemory)'s bibMatcher:
DBLP → Semantic Scholar → Google Scholar → CrossRef → Unpaywall.
A match must have an identical normalized title, a plausible year, and a non-preprint venue.

Venue names are canonicalized against the `@string` table vendored in `src/bibcite/data/strings.bib` (journals / conferences / workshops), including year-aware rules (NIPS before 2018 vs NeurIPS, WACV before 2017).

Entry types are strict: conference/workshop papers become `@inproceedings` + `booktitle`, journal papers `@article` + `journal`, and unpublished arXiv preprints `@misc` + `howpublished = {arXiv preprint arXiv:ID}`.
Types coming from authoritative source BibTeX (DBLP) are preserved.

After every write, the file is formatted with [bibtex-tidy](https://github.com/FlamingTempura/bibtex-tidy) using the canonical flags in `bibfile.TIDY_ARGS` (requires `bibtex-tidy` on PATH or `npx`).

## Install

```bash
# from a local checkout (development)
uv tool install --editable .

# from git, no checkout needed
uv tool install git+https://github.com/leo1oel/bibcite

# once published to PyPI (package name bibcite-cli, command name bibcite)
uv tool install bibcite-cli   # or: uvx --from bibcite-cli bibcite ...

# plus, once (required for the tidy step):
npm install -g bibtex-tidy
```

To use your own venue table instead of the vendored one, set `BIBCITE_STRINGS=/path/to/strings.bib` or place it at `~/.config/bibcite/strings.bib`.

Environment variables that make the sources faster/more reliable:

| Variable | Effect |
|---|---|
| `OPENALEX_API_KEY` | OpenAlex refuses anonymous search with 503 under load; a free key makes it dependable |
| `S2_API_KEY` | Semantic Scholar private quota (~1 req/s) instead of the shared global pool |
| `BIBCITE_MAILTO` | Your contact email for the CrossRef/OpenAlex/Unpaywall polite pools |
| `BIBCITE_CORE_SOURCES` | Override which sources count as "core" for `published_check` verdicts (default `dblp,semanticscholar,crossref,openalex`) |
| `BIBCITE_NO_CACHE=1` | Disable the local match cache |

## Usage

```bash
# Preview the BibTeX for a paper (nothing written)
bibcite get 1706.03762
bibcite get "Attention is all you need"
bibcite get 10.1109/CVPR52688.2022.01167

# Resolve and write into a .bib file, dedupe, then bibtex-tidy; prints the final key
bibcite add refs.bib 2103.14030 --json

# Add a raw BibTeX entry you already have (venue still canonicalized, file still tidied)
bibcite add refs.bib --bibtex "$(pbpaste)"

# Batch add (one query per line; shares rate-limit state, tidies once)
bibcite add refs.bib --from ids.txt

# Overwrite a bad existing entry (keeps its key), or delete one
bibcite add refs.bib <query> --replace
bibcite remove refs.bib <key>

# One-shot cleanup: upgrade preprints → tidy → lint
bibcite fix refs.bib

# Upgrade every arXiv entry in a file to its published version (bibMatcher, CLI-style)
bibcite upgrade refs.bib --dry-run

# Just format, or just lint (check is read-only)
bibcite tidy refs.bib
bibcite check refs.bib
```

`add`/`upgrade`/`check`/`fix`/`remove` print a machine-readable JSON result on stdout (`action`, `key`, `venue`, `source`, ...); all diagnostics go to stderr.
`add` is idempotent: an existing entry returns `action: exists` with its key, and an existing arXiv entry matched to a published version is upgraded in place, keeping its citation key.
Exit codes: 0 success, 2 paper not found (ask for a better identifier), 3 sources/tool failure (retry later).
Successful matches are cached at `~/.cache/bibcite/published.json` (published papers only — preprint status is never cached); bypass with `--no-cache` or `BIBCITE_NO_CACHE=1`.
Entries marked `pubstate = {preprint}` are treated as confirmed preprint-only and muted from `check`/`upgrade`.

## For agents

Never edit `.bib` files by hand.
Call `bibcite add <file> <query> --json` and use the returned `key` in `\cite{...}`.
