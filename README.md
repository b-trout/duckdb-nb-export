# duckdb-nb-export

`duckdb-nb-export` is a CLI for exporting DuckDB UI (`duckdb --ui`) notebooks
to a single static HTML file by re-executing notebook cells against a target
database. It fills the current gap where DuckDB UI notebooks do not have a
Jupyter `nbconvert` equivalent.

Status: alpha, Phase 1, CLI only. Published on PyPI as
[`duckdb-nb-export`](https://pypi.org/project/duckdb-nb-export/). It depends
on DuckDB UI's unofficial internal `ui.db` schema, so a DuckDB UI update can
break notebook discovery or parsing. This is an unofficial third-party tool,
not affiliated with DuckDB, DuckDB Labs, or MotherDuck.

## For users

### Requirements

- Python >=3.11
- `duckdb>=1.5.4`
- Linux and macOS are fully supported. Windows is supported on a
  best-effort basis: reading `ui.db` while DuckDB UI is running is not
  possible on Windows because of OS file locking (copying a file another
  process holds open for read-write fails there). Close DuckDB UI before
  exporting on Windows; once the UI is closed, both the default snapshot
  path and `--require-ui-closed` work.

### Installation

```bash
uv tool install duckdb-nb-export
```

or:

```bash
pip install duckdb-nb-export
```

To install the latest unreleased code from this repository instead, use
`uv tool install git+<repository-url>` or `pip install git+<repository-url>`.

### Quick start

List available notebooks:

```bash
duckdb-nb-export --list
```

Export a notebook:

```bash
duckdb-nb-export "My Notebook" -o report.html
```

By default, the exporter reads DuckDB UI notebooks from
`~/.duckdb/extension_data/ui/ui.db`. If your `ui.db` is stored in a custom
location, pass it explicitly:

```bash
duckdb-nb-export --ui-db ~/duckdb-ui-profiles/work/ui.db --list
duckdb-nb-export "My Notebook" --ui-db ~/duckdb-ui-profiles/work/ui.db -o report.html
```

The default reader path uses a snapshot copy of `ui.db` and its WAL file, so
the command can read notebooks while DuckDB UI is still running. Use
`--require-ui-closed` only when you want to open `ui.db` directly.

Notebook name matching resolves against the display name shown in DuckDB UI
(the current notebook title) first, and falls back to DuckDB UI's internal
notebook name (the `notebook_...` slug stored in `notebooks.name`) only if
no title matches. `--list` always shows the display name. If several
notebooks share the same display name, pass `--notebook-id <id>` (from
`--list`) to select one unambiguously.

### How it works

The exporter copies `ui.db` together with its WAL, reads the notebook
definition, re-executes each SQL cell against the target DuckDB database, and
renders one standalone HTML file. The HTML has inline CSS, supports light and
dark color schemes, and does not reference external resources.

The rendered HTML footer records the export timestamp, DuckDB version,
notebook version, tool version, target database, and write mode, so a
downstream reader can tell how the export was produced without access to the
original command line. The target database line shows only a privacy-safe
display form, never the full connect string or path: `:memory:` is shown
verbatim, URI-style connect strings such as `md:...` or `postgres://...`
(which may embed credentials) show only the scheme, e.g. `md: (URI)`, and
plain file paths show only the basename, e.g. `sales.duckdb`. The write mode
line shows one of `rollback (default)`, `writes committed (--allow-writes)`,
or `read-only`, matching the safety model described below.

During execution, the exporter logs a `cell_started` and `cell_finished`
event (via `structlog`, to stderr) for every cell, including its 1-based
index, the total cell count, and, for `cell_finished`, the resulting status
and duration in seconds. This gives visibility into long-running notebooks
without waiting for the whole export to finish; pipe or `grep` stderr to
follow progress.

If Ctrl-C (SIGINT) is sent while a cell is executing, the exporter attempts
to interrupt the running query and, if that succeeds within
`--interrupt-grace`, rolls back and closes the target database connection
before exiting. If the query cannot be interrupted in time, the connection
is intentionally left untouched (mirroring the uninterruptible-timeout
behavior) rather than risking a hang or a corrupt state.

### Safety model

Exporting a notebook executes its SQL with your privileges. Do not export
notebooks from sources you would not trust enough to run yourself.

By default, notebook cells run inside one transaction and the exporter finishes
with `ROLLBACK`, so changes inside the target database file are not retained.
Use `--allow-writes` only when you want the exporter to commit those changes.
For a stronger no-writes guarantee, pass `--read-only` to open the target
database in DuckDB's read-only mode instead: notebook cells that create or
modify tables then fail outright rather than being rolled back after the
fact. `--read-only` and `--allow-writes` are mutually exclusive, and
`--read-only` cannot be combined with a `:memory:` target (DuckDB cannot open
`:memory:` read-only). The default remains rollback-based (not read-only)
because some analytics notebooks create intermediate tables that are expected
to be rolled back at the end of the run.

With `--allow-writes`, if a cell error or an unrecoverable timeout aborts the
transaction, the exporter never partially commits: it skips the remaining
cells, rolls back the whole transaction instead of committing it, and adds a
warning to the rendered HTML explaining that nothing was committed. This
avoids silently persisting only the writes made before (or, for a
timeout-abort, only after) the point of failure.

`ROLLBACK` cannot undo external side effects such as `COPY ... TO` file writes,
writes to an attached database, remote writes, `INSTALL`, or `LOAD`. The CLI
therefore asks for confirmation before execution; in non-interactive contexts,
use `--yes` to make that confirmation explicit. The confirmation prompt shows
the notebook name, version, cell count, target database, write mode, and
output path, followed by a short masked preview of each cell (first two
non-empty lines, up to 160 characters); URI-style target databases are shown
as their scheme only because URIs can embed credentials. `--no-external-access` runs
with DuckDB external access disabled, which also disables external file reads
such as CSV or Parquet scans.

`CREATE SECRET` parameter values are masked as `***` in rendered SQL. Secrets
written in any other SQL form are not detected and will remain in the HTML.

Masking only covers `CREATE SECRET` statement text; it does not cover every
way a credential can end up in the exported HTML. Credentials embedded in
other SQL forms are exported verbatim: for example, an
`ATTACH 'postgres://user:password@host/db' AS pg;` cell renders its full
connection string, password included, in the rendered SQL. Query *results*
are never masked either: a cell such as `SELECT * FROM duckdb_secrets();`
renders secret values as ordinary table cells, exposing them just like any
other query output. Review the generated HTML before sharing it, and prefer
DuckDB's Secrets Manager (`CREATE SECRET`, whose parameter values are masked)
over inline credentials in `ATTACH` strings or other SQL wherever possible.

If no target database is resolved, execution falls back to `:memory:` and emits
a warning. DuckDB UI notebook JSON stores database names, not reliable file
paths, so pass `--db <path>` for exports that depend on existing tables. This
fallback warning is only emitted when `:memory:` was chosen automatically;
passing `--db :memory:` explicitly does not trigger it.

`--db <path>` must point to an existing DuckDB database file (or `:memory:`,
or a URI-style connect string such as `md:...`); a nonexistent local path is
rejected with exit code 6 instead of silently creating an empty database
file, which usually means the path was mistyped.

Found a security issue, including a `CREATE SECRET` masking bypass? See
[SECURITY.md](SECURITY.md) for how to report it privately.

### CLI reference

The command is registered by `[project.scripts]` as `duckdb-nb-export`.

| Argument or option | Meaning | Default |
| --- | --- | --- |
| `notebook_name` | Notebook name to export. Optional when `--list` is used. | None |
| `-h`, `--help` | Show help and exit. | Off |
| `--version` | Show the tool version (and the DuckDB version in use) and exit. | Off |
| `-o`, `--output` | Output HTML path. | `<notebook-name>.html` under the allowed base |
| `--output-dir` | Allowed base directory and default output directory. | Current directory |
| `--notebook-id` | Export the notebook with this exact ID (from `--list`); use when names are ambiguous. | None |
| `--db` | Target DuckDB database path for notebook re-execution. Must exist (a nonexistent local path is rejected instead of creating a new file). | Resolved from notebook metadata, then `:memory:` |
| `--ui-db` | Path to DuckDB UI `ui.db`. | `~/.duckdb/extension_data/ui/ui.db` |
| `--nb-version` | Notebook version identifier to export. Must be an integer string; an unknown (but well-formed) version is reported as exit code 1 (see `--list-versions`). | Latest version |
| `--list` | List notebooks and exit. | Off |
| `--list-versions` | List versions for the selected notebook and exit. | Off |
| `--json` | With `--list` or `--list-versions`, print the listing as a JSON array instead of a table. | Off |
| `--max-rows` | Maximum rows to render per cell. Must be a positive integer (>= 1). | `1000` |
| `--cell-timeout` | Per-cell execution timeout in seconds. Must be a positive, finite number. | `300.0` |
| `--interrupt-grace` | Seconds to wait after a timeout interrupt before abandoning execution. Must be a positive, finite number. | `30.0` |
| `--stop-on-error` | Stop processing after the first cell error. | Off |
| `--no-fail-on-cell-error` | Exit 0 even when individual cells fail (previous default). Timeouts and abandoned execution still exit 2. | Off |
| `--allow-writes` | Commit notebook changes instead of rolling them back. Mutually exclusive with `--read-only`. | Off |
| `--read-only` | Open the target database in DuckDB read-only mode for a stronger no-writes guarantee. Cells that create or modify tables fail. Mutually exclusive with `--allow-writes`; cannot be combined with a `:memory:` target. | Off |
| `--no-external-access` | Disable DuckDB external access during execution. | Off |
| `--require-ui-closed` | Open `ui.db` directly and require DuckDB UI to be closed. | Off |
| `--yes` | Skip the execution confirmation prompt. | Off |
| `--force` | Overwrite the output file if it exists, instead of writing to a numeric-suffixed sibling path. | Off |
| `-q`, `--quiet` | Only show `ERROR`-level log events on stderr. Mutually exclusive with `-v`/`--verbose`. | Off |
| `-v`, `--verbose` | Show `DEBUG`-level log events on stderr in addition to the default. Mutually exclusive with `-q`/`--quiet`. | Off |

By default, existing output files are not overwritten; a numeric suffix is
added. Pass `--force` to overwrite the requested path in place instead (no
suffix, no dedupe warning). The HTML is written atomically: it is staged in
a temporary file in the destination directory and moved into place with an
atomic rename, so a reader never observes a partially written export, and
the suffixed name is reserved on disk the moment it is chosen so a
concurrent export cannot claim the same path. On success, the CLI prints
the final output path (after any numeric-suffix deduplication) as a single
line to stdout, so scripts can capture it directly; a renamed path also
emits a warning naming the requested and actual paths on stderr.

If `-o`/`--output` points outside the allowed base directory (the current
directory by default, or the directory passed to `--output-dir`), the export
is rejected with exit code 3. To write outside the current directory, pass
that location to `--output-dir` so it becomes the allowed base itself, for
example:

```bash
duckdb-nb-export "My Notebook" --output-dir /tmp -o /tmp/report.html
```

### Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Success. |
| 1 | Notebook not found, notebook name is ambiguous (use `--notebook-id`), or `--nb-version` does not match any stored version of the resolved notebook (use `--list-versions`). |
| 2 | One or more cells failed, timed out, or were skipped, or `--stop-on-error` stopped processing after the first cell error, or a timeout interrupt failed and the export ended partially. A non-integer `--nb-version`, or a non-positive `--max-rows`, `--cell-timeout`, or `--interrupt-grace` value, is also rejected with this code, as a standard argument-parsing error. |
| 3 | Output path rejected because it escapes the allowed base directory. |
| 4 | `ui.db` access failed, including lock, corruption, storage-version mismatch, an unsupported stored notebook format, or a `ui.db` file whose schema does not match what this tool expects. |
| 5 | Execution confirmation was declined, including non-interactive execution without `--yes` and EOF (Ctrl-D) at the confirmation prompt. |
| 6 | Notebook execution or HTML writing failed, including a missing or unusable `--db` target. |
| 130 | Interrupted by Ctrl-C (`128 + SIGINT`), at the confirmation prompt or during execution. |

By default, the exit code fails (exit 2) whenever any cell result is not a
plain success (`ERROR`, `SKIPPED_ABORT`, `REJECTED_TRANSACTION_STATEMENT`,
`TIMEOUT`), or execution was abandoned after an uninterruptible timeout;
`--stop-on-error` additionally stops processing early after the first cell
error. Whenever any cell fails, the CLI logs a `cell_failed` event per
failed cell (1-based cell index, status, and a truncated error message) and
a final `cells_failed_summary` event naming the output path, both on
stderr, before returning the exit code; this happens regardless of
`--no-fail-on-cell-error`. Pass `--no-fail-on-cell-error` to restore the
previous (pre-0.0.3) behavior of exiting 0 whenever the run completes
despite per-cell failures, which are still reported in the rendered HTML
and on stderr; timeouts and abandoned execution still exit 2 even with
this flag, since those indicate the export itself did not run to
completion as requested.

### Limitations

- Chart rendering is not supported, and this is a permanent limitation
  rather than a pending feature: DuckDB UI does not persist chart
  configuration anywhere (neither in notebook JSON nor in any `ui.db`
  table, verified against the UI frontend bundle), so there is no stored
  chart definition an exporter could reproduce. Cells are re-executed and
  displayed as tables. If a future DuckDB UI version starts persisting
  chart settings, chart export will be reconsidered. Chart cells cannot
  be detected in stored notebook data at all: stored format v3 does not
  record whether a cell was displayed as a chart, so exported HTML shows
  such cells as ordinary SQL cells.
- Result display is limited to the first 1,000 rows by default. The total row
  count is not computed.
- Long scalar values are truncated after 500 characters in HTML output.
- The reader depends on DuckDB UI's unofficial schema and stored notebook
  format v3. A stored notebook whose `notebookSerializationFormat` is not 3
  is rejected with a clear error (exit code 4) instead of being exported
  with unverified, potentially incorrect results; a `ui.db` file whose
  `notebooks` / `notebook_versions` tables or expected columns are missing
  is rejected the same way instead of surfacing a raw DuckDB Catalog Error.
- Notebook JSON's `currentDatabase` / `useDatabase` (database name) is
  applied with a best-effort `USE` on re-execution: it switches the database
  only when a catalog with that name is attached (via `--db` or an earlier
  ATTACH cell), and otherwise warns and continues. Database names are not
  resolved to file paths, so still pass `--db <path>` for exports that
  depend on existing tables.

### License

MIT. See [LICENSE](https://github.com/b-trout/duckdb-nb-export/blob/main/LICENSE).

## For developers

### Documentation

- [Design document](https://github.com/b-trout/duckdb-nb-export/blob/main/docs/design/duckdb-notebook-html-export-design.md) (Japanese)
- [Architecture decision records](https://github.com/b-trout/duckdb-nb-export/blob/main/docs/adr/duckdb-notebook-html-export-adr.md) (Japanese)
- [Test design document](https://github.com/b-trout/duckdb-nb-export/blob/main/docs/tests/duckdb-notebook-html-export-test-design.md) (Japanese)
- [Changelog](https://github.com/b-trout/duckdb-nb-export/blob/main/CHANGELOG.md)

### Setup

```bash
uv sync --group dev
uv run pre-commit install
```

### Common tasks

```bash
uv run pytest
uv run poe check
```

Pytest markers: `assumptions` guards DuckDB behavior and the unofficial UI
schema; `integration` covers multi-layer real-file/process tests; `e2e` drives
the real CLI and compares golden HTML.

### Test suite

The project is test-driven: tests describe Phase 1 behavior before the
implementation is treated as complete. Failures in assumption tests (`AT-*`)
are not ordinary implementation bugs; they signal that a documented design
assumption about DuckDB behavior or the unofficial DuckDB UI schema changed.

Golden HTML fixtures are updated with:

```bash
UPDATE_GOLDEN_HTML=1 uv run pytest tests/test_e2e.py
```

### Fixtures

`tests/fixtures/ui_db` is regenerated with:

```bash
uv run python scripts/regenerate_ui_db_fixtures.py
```

The current checked-in fixtures were generated by the fallback SQL generator
because the fixture environment did not have an available browser.

### Architecture

Reader copies and reads `ui.db`; Executor re-executes cells in one transaction;
Renderer builds a single Jinja2/Pygments HTML document. The CLI wires those
layers together. `models/` separates internal notebook models from stored
DuckDB UI format v3 models (`Stored*`).

```text
DuckDB UI ui.db -> Reader -> Executor -> Renderer -> standalone HTML
CLI wiring and validation connects the full pipeline.
```

### CI

GitHub Actions runs lint once on Ubuntu across Python `3.11` and latest Python
3, then runs tests across `ubuntu-latest` and `macos-latest`, Python `3.11` and
latest Python 3, and DuckDB `1.5.4` plus latest DuckDB. Assumption-test
failures on latest DuckDB are non-blocking signals for design review; failures
on DuckDB `1.5.4` remain blocking.

### Roadmap

See design document section 2.1. Phase 1.5 adds a notebook-cell callable
`export_notebook_html()` path after the target-database lock problem is
resolved. Phase 2 targets a C++ rendering core; client-side chart embedding
is dormant because DuckDB UI does not persist chart settings (see
Limitations), and will only be revisited if a future DuckDB UI version
starts persisting them.

### Conventions

Application logging uses `structlog`; do not use `print` in package code.
Docstrings are English and follow NumPy-style sections. User-facing messages
are English.
