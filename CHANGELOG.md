# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/b-trout/duckdb-nb-export/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/b-trout/duckdb-nb-export/releases/tag/v0.0.1
