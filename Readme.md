<p align="center">
  <img src="assets/bibcite.svg" width="128" alt="bibcite logo">
</p>

<h1 align="center">bibcite</h1>

<p align="center">
  Turn an arXiv ID, DOI, or paper title into clean BibTeX, then keep the whole bibliography normalized and deduplicated.
</p>

<p align="center">
  <a href="https://pypi.org/project/bibcite-cli/"><img alt="PyPI" src="https://img.shields.io/pypi/v/bibcite-cli?color=6366f1"></a>
  <a href="https://pypi.org/project/bibcite-cli/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/bibcite-cli"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/leo1oel/bibcite"></a>
</p>

`bibcite` resolves a paper to its published record when one exists, preserves arXiv links, canonicalizes venue names, and writes the result into a `.bib` file without breaking existing citation keys.
It is built for both terminal use and coding agents that need a dependable alternative to editing BibTeX by hand.

## Quick start

Install the command from PyPI:

```bash
uv tool install bibcite-cli
```

Resolve a paper and add it to your bibliography:

```bash
bibcite add references.bib 1706.03762
```

The command prints a machine-readable result, including the stable citation key:

```json
{
  "query": "1706.03762",
  "action": "added",
  "key": "vaswani2017attention",
  "title": "Attention is All you Need",
  "venue": "Advances in Neural Information Processing Systems (NIPS)",
  "published": true,
  "source": "semanticscholar",
  "file": "references.bib",
  "tidied": true
}
```

You can now cite it as `\cite{vaswani2017attention}`.
Running the same command again is safe: `bibcite` detects the existing entry and does not add a duplicate.

`bibcite` uses [bibtex-tidy](https://github.com/FlamingTempura/bibtex-tidy) for final formatting.
Install it once if you want write commands to tidy the file automatically:

```bash
npm install -g bibtex-tidy
```

You can also try a one-off command without installing `bibcite`:

```bash
uvx --from bibcite-cli bibcite get "Attention is all you need"
```

### Install the agent skill

The repository includes an agent skill that teaches Codex, Claude Code, Cursor, and other compatible agents to route bibliography changes through `bibcite` safely.

```bash
npx -y skills add leo1oel/bibcite --skill bibcite --global --yes
```

## What it handles

- It accepts arXiv IDs and URLs, arXiv DOIs such as `10.48550/arXiv.1706.03762`, standard DOIs, and paper titles.
- It searches for a published version before falling back to an arXiv preprint, and it reports when source outages make that check incomplete.
- It canonicalizes journal, conference, and workshop names against the bundled venue table, including year-sensitive names such as NIPS and NeurIPS.
- It assigns the correct BibTeX entry type and field, such as `@inproceedings` with `booktitle` or `@article` with `journal`.
- It deduplicates by arXiv ID, DOI, exact title, and similar titles from the same first author.
- It upgrades preprints in place while preserving citation keys already used by your LaTeX source.

## Commands

| Command | Purpose |
| --- | --- |
| `bibcite get <query>` | Preview resolved BibTeX without writing a file. |
| `bibcite add <file> <query>` | Resolve, deduplicate, add, and tidy an entry. |
| `bibcite add <file> --bibtex "..."` | Normalize and add a raw BibTeX entry. |
| `bibcite add <file> --from ids.txt` | Add one query per line and tidy once at the end. |
| `bibcite upgrade <file>` | Replace arXiv entries with published records when available. |
| `bibcite check <file>` | Find missing fields, duplicates, preprints, and all-caps author names without changing the file. |
| `bibcite tidy <file>` | Apply the canonical `bibtex-tidy` formatting rules. |
| `bibcite fix <file>` | Upgrade preprints, tidy the file, and run the checks in one command. |
| `bibcite remove <file> <key>` | Remove an entry by citation key. |

### Common workflows

Preview a result as BibTeX or JSON:

```bash
bibcite get 1706.03762
bibcite get 10.1109/CVPR52688.2022.01167 --json
```

Add raw BibTeX from the clipboard:

```bash
pbpaste | bibcite add references.bib --bibtex -
```

Replace a bad entry while keeping its current key:

```bash
bibcite add references.bib "correct paper title" --key existingKey
```

Check what would be upgraded without writing the file:

```bash
bibcite upgrade references.bib --dry-run
```

Mark a confirmed preprint-only entry with `pubstate = {preprint}` if you want `check` and `upgrade` to leave it alone.

## How resolution works

For arXiv IDs and titles, `bibcite` collects paper metadata and checks publication sources in a cascade derived from [PaperMemory](https://github.com/vict0rsch/PaperMemory): DBLP, Semantic Scholar, Google Scholar, Crossref, Unpaywall, and OpenAlex.
A published match must have the same normalized title or pass a guarded title-drift check, have a plausible publication year, and name a non-preprint venue.

Successful published matches are cached at `~/.cache/bibcite/published.json`.
Preprint-only results are never cached because a paper may be published later.
Use `--no-cache` or set `BIBCITE_NO_CACHE=1` to bypass the cache.

## Configuration

Set `BIBCITE_STRINGS=/path/to/strings.bib` to use your own venue table, or place one at `~/.config/bibcite/strings.bib`.

These optional environment variables improve source reliability:

| Variable | Effect |
| --- | --- |
| `OPENALEX_API_KEY` | Uses your OpenAlex quota instead of the anonymous shared pool. |
| `S2_API_KEY` | Uses a private Semantic Scholar quota. |
| `BIBCITE_MAILTO` | Sends your contact email to the Crossref, OpenAlex, and Unpaywall polite pools. |
| `BIBCITE_CORE_SOURCES` | Overrides the sources required for a trustworthy publication check. |
| `BIBCITE_NO_CACHE=1` | Disables the local publication cache. |

## Exit codes and agent use

`add`, `remove`, `upgrade`, `check`, and `fix` print JSON on standard output and send diagnostics to standard error.
This keeps their output easy to parse from scripts and agents.

| Code | Meaning |
| --- | --- |
| `0` | The command completed successfully. |
| `1` | A file, lint, or formatting problem remains. |
| `2` | The paper or requested entry could not be found. |
| `3` | Publication sources or an internal tool failed. |

Agents should call `bibcite add <file> <query>` and use the returned `key` in `\cite{...}`.
They should never modify `.bib` entries directly because doing so bypasses deduplication, venue normalization, and stable-key handling.

## Development

```bash
git clone https://github.com/leo1oel/bibcite.git
cd bibcite
uv sync --all-groups
uv run pytest
```

Install the checkout as an editable command while developing:

```bash
uv tool install --editable .
```

## Releasing

Publishing a GitHub Release triggers `.github/workflows/publish.yml`, which verifies that the release tag matches `v<package-version>`, runs the tests and linter, builds the distributions, and uploads them to PyPI through Trusted Publishing.

Configure the `bibcite-cli` project on PyPI once with this Trusted Publisher identity:

| Field | Value |
| --- | --- |
| Owner | `leo1oel` |
| Repository | `bibcite` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

After that setup, create a GitHub Release with a tag such as `v0.6.0` to publish the matching package version.

## License

`bibcite` is available under the [MIT License](LICENSE).
