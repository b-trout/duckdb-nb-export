# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Log output no longer emits ANSI color escape codes when stderr is not a
  terminal (for example when piped or redirected to a file), and the
  `NO_COLOR` environment variable (any value, including an empty string,
  per [no-color.org](https://no-color.org/)) now disables color output even
  when stderr is a terminal. Previously `structlog`'s `ConsoleRenderer` always
  emitted color codes regardless of the output stream or `NO_COLOR`
  ([#46](https://github.com/b-trout/duckdb-nb-export/issues/46)).

### Added

- The exported HTML footer now records the target database and write mode
  used for the export, alongside the existing timestamp/version fields.
  The target database line shows a privacy-safe display form only:
  `:memory:` verbatim, the scheme name for URI-style connect strings (e.g.
  `md: (URI)`; the rest of the string, which may embed credentials, is
  never shown), or the basename only for plain file paths (e.g.
  `sales.duckdb`, never the full path). The write mode line shows one of
  `rollback (default)`, `writes committed (--allow-writes)`, or
  `read-only`
  ([#56](https://github.com/b-trout/duckdb-nb-export/issues/56)).
- The executor now logs a `cell_started` event before, and a
  `cell_finished` event after, every notebook cell at INFO level via
  `structlog` (to stderr), including the 1-based cell index, total cell
  count, and, for `cell_finished`, the resulting status and duration in
  seconds. This gives progress feedback during long-running exports
  instead of silence until the whole notebook finishes
  ([#51](https://github.com/b-trout/duckdb-nb-export/issues/51)).
- `--read-only` to open the target database in DuckDB read-only mode for a
  stronger no-writes guarantee than the default rollback-based safety net.
  Notebook cells that create or modify tables then fail outright instead
  of being rolled back after the fact. Mutually exclusive with
  `--allow-writes`; cannot be combined with a `:memory:` target. The
  default execution mode is unchanged (rollback-based, not read-only)
  because some analytics notebooks create intermediate tables that are
  expected to be rolled back
  ([#31](https://github.com/b-trout/duckdb-nb-export/issues/31)).
- `--interrupt-grace` exposes the seconds to wait after a timeout interrupt
  before abandoning execution, previously only reachable programmatically
  through `execute_notebook`'s `interrupt_grace` parameter (default: `30.0`,
  unchanged)
  ([#37](https://github.com/b-trout/duckdb-nb-export/issues/37)).
- On success, the CLI now prints the final output path (after any
  numeric-suffix deduplication) as a single line to stdout, so scripts can
  capture it without re-deriving it from `-o`/`--output-dir`. When the
  requested path already exists and a numeric suffix is applied, a
  structured `output_path_deduplicated` warning naming the requested and
  actual paths is now emitted on stderr before the file is written
  ([#35](https://github.com/b-trout/duckdb-nb-export/issues/35)).
- `SECURITY.md` documents how to report vulnerabilities privately (GitHub
  private vulnerability reporting), the best-effort 14-day acknowledgement
  expectation for this solo-maintainer alpha project, the
  latest-release-only support policy, and scope notes distinguishing
  by-design behavior (executing notebook SQL with the caller's
  privileges; the already-documented `CREATE SECRET` masking gaps) from
  in-scope reports (masking bypasses and other vulnerabilities). Linked
  from the README's Safety model section
  ([#65](https://github.com/b-trout/duckdb-nb-export/issues/65)).
- `.github/dependabot.yml` now proposes weekly update PRs for GitHub
  Actions pins and Python dependencies (grouped by minor/patch to reduce
  PR noise); previously nothing updated action tags or `uv.lock`
  automatically. The Python ecosystem entry uses Dependabot's `pip`
  ecosystem rather than a dedicated `uv` ecosystem, since first-class
  uv-lockfile support in Dependabot was not confirmed at the time of
  writing; see the comment in `dependabot.yml` for the tradeoff.
- `.github/workflows/pip-audit-watch.yml` runs `pip-audit` on a weekly
  schedule (mirroring `assumption-watch.yml`'s pattern) and files or
  updates a `dependency-vulnerability`-labeled GitHub issue on failure.
  Previously `pip-audit` only ran as a pre-commit hook inside CI's lint
  job, so a newly disclosed vulnerability produced no signal at all
  during idle periods with no pull request activity
  ([#63](https://github.com/b-trout/duckdb-nb-export/issues/63)).

### Changed

- **Breaking:** `--nb-version` now rejects a non-integer value at the
  argument-parsing stage (exit code 2) instead of reaching the reader and
  failing with a `ui.db access failed` message (exit code 4). An unknown
  but well-formed version now raises `NotebookNotFoundError` (exit code 1)
  instead of `UiDbAccessError` (exit code 4), and its message names the
  resolved notebook's display name (never a raw `None`) and points at
  `--list-versions`
  ([#48](https://github.com/b-trout/duckdb-nb-export/issues/48)).
- **Breaking:** A stored notebook whose `notebookSerializationFormat` is
  not 3 now hard-fails with a new `UnsupportedNotebookFormatError` (exit
  code 4) instead of being silently parsed and exported with no warning.
  Only stored notebook format v3 is supported by this release
  ([#58](https://github.com/b-trout/duckdb-nb-export/issues/58)).
- The `publish.yml` release workflow now runs the full pytest suite before
  building, asserts (on tag pushes) that the pushed tag matches
  `__version__`, smoke-tests the TestPyPI install with a retry loop before
  promoting to production PyPI (`pip install ... && duckdb-nb-export
  --help`), and creates a GitHub Release from the tag using the matching
  CHANGELOG.md section as release notes after a successful PyPI publish.
  Previously, any tagged commit was published unconditionally with no test
  run, no tag/version consistency check, no install verification, and no
  Release. `workflow_dispatch` rehearsals still stop before PyPI (now at
  the smoke-test job) and never create a Release
  ([#59](https://github.com/b-trout/duckdb-nb-export/issues/59)).
- CI now also runs on every push to `main`, not only on pull requests. A
  PR's checks previously ran against the merge ref as of the last push to
  the PR, not as of merge time, so a second PR merged in between could
  land an untested combination on `main` (a "stale merge"); direct pushes
  to `main` also ran no CI at all. The existing concurrency group already
  dedupes overlapping runs, so this does not add queue pile-up
  ([#61](https://github.com/b-trout/duckdb-nb-export/issues/61)).
- `--max-rows` and `--cell-timeout` now reject non-positive values
  (`--max-rows` must be an integer >= 1; `--cell-timeout` and
  `--interrupt-grace` must be positive, finite numbers) with a clear
  argparse error instead of silently accepting values such as `0` or a
  negative number that would otherwise produce confusing downstream
  behavior
  ([#37](https://github.com/b-trout/duckdb-nb-export/issues/37)).
- **Breaking:** The exit code now fails by default whenever any notebook
  cell result is not a plain success. Previously, without
  `--stop-on-error`, a run that completed exited 0 even if individual
  cells returned `ERROR`, `SKIPPED_ABORT`, or
  `REJECTED_TRANSACTION_STATEMENT` (only `TIMEOUT` and abandoned execution
  already failed the exit code); this made it easy to miss cell failures
  in automated pipelines that only check the process exit code. The CLI
  now returns `ExitCode.CELL_ERROR` (2) whenever any cell result is not
  `CellStatus.OK`, or `report.abandoned` is true, without requiring
  `--stop-on-error`. Pass the new `--no-fail-on-cell-error` flag to
  restore the previous behavior (exit 0 despite per-cell failures;
  timeouts and abandoned execution still exit 2). This project is still
  alpha (0.0.x); the change is called out here because it affects
  CI/automation exit-code checks
  ([#33](https://github.com/b-trout/duckdb-nb-export/issues/33)).
- Execution-phase failures (notebook re-execution or HTML writing) now exit
  with a dedicated exit code 6 instead of exit code 4. Exit code 4 is now
  reserved strictly for `ui.db` access failures (lock, corruption, or
  storage-version mismatch); a missing or unusable `--db` target, or a
  failure while writing the output HTML, previously returned the same exit
  code 4 as a `ui.db` access failure even though the cause was unrelated to
  `ui.db`
  ([#34](https://github.com/b-trout/duckdb-nb-export/issues/34)).
- Documented that `CREATE SECRET` masking does not cover every way a
  credential can leak into the exported HTML: connection strings embedded
  in other SQL forms (for example `ATTACH 'postgres://user:password@host/db'
  AS pg;`) are exported verbatim, and query results that expose secret
  values (for example `SELECT * FROM duckdb_secrets();`) are rendered as
  ordinary table cells with no masking. Recommend reviewing exported HTML
  before sharing it and preferring `CREATE SECRET` over inline credentials
  ([#39](https://github.com/b-trout/duckdb-nb-export/issues/39)).
- Documented that chart cells cannot be detected in stored notebook data
  at all: stored notebook format v3 does not record whether a cell was
  displayed as a chart in DuckDB UI, so exported HTML always shows such
  cells as ordinary SQL cells. The renderer's chart-fallback note is
  therefore unreachable for notebooks read from `ui.db` and is only
  exercised by programmatic callers that construct a `Cell` with
  `cell_type="chart"` directly
  ([#36](https://github.com/b-trout/duckdb-nb-export/issues/36)).

### Fixed

- A `ui.db` path that opens as a valid DuckDB file but does not have the
  expected `notebooks` / `notebook_versions` tables or columns (for
  example, an unrelated DuckDB file passed as `--ui-db`, or a future
  DuckDB UI schema change) now raises a clear `UiDbAccessError` explaining
  that the database does not look like a DuckDB UI `ui.db`, instead of a
  misleading "Cannot open" message that leaked the internal preflight
  query and a raw DuckDB Catalog Error
  ([#60](https://github.com/b-trout/duckdb-nb-export/issues/60)).
- `copy_ui_db` no longer retries deterministic copy failures (out of disk
  space, permission denied, read-only filesystem, or a destination path
  that is too long) three times before raising a misleading "the UI may
  be running" hint. These errno-classified `OSError`s now fail
  immediately with a message naming the real cause and the destination
  temp directory. Transient/unclassified failures (for example, a
  genuine validation failure while the UI is writing to `ui.db`) still
  retry and raise the existing "UI may be running" hint unchanged
  ([#64](https://github.com/b-trout/duckdb-nb-export/issues/64)).
- Ctrl-C (SIGINT) during a running cell no longer risks a native DuckDB
  abort (`terminate called without an active exception`, `SIGABRT`) at
  interpreter teardown. `KeyboardInterrupt` arriving while a cell's worker
  thread is running is now handled explicitly: the connection is
  interrupted and, if the worker thread returns within
  `--interrupt-grace`, the transaction is rolled back and the connection
  is closed before `KeyboardInterrupt` is re-raised. If the worker thread
  is still uninterruptible after the grace period, the connection is
  deliberately left untouched, mirroring the existing uninterruptible-
  timeout behavior, since DuckDB serializes operations per connection and
  touching it again could block forever
  ([#57](https://github.com/b-trout/duckdb-nb-export/issues/57)).
- An explicit `--db :memory:` no longer emits the "No target database was
  resolved; executing against `:memory:`." warning. Previously, the CLI
  discarded the `used_memory_fallback` flag returned by
  `resolve_target_db`, so `execute_notebook` always recomputed it from
  `db == ":memory:"` and warned even when `:memory:` was requested
  explicitly rather than being a fallback
  ([#49](https://github.com/b-trout/duckdb-nb-export/issues/49)).
- Stale `ui.db` snapshot directories left behind by a crashed or killed
  process no longer accumulate indefinitely: each snapshot-path call to
  `open_ui_db` now opportunistically removes snapshot directories older
  than 24 hours before creating a new one
  ([#38](https://github.com/b-trout/duckdb-nb-export/issues/38)).
- `mask_secrets` no longer corrupts multi-statement cells. Masking was
  previously scoped to the whole cell text via `sql.find("(")` /
  `sql.rfind(")")`, so a cell containing a `CREATE SECRET` statement
  followed (or preceded) by other statements would silently drop or
  mangle those other statements in the rendered HTML. Masking is now
  scoped per statement using `duckdb.extract_statements`, and falls back
  to the previous whole-text masking if statement splitting itself fails
  ([#29](https://github.com/b-trout/duckdb-nb-export/issues/29)).
- An uninterruptible timeout no longer touches the database connection
  again from the main thread. Previously, after abandoning a cell whose
  worker thread could not be interrupted, the exporter still issued a
  final `COMMIT`/`ROLLBACK` and `close()` on that same connection, which
  DuckDB serializes per connection, so the process could block forever
  behind the stuck query instead of producing partial HTML and exiting
  ([#28](https://github.com/b-trout/duckdb-nb-export/issues/28)).
- A mistyped `--db` path no longer silently creates a new, empty DuckDB
  database file. `execute_notebook` now rejects a plain local `--db` path
  that does not already exist with a clear error (exit code 6) before
  connecting, instead of producing an error-filled HTML export with exit
  code 0. `:memory:` and URI-style connect strings (for example `md:...`,
  `s3://...`) are unaffected
  ([#30](https://github.com/b-trout/duckdb-nb-export/issues/30)).
- `--allow-writes` no longer risks a silent partial commit after the
  transaction is aborted mid-run. A timeout that aborts the transaction is
  now treated as terminal instead of being rolled back and silently
  restarted in a new transaction (which previously caused the final
  `COMMIT` to persist only the writes made after the timeout); an error
  that aborts the transaction now also skips the remaining cells and
  rolls back instead of attempting a `COMMIT` on an aborted transaction
  (which previously could abort the whole export with no HTML written at
  all). Both cases now complete the export normally, roll back every
  change, and add a prominent warning to the rendered HTML explaining
  that nothing was committed
  ([#32](https://github.com/b-trout/duckdb-nb-export/issues/32)).

## [0.0.2] - 2026-07-06

### Added

- Windows support on a best-effort basis, with `windows-latest` added to
  CI: reading `ui.db` while DuckDB UI is running is verified impossible on
  Windows due to OS file locking, so the UI must be closed before
  exporting there; the handful of tests that depend on the locked-copy
  behavior are skipped on Windows accordingly
  ([#5](https://github.com/b-trout/duckdb-nb-export/issues/5)).
- Best-effort database-name replay: the notebook's stored
  `currentDatabase` is applied with `USE` after the transaction starts,
  and a cell's stored `useDatabase` is applied before that cell runs.
  When the name does not match an attached catalog, the export warns
  and continues with the current database
  ([#7](https://github.com/b-trout/duckdb-nb-export/issues/7)).

### Changed

- The `pygments` dependency floor was raised from `>=2` to `>=2.19`.
  CI now verifies every declared dependency floor by resolving direct
  dependencies at their lowest allowed versions and running the full
  test suite; that check found the old floor produced HTML styling
  that no longer matches current output
  ([#9](https://github.com/b-trout/duckdb-nb-export/issues/9)).
- `--help` now shows the default value for `--max-rows`, `--cell-timeout`,
  `-o`/`--output`, `--output-dir`, and `--db` instead of documenting them
  only in the README
  ([#15](https://github.com/b-trout/duckdb-nb-export/issues/15)).
- The error for a declined non-interactive confirmation now names the fix:
  `Execution confirmation required; pass --yes to run non-interactively.`
  ([#15](https://github.com/b-trout/duckdb-nb-export/issues/15)).
- Documented that the lack of chart support is a permanent limitation
  rather than an unimplemented Phase 1 feature: DuckDB UI does not
  persist chart configuration anywhere, so there is no stored chart
  definition an exporter could reproduce
  ([#6](https://github.com/b-trout/duckdb-nb-export/issues/6)). The
  note rendered in exported HTML for chart cells was updated to match
  this wording (it previously said "not supported in Phase 1").
- CI's "latest" duckdb leg now actually installs and tests against the
  newest PyPI release. An unconstrained `uv run --with duckdb` silently
  reused the version already locked by `uv sync`, so that leg had been
  testing the same duckdb as the pinned-floor leg.

### Fixed

- Notebook name resolution now matches the display name shown in DuckDB UI
  (`notebook_versions.title`); previously the internal slug in
  `notebooks.name` was used, so `--list` showed slugs and display-name
  lookups failed
  ([#8](https://github.com/b-trout/duckdb-nb-export/issues/8)).

### Known limitations

- No chart rendering. This is a permanent limitation, not a pending
  feature: DuckDB UI does not persist chart configuration anywhere.
- Environment replay is limited to the database name (`USE`, best
  effort); ATTACH, extensions, secrets, and variables are not
  reproduced because stored notebooks do not record them.
- Windows is supported on a best-effort basis: reading `ui.db` while
  DuckDB UI is running is not possible there.

## [0.0.1] - 2026-07-05

Initial alpha release.

### Added

- CLI export of DuckDB UI notebooks to a standalone HTML file by
  re-executing notebook cells against a target database, inside a single
  transaction that finishes with `ROLLBACK` by default.
- A snapshot-copy reader that can read `ui.db` while DuckDB UI is still
  running.
- A safety model covering output path confinement, `CREATE SECRET`
  parameter masking, an execution confirmation prompt, and
  `--no-external-access` to disable DuckDB external access.
- `--notebook-id` to disambiguate notebooks that share a name.
- `--list` and `--list-versions` to list notebooks and notebook versions.
- Single-file HTML rendering with inline CSS supporting light and dark
  color schemes.

### Known limitations

- No chart rendering, no notebook environment (database) replay, and no
  verified Windows support.

[Unreleased]: https://github.com/b-trout/duckdb-nb-export/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/b-trout/duckdb-nb-export/releases/tag/v0.0.2
[0.0.1]: https://github.com/b-trout/duckdb-nb-export/releases/tag/v0.0.1
