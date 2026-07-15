---
name: bibcite
description: Manage paper citations and .bib files through the bibcite CLI instead of hand-editing them. Use whenever a task involves adding a paper reference or \cite, resolving an arXiv ID, arXiv DOI, DOI, or title to BibTeX, cleaning or checking a bibliography, deduplicating entries, or upgrading arXiv preprints to published records.
compatibility: Requires bibcite or uv, network access for paper resolution, and Node.js with npx for automatic formatting.
---

# bibcite

Route every `.bib` change through `bibcite` because the CLI resolves metadata, canonicalizes venues, deduplicates entries, preserves existing citation keys, and formats the file.
Never add, replace, or delete a `.bib` entry by editing the file directly.

## Ensure the CLI is available

Check whether `bibcite` is on `PATH` before the first bibliography command.
If it is missing, install it once with `uv tool install bibcite-cli`, then use the `bibcite` command normally for the rest of the task.
A persistent tool installation avoids resolving and creating an isolated environment on every invocation.
Verify the installation with `bibcite --help`.
If uv reports that its tool directory is not on `PATH`, locate the installed command with `uv tool dir --bin` and invoke that `bibcite` executable by its absolute path for the current task.
Do not edit the user's shell startup files automatically; mention `uv tool update-shell` if they want the command added to future shells.

If the environment is temporary, read-only, or does not allow a persistent tool installation, use `uvx --from bibcite-cli bibcite <subcommand>` as the fallback.
Do not upgrade or reinstall an existing `bibcite` command unless the user asks for an upgrade.

## Choose the command

```bash
bibcite add refs.bib <arXiv ID | arXiv URL | arXiv DOI | DOI | title>
bibcite add refs.bib --bibtex '<complete BibTeX entry>'
bibcite add refs.bib --from queries.txt
bibcite add refs.bib <query> --replace
bibcite add refs.bib <query> --key existingKey
bibcite remove refs.bib <key>
bibcite get <query> [--json]
bibcite upgrade refs.bib [--dry-run]
bibcite check refs.bib
bibcite tidy refs.bib
bibcite fix refs.bib
```

Use `get` only to preview a paper without changing a bibliography.
Use `add` for one paper, and pass an arXiv ID, an arXiv DOI such as `10.48550/arXiv.1706.03762`, a standard DOI, or an exact title.
Use `--from` for multiple papers because one process shares source throttling state and tidies the file once.
Use `--replace` only when the resolved paper should overwrite an automatically matched entry.
Use `--key` when title drift prevents automatic matching and a specific existing citation key must be replaced.
Use `upgrade --dry-run` before upgrading a large bibliography.
Use `fix` when the user asks to clean up a bibliography end to end.

## Use the returned citation key

File-changing commands print a JSON result on standard output and diagnostics on standard error.
Read the `key` from JSON after `add` and use that exact value in `\cite{...}` instead of guessing or reconstructing it.
After a write, confirm that `tidied` is `true` before reporting that formatting completed.
An `exists` result may have `tidied` set to `false` because the file was unchanged, so `add` skipped the formatting pass.
For any other result with `tidied` set to `false`, the command should exit with code `1` after trying both a global `bibtex-tidy` command and automatic download through `npx`.
Run `bibcite tidy <file>` once to retry a transient formatter failure.
Do not install `bibtex-tidy` separately because `bibcite` downloads it through `npx` when needed.
If formatting still fails because Node.js or `npx` is missing, explain the dependency and ask before installing Node.js because that changes the user's system environment.
After the user approves that installation, follow the environment's existing package-manager conventions, then rerun `bibcite tidy <file>`.
Never format the file by hand as a fallback.

Treat these `action` values as successful outcomes:

- `added` means a new entry was written.
- `exists` means the paper was already present and no duplicate was added.
- `upgraded` means a preprint was replaced by its published record while keeping the existing key.
- `replaced` means an explicitly targeted entry was overwritten while keeping its existing key.

For batch commands, inspect every item in `results` because one failed query does not invalidate successful entries.

## Handle publication uncertainty

An unmatched upgrade reports either `no_published_version` or `sources_unavailable`.
Treat `no_published_version` as a trustworthy miss.
Treat `sources_unavailable` as temporary because rate limits or outages prevented a complete check, so retry later without writing a replacement by hand.

A preprint result may include `published_check`.
Treat `complete` as a trustworthy publication check and `incomplete` as a reason to retry later.

An entry that is intentionally preprint-only can use `pubstate = {preprint}` to mute future `check` and `upgrade` warnings.
Submit the complete updated entry through `--bibtex` and target its existing key rather than editing the file directly.

## Handle exit codes

- Exit code `0` means the command completed successfully.
- Exit code `1` means a file, lint, or formatting problem remains.
  Inspect `problems`, `remaining_problems`, and `tidied` before deciding what to do next.
- Exit code `2` means the paper or requested entry was not found.
  Ask for a stronger identifier, preferably an arXiv ID or DOI, instead of fabricating an entry.
- Exit code `3` means publication sources or an internal tool failed.
  Retry later and never fall back to hand-editing the bibliography.

`check` is read-only, but it returns exit code `1` when it finds problems.
`fix` also returns exit code `1` when unresolved lint issues remain or formatting fails.
