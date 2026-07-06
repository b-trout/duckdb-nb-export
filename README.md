# duckdb-nb-export

`duckdb-nb-export` is a CLI for exporting DuckDB UI (`duckdb --ui`) notebooks
to a single static HTML file by re-executing notebook cells against a target
database. It fills the current gap where DuckDB UI notebooks do not have a
Jupyter `nbconvert` equivalent.

Status: alpha, Phase 1, CLI only. The package is not published on PyPI yet.
It depends on DuckDB UI's unofficial internal `ui.db` schema, so a DuckDB UI
update can break notebook discovery or parsing. This is an unofficial
third-party tool, not affiliated with DuckDB, DuckDB Labs, or MotherDuck.

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

Until PyPI publication, install from a repository URL:

```bash
uv tool install git+<repository-url>
```

or:

```bash
pip install git+<repository-url>
```

The intended future install command is:

```bash
pip install duckdb-nb-export
```

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

### Safety model

Exporting a notebook executes its SQL with your privileges. Do not export
notebooks from sources you would not trust enough to run yourself.

By default, notebook cells run inside one transaction and the exporter finishes
with `ROLLBACK`, so changes inside the target database file are not retained.
Use `--allow-writes` only when you want the exporter to commit those changes.

`ROLLBACK` cannot undo external side effects such as `COPY ... TO` file writes,
writes to an attached database, remote writes, `INSTALL`, or `LOAD`. The CLI
therefore asks for confirmation before execution; in non-interactive contexts,
use `--yes` to make that confirmation explicit. `--no-external-access` runs
with DuckDB external access disabled, which also disables external file reads
such as CSV or Parquet scans.

`CREATE SECRET` parameter values are masked as `***` in rendered SQL. Secrets
written in any other SQL form are not detected and will remain in the HTML.

If no target database is resolved, execution falls back to `:memory:` and emits
a warning. DuckDB UI notebook JSON stores database names, not reliable file
paths, so pass `--db <path>` for exports that depend on existing tables.

### CLI reference

The command is registered by `[project.scripts]` as `duckdb-nb-export`.

| Argument or option | Meaning | Default |
| --- | --- | --- |
| `notebook_name` | Notebook name to export. Optional when `--list` is used. | None |
| `-h`, `--help` | Show help and exit. | Off |
| `-o`, `--output` | Output HTML path. | `<notebook-name>.html` under the allowed base |
| `--output-dir` | Allowed base directory and default output directory. | Current directory |
| `--notebook-id` | Export the notebook with this exact ID (from `--list`); use when names are ambiguous. | None |
| `--db` | Target DuckDB database path for notebook re-execution. | Resolved from notebook metadata, then `:memory:` |
| `--ui-db` | Path to DuckDB UI `ui.db`. | `~/.duckdb/extension_data/ui/ui.db` |
| `--nb-version` | Notebook version identifier to export. | Latest version |
| `--list` | List notebooks and exit. | Off |
| `--list-versions` | List versions for the selected notebook and exit. | Off |
| `--max-rows` | Maximum rows to render per cell. | `1000` |
| `--cell-timeout` | Per-cell execution timeout in seconds. | `300.0` |
| `--stop-on-error` | Stop processing after the first cell error. | Off |
| `--allow-writes` | Commit notebook changes instead of rolling them back. | Off |
| `--no-external-access` | Disable DuckDB external access during execution. | Off |
| `--require-ui-closed` | Open `ui.db` directly and require DuckDB UI to be closed. | Off |
| `--yes` | Skip the execution confirmation prompt. | Off |

Existing output files are not overwritten; a numeric suffix is added.

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
| 1 | Notebook not found, or notebook name is ambiguous (use `--notebook-id`). |
| 2 | Cell execution stopped because `--stop-on-error` was set, or a timeout interrupt failed and the export ended partially. |
| 3 | Output path rejected because it escapes the allowed base directory. |
| 4 | `ui.db` access or export setup failed, including lock, corruption, or storage-version mismatch. |
| 5 | Execution confirmation was declined, including non-interactive execution without `--yes`. |

Without `--stop-on-error`, a run that completes exits 0 even if individual
cells failed; per-cell failures are reported in the rendered HTML and on
stderr, not through the exit code. Use `--stop-on-error` if you need failure
detection through the exit code, for example in CI.

### Limitations

- Chart rendering is not supported, and this is a permanent limitation
  rather than a pending feature: DuckDB UI does not persist chart
  configuration anywhere (neither in notebook JSON nor in any `ui.db`
  table, verified against the UI frontend bundle), so there is no stored
  chart definition an exporter could reproduce. Cells are re-executed and
  displayed as tables. If a future DuckDB UI version starts persisting
  chart settings, chart export will be reconsidered.
- Result display is limited to the first 1,000 rows by default. The total row
  count is not computed.
- Long scalar values are truncated after 500 characters in HTML output.
- The reader depends on DuckDB UI's unofficial schema and stored notebook
  format v3.
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
