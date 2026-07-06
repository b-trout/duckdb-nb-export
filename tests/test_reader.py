"""Unit tests for the DuckDB UI notebook reader layer."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import duckdb
import pytest

from duckdb_ui_notebook_export.exceptions import (
    AmbiguousNotebookError,
    NotebookNotFoundError,
    StorageVersionMismatchError,
    UiDbAccessError,
)
from duckdb_ui_notebook_export.models import Cell, Notebook
from duckdb_ui_notebook_export.reader import (
    _cleanup_stale_snapshots,
    copy_ui_db,
    list_notebooks,
    list_versions,
    load_notebook,
    open_ui_db,
)


def _ui_db_wal_path(ui_db_path: Path) -> Path:
    """Return the companion WAL path for a DuckDB UI database.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` database file.

    Returns
    -------
    pathlib.Path
        Path to the companion ``ui.db.wal`` file.
    """
    return ui_db_path.with_name(f"{ui_db_path.name}.wal")


def _build_readable_duckdb(ui_db_path: Path) -> None:
    """Create a closed DuckDB file that can be opened read-only.

    Parameters
    ----------
    ui_db_path
        Destination database path.

    Returns
    -------
    None
        The helper creates the database file in place.
    """
    with duckdb.connect(str(ui_db_path)) as connection:
        connection.execute("CREATE TABLE marker(id INTEGER PRIMARY KEY, label VARCHAR)")
        connection.execute("INSERT INTO marker VALUES (1, 'copied')")


def _assert_marker_table_readable(ui_db_path: Path) -> None:
    """Assert that a copied DuckDB database contains the marker table.

    Parameters
    ----------
    ui_db_path
        Path to the copied DuckDB database file.

    Returns
    -------
    None
        The helper raises an assertion error if the marker row is unavailable.
    """
    with duckdb.connect(str(ui_db_path), read_only=True) as connection:
        rows = connection.execute("SELECT id, label FROM marker").fetchall()

    assert rows == [(1, "copied")]


@pytest.fixture
def synthetic_ui_db(tmp_path: Path) -> Path:
    """Build a synthetic ui.db, skipping with the builder's unsupported reason.

    Parameters
    ----------
    tmp_path
        Temporary directory where the generated database should be written.

    Returns
    -------
    pathlib.Path
        Path to a generated ui.db file.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    try:
        return build_ui_db(
            [
                {
                    "name": "reader-notebook",
                    "notebook_id": "nb-reader",
                    "versions": [
                        {
                            "version_id": "1",
                            "created_at": "2026-07-05T00:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 1"}],
                        },
                        {
                            "version_id": "2",
                            "created_at": "2026-07-05T01:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 2"}],
                        },
                    ],
                },
                {
                    "name": "duplicate",
                    "notebook_id": "nb-duplicate-a",
                    "updated_at": "2026-07-05T02:00:00Z",
                    "versions": [
                        {
                            "version_id": "dup-a-v1",
                            "created_at": "2026-07-05T02:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 'a'"}],
                        }
                    ],
                },
                {
                    "name": "duplicate",
                    "notebook_id": "nb-duplicate-b",
                    "updated_at": "2026-07-05T03:00:00Z",
                    "versions": [
                        {
                            "version_id": "dup-b-v1",
                            "created_at": "2026-07-05T03:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 'b'"}],
                        }
                    ],
                },
            ],
            tmp_path,
        )
    except NotImplementedError as error:
        pytest.skip(str(error))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows cannot copy a locked DuckDB file (design doc 6.3#11); "
        "snapshot-while-UI-running is Linux/macOS only"
    ),
)
def test_ut_r_001_wal_copied_together(tmp_path: Path) -> None:
    """UT-R-001: copy_ui_db copies ui.db and its companion WAL.

    Parameters
    ----------
    tmp_path
        Temporary directory used for the source and copied databases.

    Returns
    -------
    None
        The test asserts that both copied files exist and are readable.

    Notes
    -----
    The writer connection intentionally remains open so DuckDB has a chance to
    leave uncheckpointed state in ``ui.db.wal`` while the snapshot is copied.
    """
    source = tmp_path / "source" / "ui.db"
    source.parent.mkdir()
    destination = tmp_path / "snapshot"
    destination.mkdir()

    connection = duckdb.connect(str(source))
    try:
        connection.execute("CREATE TABLE marker(id INTEGER PRIMARY KEY, label VARCHAR)")
        connection.execute("INSERT INTO marker VALUES (1, 'copied')")
        assert _ui_db_wal_path(source).exists()

        copied = copy_ui_db(source, destination, retries=1, retry_wait=0.01)

        assert copied.exists()
        assert _ui_db_wal_path(copied).exists()
        _assert_marker_table_readable(copied)
    finally:
        connection.close()


def test_ut_r_002_copy_without_wal_is_readable(tmp_path: Path) -> None:
    """UT-R-002: copy_ui_db produces a readable copy when no WAL exists.

    Parameters
    ----------
    tmp_path
        Temporary directory used for the source and copied databases.

    Returns
    -------
    None
        The test asserts that the copied database opens read-only.
    """
    source = tmp_path / "source" / "ui.db"
    source.parent.mkdir()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    _build_readable_duckdb(source)
    _ui_db_wal_path(source).unlink(missing_ok=True)

    copied = copy_ui_db(source, destination, retries=1, retry_wait=0.01)

    assert copied.exists()
    assert not _ui_db_wal_path(copied).exists()
    _assert_marker_table_readable(copied)


def test_ut_r_003_successful_validation_does_not_retry(tmp_path: Path) -> None:
    """UT-R-003: copy_ui_db returns promptly when validation succeeds.

    Parameters
    ----------
    tmp_path
        Temporary directory used for the source and copied databases.

    Returns
    -------
    None
        The test asserts that a successful validation does not incur retry
        sleeps.
    """
    source = tmp_path / "source" / "ui.db"
    source.parent.mkdir()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    _build_readable_duckdb(source)

    started_at = time.monotonic()
    copied = copy_ui_db(source, destination, retries=3, retry_wait=1.0)
    elapsed = time.monotonic() - started_at

    assert copied.exists()
    assert elapsed < 1.0


def test_ut_r_004_failed_validation_retries_with_short_wait(tmp_path: Path) -> None:
    """UT-R-004: copy_ui_db retries validation failures before giving up.

    Parameters
    ----------
    tmp_path
        Temporary directory used for the corrupt source database.

    Returns
    -------
    None
        The test asserts that retry waits are observed for a corrupt database.

    Notes
    -----
    A small explicit ``retry_wait`` keeps the test fast while still exercising
    retry timing with a real unreadable DuckDB file.
    """
    source = tmp_path / "ui.db"
    source.write_bytes(b"not a duckdb database")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    started_at = time.monotonic()
    with pytest.raises(UiDbAccessError):
        copy_ui_db(source, destination, retries=3, retry_wait=0.01)
    elapsed = time.monotonic() - started_at

    assert elapsed >= 0.02


def test_ut_r_005_exhausted_retries_return_clear_ui_running_message(
    tmp_path: Path,
) -> None:
    """UT-R-005: copy_ui_db reports a clear message after retry exhaustion.

    Parameters
    ----------
    tmp_path
        Temporary directory used for the corrupt source database.

    Returns
    -------
    None
        The test asserts that the final error tells the user how to retry.
    """
    source = tmp_path / "ui.db"
    source.write_bytes(b"not a duckdb database")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    with pytest.raises(UiDbAccessError, match=r"UI.*running|require-ui-closed|retry"):
        copy_ui_db(source, destination, retries=3, retry_wait=0.01)


def test_ut_r_006_missing_notebook_lists_available_names(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-006: load_notebook lists available names for a missing notebook.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that the not-found error includes available names.
    """
    with pytest.raises(NotebookNotFoundError) as error_info:
        load_notebook(synthetic_ui_db, "does-not-exist")

    message = str(error_info.value)
    assert "does-not-exist" in message
    assert "reader-notebook" in message
    assert "duplicate" in message


def test_ut_r_007_duplicate_notebook_lists_ambiguous_candidates(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-007: load_notebook reports duplicate-name candidates.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that ambiguity is reported with IDs and timestamps.
    """
    with pytest.raises(AmbiguousNotebookError) as error_info:
        load_notebook(synthetic_ui_db, "duplicate")

    message = str(error_info.value)
    assert "duplicate" in message
    assert "nb-duplicate-a" in message
    assert "nb-duplicate-b" in message
    assert "2026-07-05" in message


def test_ut_r_008_load_notebook_defaults_to_latest_version(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-008: load_notebook selects the latest version by default.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that the newest version is returned when unspecified.

    Notes
    -----
    Reader version IDs mirror integer ``notebook_versions.version`` values.
    """
    notebook = load_notebook(synthetic_ui_db, "reader-notebook")

    assert notebook.version_id == "2"
    assert [cell.sql for cell in notebook.cells] == ["SELECT 2"]


def test_ut_r_009_load_notebook_uses_requested_version(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-009: load_notebook returns the requested notebook version.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that ``version_id`` selects an older version.

    Notes
    -----
    Reader version IDs mirror integer ``notebook_versions.version`` values.
    """
    notebook = load_notebook(synthetic_ui_db, "reader-notebook", version_id="1")

    assert notebook.version_id == "1"
    assert [cell.sql for cell in notebook.cells] == ["SELECT 1"]


def test_ut_r_010_models_allow_unknown_notebook_and_cell_fields() -> None:
    """UT-R-010: Notebook and Cell parsing tolerates unknown JSON fields.

    Returns
    -------
    None
        The test asserts that future schema fields are preserved.
    """
    notebook = Notebook.model_validate(
        {
            "name": "forward-compatible",
            "version_id": "version-extra",
            "database_info": {"path": ":memory:"},
            "notebook_future_field": {"layout": "grid"},
            "cells": [
                {
                    "cell_type": "sql",
                    "sql": "SELECT 1",
                    "cell_future_field": {"chart": "bar"},
                }
            ],
        }
    )

    assert notebook.model_extra is not None
    assert notebook.model_extra["notebook_future_field"] == {"layout": "grid"}
    assert isinstance(notebook.cells[0], Cell)
    assert notebook.cells[0].model_extra is not None
    assert notebook.cells[0].model_extra["cell_future_field"] == {"chart": "bar"}


def test_ut_r_011_list_notebooks_uses_explicit_ui_db_path(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-011: list_notebooks reads the explicit --ui-db path.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that notebooks from the given path are listed.
    """
    notebooks = list_notebooks(synthetic_ui_db)

    assert {notebook.name for notebook in notebooks} >= {
        "reader-notebook",
        "duplicate",
    }


def test_ut_r_012_require_ui_closed_reads_directly(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-012: load_notebook directly reads ui.db with require_ui_closed.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that direct reading succeeds when no UI lock exists.
    """
    notebook = load_notebook(
        synthetic_ui_db,
        "reader-notebook",
        require_ui_closed=True,
    )

    assert notebook.name == "reader-notebook"
    assert notebook.cells


def test_ut_r_013_newer_storage_version_reports_duckdb_upgrade() -> None:
    """UT-R-013: newer storage versions tell users to upgrade duckdb.

    Returns
    -------
    None
        The test asserts that storage-version errors use actionable English.
    """
    fixture_dir = Path(__file__).parent / "fixtures" / "storage_version"
    candidates = [
        path
        for path in fixture_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ]
    if not candidates:
        pytest.skip("storage version fixture not present")

    try:
        duckdb.connect(str(candidates[0]), read_only=True).close()
    except duckdb.Error:
        pass
    else:
        # Staleness rule from test design doc 2.4: once the environment's
        # duckdb can open the fixture, the mismatch path is unexercisable
        # until the fixture is rebuilt with a newer DuckDB.
        pytest.skip(
            "storage version fixture is stale (readable by this duckdb); "
            "regenerate with scripts/regenerate_storage_version_fixture.py "
            "under a newer duckdb build"
        )

    with pytest.raises(StorageVersionMismatchError) as error_info:
        list_versions(candidates[0], "any-notebook")

    message = str(error_info.value).lower()
    assert "duckdb" in message
    assert "update" in message or "upgrade" in message


def test_ut_r_014_notebook_id_disambiguates_duplicate_names(
    synthetic_ui_db: Path,
) -> None:
    """UT-R-014: notebook_id resolves a duplicate-name notebook unambiguously.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.

    Returns
    -------
    None
        The test asserts that passing ``notebook_id`` for a duplicated name
        selects exactly the requested notebook instead of raising
        ``AmbiguousNotebookError``.

    Notes
    -----
    Traceability: design doc 4.1 section, 7 section.
    """
    notebooks = list_notebooks(synthetic_ui_db)
    duplicate_b = next(
        notebook
        for notebook in notebooks
        if notebook.name == "duplicate" and notebook.updated_at.hour == 3
    )

    notebook = load_notebook(
        synthetic_ui_db,
        "duplicate",
        notebook_id=duplicate_b.notebook_id,
    )

    assert notebook.name == "duplicate"
    assert [cell.sql for cell in notebook.cells] == ["SELECT 'b'"]


def test_ut_r_015_missing_ui_db_reports_clear_not_found_error(
    tmp_path: Path,
) -> None:
    """UT-R-015: a missing ui.db file reports a clear not-found error.

    Parameters
    ----------
    tmp_path
        Temporary directory used to build a non-existing ``ui.db`` path.

    Returns
    -------
    None
        The test asserts that the error names the missing path and does not
        misleadingly suggest that the UI might be running.

    Notes
    -----
    Traceability: design doc 4.1 section, 7 section.
    """
    missing_ui_db = tmp_path / "does-not-exist" / "ui.db"

    with pytest.raises(UiDbAccessError) as error_info:
        load_notebook(missing_ui_db, "any-notebook")

    message = str(error_info.value)
    assert str(missing_ui_db) in message
    assert "not found" in message.lower()
    assert "running" not in message.lower()


# Real-browser-derived fixture: tests/fixtures/ui_db/ui.db was regenerated
# from an actual DuckDB UI browser session (scripts/regenerate_ui_db_fixtures.py
# browser mode). Diffing it against the fallback build revealed that
# notebooks.name holds an internal slug (e.g. "notebook_OR_g9u20SBN9"), while
# the name shown to users in the UI lives in notebook_versions.title (e.g.
# "Untitled Notebook", read from the latest version where expires IS NULL).
# UT-R-016..019 pin the corrected resolution semantics against that fixture.
_REAL_UI_DB_FIXTURE = Path(__file__).parent / "fixtures" / "ui_db" / "ui.db"
_REAL_NOTEBOOK_A_ID = "902baeaf-241e-437e-9564-ec03c316b3f0"
_REAL_NOTEBOOK_A_SLUG = "notebook_OR_g9u20SBN9"
_REAL_NOTEBOOK_B_SLUG = "notebook_JKS7o1wU06Fs"
_REAL_DISPLAY_TITLE = "Untitled Notebook"


def _real_ui_db_fixture() -> Path:
    """Return the real browser-derived ui.db fixture, skipping if absent.

    Returns
    -------
    pathlib.Path
        Path to ``tests/fixtures/ui_db/ui.db``.
    """
    if not _REAL_UI_DB_FIXTURE.exists():
        pytest.skip("real ui.db fixture not present")
    return _REAL_UI_DB_FIXTURE


def test_ut_r_016_list_notebooks_reports_display_title_not_slug() -> None:
    """UT-R-016: list_notebooks reports the display title, not the slug.

    Returns
    -------
    None
        The test asserts that ``list_notebooks`` returns the notebook
        display name (the latest version's ``title``) for both notebooks in
        the real fixture, rather than the internal ``notebooks.name`` slug.

    Notes
    -----
    Traceability: design doc 4.1 section, 6.3#9 (real-browser-fixture
    finding). Both notebooks in the real fixture happen to share the same
    display title ("Untitled Notebook"), which is itself the same-name
    collision case covered by UT-R-007/UT-R-014.
    """
    ui_db_path = _real_ui_db_fixture()

    notebooks = list_notebooks(ui_db_path)

    names = [notebook.name for notebook in notebooks]
    assert names == [_REAL_DISPLAY_TITLE, _REAL_DISPLAY_TITLE]
    assert _REAL_NOTEBOOK_A_SLUG not in names
    assert _REAL_NOTEBOOK_B_SLUG not in names


def test_ut_r_017_load_by_display_title_is_ambiguous_for_real_fixture() -> None:
    """UT-R-017: loading by the shared display title raises ambiguity.

    Returns
    -------
    None
        The test asserts that resolving by the display title
        ``"Untitled Notebook"`` raises ``AmbiguousNotebookError`` because
        both real-fixture notebooks share that title, and that the error
        points at ``--notebook-id``.

    Notes
    -----
    Traceability: design doc 4.1 section, 7 section.
    """
    ui_db_path = _real_ui_db_fixture()

    with pytest.raises(AmbiguousNotebookError) as error_info:
        load_notebook(ui_db_path, _REAL_DISPLAY_TITLE)

    message = str(error_info.value)
    assert "--notebook-id" in message


def test_ut_r_018_load_by_notebook_id_reads_latest_three_cell_version() -> None:
    """UT-R-018: notebook_id resolves the real fixture's latest version.

    Returns
    -------
    None
        The test asserts that loading by ``notebook_id`` succeeds despite the
        display-title collision, and that the latest version (``expires IS
        NULL``) is returned with its three cells, including the empty
        (``query IS NULL``) trailing cell.

    Notes
    -----
    Traceability: design doc 4.1 section, 6.3#9 (real-browser-fixture
    finding), 7 section.
    """
    ui_db_path = _real_ui_db_fixture()

    notebook = load_notebook(
        ui_db_path,
        _REAL_DISPLAY_TITLE,
        notebook_id=_REAL_NOTEBOOK_A_ID,
    )

    assert notebook.name == _REAL_DISPLAY_TITLE
    assert [cell.sql for cell in notebook.cells] == [
        "select 1 as one, 2 as two;",
        "select current_database() as db_name;",
        "",
    ]


def test_ut_r_019_load_by_internal_slug_falls_back_when_title_ambiguous() -> None:
    """UT-R-019: an internal slug still resolves via the fallback match.

    Returns
    -------
    None
        The test asserts that passing the internal ``notebooks.name`` slug
        (as a user might if they copied it from an older tool version or
        from the raw database) as the notebook name still resolves
        unambiguously, because slug matching is only consulted when title
        matching finds zero candidates.

    Notes
    -----
    Traceability: design doc 4.1 section, 6.3#9 (real-browser-fixture
    finding).
    """
    ui_db_path = _real_ui_db_fixture()

    notebook = load_notebook(ui_db_path, _REAL_NOTEBOOK_B_SLUG)

    assert notebook.name == _REAL_DISPLAY_TITLE


def _build_raw_ui_db(ui_db_path: Path) -> duckdb.DuckDBPyConnection:
    """Create the four-table ``ui.db`` schema and return an open connection.

    Parameters
    ----------
    ui_db_path
        Destination database path.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Open connection to the newly created database, ready for the
        caller to insert ``notebooks`` and ``notebook_versions`` rows.

    Notes
    -----
    Mirrors the real ``ui.db`` DDL from design doc section 6.3#9, without
    the foreign-key constraints DuckDB UI itself does not declare.
    """
    connection = duckdb.connect(str(ui_db_path))
    connection.execute(
        "CREATE TABLE notebooks("
        "id UUID NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, "
        "created TIMESTAMP NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE notebook_versions("
        "notebook_id UUID NOT NULL, version INTEGER NOT NULL, "
        "title VARCHAR NOT NULL, json VARCHAR NOT NULL, "
        "created TIMESTAMP NOT NULL, expires TIMESTAMP, "
        "PRIMARY KEY (notebook_id, version))"
    )
    connection.execute("CREATE TABLE current_notebook_id(id UUID NOT NULL)")
    connection.execute("CREATE TABLE has_onboarded AS SELECT false AS has_onboarded")
    return connection


_MINIMAL_STORED_NOTEBOOK_JSON = (
    '{"notebookSerializationFormat": 3, "cells": [], "viewMode": {}, "version": 1}'
)


def test_ut_r_020_duplicate_expires_null_rows_do_not_duplicate_or_ambiguate(
    tmp_path: Path,
) -> None:
    """UT-R-020: duplicate ``expires IS NULL`` rows do not duplicate or ambiguate.

    Parameters
    ----------
    tmp_path
        Temporary directory used to build a raw ``ui.db`` fixture.

    Returns
    -------
    None
        The test asserts that ``list_notebooks`` returns exactly one entry
        for a notebook with two ``expires IS NULL`` version rows, that
        ``load_notebook`` resolves it without raising
        ``AmbiguousNotebookError``, and that the reported ``updated_at`` is
        deterministically the newest such row's ``created`` timestamp.

    Notes
    -----
    Traceability: design doc 4.1 section, 6.3#9. Real DuckDB UI keeps
    exactly one ``expires IS NULL`` row per notebook, but the default read
    path snapshots a live database, so a defensive newest-row pick is
    warranted should two such rows ever coexist transiently.
    """
    ui_db_path = tmp_path / "ui.db"
    notebook_id = "11111111-1111-1111-1111-111111111111"
    connection = _build_raw_ui_db(ui_db_path)
    try:
        connection.execute(
            "INSERT INTO notebooks VALUES "
            "(CAST(? AS UUID), ?, TIMESTAMP '2026-07-01 00:00:00')",
            [notebook_id, "notebook_dupnull0001"],
        )
        connection.execute(
            "INSERT INTO notebook_versions VALUES "
            "(CAST(? AS UUID), 1, ?, ?, TIMESTAMP '2026-07-01 00:00:00', NULL)",
            [notebook_id, "Dup Null Title", _MINIMAL_STORED_NOTEBOOK_JSON],
        )
        connection.execute(
            "INSERT INTO notebook_versions VALUES "
            "(CAST(? AS UUID), 2, ?, ?, TIMESTAMP '2026-07-02 00:00:00', NULL)",
            [notebook_id, "Dup Null Title", _MINIMAL_STORED_NOTEBOOK_JSON],
        )
    finally:
        connection.close()

    notebooks = list_notebooks(ui_db_path)

    matching = [notebook for notebook in notebooks if notebook.name == "Dup Null Title"]
    assert len(matching) == 1
    assert matching[0].updated_at.isoformat().startswith("2026-07-02")

    notebook = load_notebook(ui_db_path, "Dup Null Title")
    assert notebook.name == "Dup Null Title"


def test_ut_r_021_notebook_without_versions_is_excluded_not_crash(
    tmp_path: Path,
) -> None:
    """UT-R-021: a notebook with zero version rows is excluded, not a crash.

    Parameters
    ----------
    tmp_path
        Temporary directory used to build a raw ``ui.db`` fixture.

    Returns
    -------
    None
        The test asserts that ``list_notebooks`` excludes a notebook that
        has no ``notebook_versions`` rows at all, rather than raising a
        ``pydantic.ValidationError`` from a null display name or timestamp,
        and that a normal notebook alongside it still resolves correctly.

    Notes
    -----
    Traceability: design doc 4.1 section. Restores the pre-existing
    inner-join exclusion semantics for versionless notebooks.
    """
    ui_db_path = tmp_path / "ui.db"
    real_notebook_id = "22222222-2222-2222-2222-222222222222"
    versionless_notebook_id = "33333333-3333-3333-3333-333333333333"
    connection = _build_raw_ui_db(ui_db_path)
    try:
        connection.execute(
            "INSERT INTO notebooks VALUES "
            "(CAST(? AS UUID), ?, TIMESTAMP '2026-07-01 00:00:00')",
            [real_notebook_id, "notebook_real0001"],
        )
        connection.execute(
            "INSERT INTO notebooks VALUES "
            "(CAST(? AS UUID), ?, TIMESTAMP '2026-07-01 00:00:00')",
            [versionless_notebook_id, "notebook_noversions01"],
        )
        connection.execute(
            "INSERT INTO notebook_versions VALUES "
            "(CAST(? AS UUID), 1, ?, ?, TIMESTAMP '2026-07-01 00:00:00', NULL)",
            [real_notebook_id, "Real Title", _MINIMAL_STORED_NOTEBOOK_JSON],
        )
    finally:
        connection.close()

    notebooks = list_notebooks(ui_db_path)

    assert len(notebooks) == 1
    assert notebooks[0].name == "Real Title"

    notebook = load_notebook(ui_db_path, "Real Title")
    assert notebook.name == "Real Title"


def test_ut_r_022_cleanup_stale_snapshots_removes_only_old_snapshot_dirs(
    tmp_path: Path,
) -> None:
    """UT-R-022: _cleanup_stale_snapshots removes only stale snapshot dirs.

    Parameters
    ----------
    tmp_path
        Temporary directory used as a fake system temp root.

    Returns
    -------
    None
        The test asserts that a stale snapshot directory is removed while a
        fresh snapshot directory, an unrelated directory, and a stale-named
        regular file are all left untouched.

    Notes
    -----
    Traceability: GitHub issue #38 (stale ui.db snapshot directories
    accumulate after crashes).
    """
    stale_dir = tmp_path / "duckdb-ui-notebook-export-stale"
    stale_dir.mkdir()
    (stale_dir / "ui.db").write_text("stale")
    old_timestamp = time.time() - (25 * 60 * 60)
    os.utime(stale_dir, (old_timestamp, old_timestamp))

    fresh_dir = tmp_path / "duckdb-ui-notebook-export-fresh"
    fresh_dir.mkdir()

    unrelated_dir = tmp_path / "something-else"
    unrelated_dir.mkdir()

    stale_named_file = tmp_path / "duckdb-ui-notebook-export-notadir"
    stale_named_file.write_text("not a directory")
    os.utime(stale_named_file, (old_timestamp, old_timestamp))

    _cleanup_stale_snapshots(temp_root=tmp_path)

    assert not stale_dir.exists()
    assert fresh_dir.exists()
    assert unrelated_dir.exists()
    assert stale_named_file.exists()


def test_ut_r_023_open_ui_db_triggers_cleanup_only_for_snapshot_path(
    synthetic_ui_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-R-023: open_ui_db calls the cleanup only on the snapshot path.

    Parameters
    ----------
    synthetic_ui_db
        Generated DuckDB UI database fixture.
    monkeypatch
        Pytest monkeypatch fixture used to replace the cleanup helper.

    Returns
    -------
    None
        The test asserts that ``_cleanup_stale_snapshots`` is called exactly
        once when ``require_ui_closed`` is false (the default), and is not
        called at all when ``require_ui_closed=True``.

    Notes
    -----
    Traceability: GitHub issue #38 (stale ui.db snapshot directories
    accumulate after crashes).
    """
    calls: list[None] = []
    monkeypatch.setattr(
        "duckdb_ui_notebook_export.reader._cleanup_stale_snapshots",
        lambda **kwargs: calls.append(None),
    )

    connection = open_ui_db(synthetic_ui_db)
    connection.close()

    assert len(calls) == 1

    connection = open_ui_db(synthetic_ui_db, require_ui_closed=True)
    connection.close()

    assert len(calls) == 1


def test_ut_r_024_cleanup_stale_snapshots_nonexistent_root_does_not_raise(
    tmp_path: Path,
) -> None:
    """UT-R-024: _cleanup_stale_snapshots tolerates a missing temp root.

    Parameters
    ----------
    tmp_path
        Temporary directory used to build a non-existing temp root path.

    Returns
    -------
    None
        The test asserts that calling ``_cleanup_stale_snapshots`` against a
        temp root that does not exist on disk does not raise.

    Notes
    -----
    Traceability: GitHub issue #38 (stale ui.db snapshot directories
    accumulate after crashes). The cleanup is best-effort and must never
    surface an ``OSError`` to callers of ``open_ui_db``.
    """
    missing_root = tmp_path / "does-not-exist"

    _cleanup_stale_snapshots(temp_root=missing_root)
