"""Unit tests for the command-line interface layer.

Parameters
----------
None
    This module is imported by pytest.

Returns
-------
None
    Importing this module registers CLI tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.
"""

from pathlib import Path

import pytest

from duckdb_ui_notebook_export.cli import (
    _cell_error_exit_required,
    confirm_execution,
    dedupe_output_path,
    main,
    resolve_output_path,
    sanitize_filename,
)
from duckdb_ui_notebook_export.exceptions import ExitCode, OutputPathError
from duckdb_ui_notebook_export.executor import CellResult, CellStatus, ExecutionReport
from duckdb_ui_notebook_export.models import Cell


def _sql_cell(sql: str) -> dict[str, str]:
    """Build a synthetic SQL cell specification.

    Parameters
    ----------
    sql
        SQL text to encode in the synthetic cell.

    Returns
    -------
    dict[str, str]
        Cell specification consumed by the synthetic ui.db builder.
    """
    return {"cell_type": "sql", "sql": sql}


def _notebook_spec(name: str, *cells: dict[str, str]) -> dict[str, object]:
    """Build a synthetic notebook specification.

    Parameters
    ----------
    name
        Notebook name to encode.
    *cells
        Cell specifications to place in the notebook version.

    Returns
    -------
    dict[str, object]
        Notebook specification consumed by the synthetic ui.db builder.
    """
    safe_id = name.lower().replace(" ", "-").replace("/", "-").replace(":", "-")
    return {
        "name": name,
        "notebook_id": f"nb-{safe_id}",
        "versions": [
            {
                "version_id": f"{safe_id}-v1",
                "created_at": "2026-07-05T00:00:00Z",
                "cells": list(cells),
            }
        ],
    }


@pytest.fixture
def synthetic_ui_db(tmp_path: Path) -> Path:
    """Build a synthetic ui.db, skipping with the builder's unsupported reason.

    Returns
    -------
    pathlib.Path
        Path to a generated ui.db file.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    try:
        return build_ui_db(
            [
                _notebook_spec(
                    "Notebook",
                    _sql_cell("SELECT 1"),
                    _sql_cell("SELECT * FROM missing_table"),
                    _sql_cell("SELECT 2"),
                )
            ],
            tmp_path,
        )
    except NotImplementedError as error:
        pytest.skip(str(error))


def test_ut_c_001_default_output_path_allowed_under_current_directory(
    tmp_workdir: Path,
) -> None:
    """UT-C-001: Resolves default output paths under the current directory."""
    output = resolve_output_path(None, "Sales Report", None)

    assert output == tmp_workdir / "Sales_Report.html"
    assert output.is_absolute()


def test_ut_c_002_output_path_outside_base_rejected(tmp_workdir: Path) -> None:
    """UT-C-002: Rejects normalized output paths outside the allowed base."""
    outside = tmp_workdir.parent / "outside.html"

    with pytest.raises(OutputPathError):
        resolve_output_path(str(outside), "Notebook", None)


def test_ut_c_003_dotdot_paths_judged_after_normalization(
    tmp_workdir: Path,
) -> None:
    """UT-C-003: Judges ``..`` paths by normalized containment."""
    allowed_dir = tmp_workdir / "allowed"
    allowed_dir.mkdir()

    allowed = resolve_output_path("allowed/../inside.html", "Notebook", None)

    assert allowed == tmp_workdir / "inside.html"
    with pytest.raises(OutputPathError):
        resolve_output_path("../outside.html", "Notebook", None)


def test_ut_c_004_symlink_escape_rejected(tmp_workdir: Path) -> None:
    """UT-C-004: Rejects output paths escaping through a symlink."""
    outside = tmp_workdir.parent / "outside"
    outside.mkdir()
    (tmp_workdir / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OutputPathError):
        resolve_output_path("link/escape.html", "Notebook", None)


def test_ut_c_005_existing_output_path_gets_numeric_suffix(
    tmp_workdir: Path,
) -> None:
    """UT-C-005: Adds ``-1`` when the requested output file exists."""
    desired = tmp_workdir / "report.html"
    desired.write_text("existing", encoding="utf-8")

    assert dedupe_output_path(desired) == tmp_workdir / "report-1.html"


def test_ut_c_006_existing_suffixes_increment_until_free(tmp_workdir: Path) -> None:
    """UT-C-006: Increments numeric suffixes until a free path is found."""
    for name in ("report.html", "report-1.html", "report-2.html"):
        (tmp_workdir / name).write_text("existing", encoding="utf-8")

    assert dedupe_output_path(tmp_workdir / "report.html") == (
        tmp_workdir / "report-3.html"
    )


def test_ut_c_007_notebook_name_sanitized_for_filename() -> None:
    """UT-C-007: Replaces invalid filename characters and whitespace."""
    assert sanitize_filename("Sales / Cost: Q1\tDraft") == "Sales_Cost_Q1_Draft"


def test_ut_c_008_sanitized_default_name_emits_warning(
    tmp_workdir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-008: Warns when a notebook name is changed for output."""
    output = resolve_output_path(None, "Sales / Cost", None)

    captured = capsys.readouterr()
    assert output == tmp_workdir / "Sales_Cost.html"
    assert "Sales / Cost" in captured.err
    assert "Sales_Cost" in captured.err


def test_ut_c_009_output_dir_becomes_allowed_base(
    tmp_workdir: Path,
    tmp_path: Path,
) -> None:
    """UT-C-009: Uses ``--output-dir`` as the default and allowed base."""
    output_dir = tmp_workdir / "exports"
    output_dir.mkdir()
    outside = tmp_path / "outside.html"

    output = resolve_output_path(None, "Notebook", str(output_dir))

    assert output == output_dir / "Notebook.html"
    with pytest.raises(OutputPathError):
        resolve_output_path(str(outside), "Notebook", str(output_dir))


def test_ut_c_010_non_tty_confirmation_declines_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-010: Declines in non-TTY mode without prompting."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    confirmed = confirm_execution([Cell(sql="SELECT 1")], assume_yes=False)

    captured = capsys.readouterr()
    assert confirmed is False
    assert "SELECT 1" not in captured.out


def test_ut_c_011_assume_yes_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-011: Confirms immediately when ``--yes`` is used."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    confirmed = confirm_execution([Cell(sql="SELECT 1")], assume_yes=True)

    captured = capsys.readouterr()
    assert confirmed is True
    assert captured.out == ""


def test_ut_c_012_prompt_lists_all_cell_sql(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-012: Shows every cell SQL body in the confirmation prompt."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    confirmed = confirm_execution(
        [Cell(sql="SELECT 1 AS first"), Cell(sql="SELECT 2 AS second")],
        assume_yes=False,
    )

    captured = capsys.readouterr()
    assert confirmed is True
    assert "SELECT 1 AS first" in captured.out
    assert "SELECT 2 AS second" in captured.out


def test_ut_c_013_main_returns_cell_error_when_a_cell_fails_by_default(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-013: Returns exit code 2 by default when any cell result fails.

    Notes
    -----
    The ``synthetic_ui_db`` fixture notebook includes a failing
    ``SELECT * FROM missing_table`` cell. Since issue #33, the CLI exits
    with ``ExitCode.CELL_ERROR`` by default whenever any cell result is not
    ``CellStatus.OK``, without requiring ``--stop-on-error``.
    """
    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.CELL_ERROR


def test_ut_c_033_no_fail_on_cell_error_restores_exit_zero(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-033: ``--no-fail-on-cell-error`` restores the previous exit 0.

    Notes
    -----
    The ``synthetic_ui_db`` fixture notebook includes a plain ``ERROR``
    cell. ``--no-fail-on-cell-error`` restores the pre-#33 behavior: exit 0
    on completion despite the cell failure.

    Traceability
    ------------
    Issue #33
    """
    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--no-fail-on-cell-error",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.OK


def test_ut_c_034_no_fail_on_cell_error_still_fails_on_abandoned_report(
    tmp_path: Path,
) -> None:
    """UT-C-034: ``--no-fail-on-cell-error`` still exits 2 when abandoned.

    Notes
    -----
    ``report.abandoned`` and ``CellStatus.TIMEOUT`` results must still map
    to ``ExitCode.CELL_ERROR`` even when ``--no-fail-on-cell-error`` is set;
    only plain per-cell failures are forgiven by that flag.

    Traceability
    ------------
    Issue #33
    """
    del tmp_path
    report = ExecutionReport(
        cell_results=[
            CellResult(
                status=CellStatus.OK,
                columns=[],
                rows=[],
                truncated=False,
                affected_rows=None,
                error_message=None,
            ),
        ],
        warnings=[],
        used_memory_fallback=False,
        abandoned=True,
    )

    assert (
        _cell_error_exit_required(
            report, stop_on_error=False, no_fail_on_cell_error=True
        )
        is True
    )


def test_ut_c_014_main_returns_notebook_not_found_for_missing_name(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-014: Returns exit code 1 when the notebook is not found."""
    exit_code = main(
        [
            "Missing Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.NOTEBOOK_NOT_FOUND


def test_ut_c_015_main_returns_cell_error_with_stop_on_error(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-015: Returns exit code 2 when ``--stop-on-error`` interrupts."""
    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--stop-on-error",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.CELL_ERROR


def test_ut_c_016_main_returns_cell_error_and_partial_html_on_hard_timeout(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-016: Returns exit code 2 and writes partial HTML on hard timeout.

    Notes
    -----
    The fixture cells are lightweight and do not time out at ``--cell-timeout
    0.1``. A hard timeout after an interrupt fails to complete within the grace
    period cannot be reproduced reliably with real DuckDB, matching the
    limitation documented by UT-X-013.
    """
    pytest.skip("cannot reliably reproduce un-interruptible query with real DuckDB")

    output = tmp_workdir / "partial.html"

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(output),
            "--cell-timeout",
            "0.1",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.CELL_ERROR
    assert output.exists()


def test_ut_c_028_main_returns_execution_failed_for_missing_target_db(
    synthetic_ui_db: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-028: A mistyped ``--db`` maps to exit code 6, no file created.

    Notes
    -----
    ``TargetDatabaseError`` maps to ``ExitCode.EXECUTION_FAILED`` with a
    ``target_database_missing`` log event, distinct from ui.db access
    failures.

    Traceability
    ------------
    Issue #30, #34
    """
    missing_db = tmp_workdir / "typo.duckdb"

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(missing_db),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.EXECUTION_FAILED
    assert not missing_db.exists()


def test_ut_c_032_main_returns_execution_failed_when_html_writing_fails(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-C-032: An HTML write failure maps to exit code 6, not 4.

    Traceability
    ------------
    Issue #34
    """
    import duckdb_ui_notebook_export.cli as cli_module

    def _raise_os_error(path: Path, html: str) -> None:
        del path, html
        raise OSError("disk full")

    monkeypatch.setattr(cli_module, "_write_html", _raise_os_error)

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.EXECUTION_FAILED


def test_ut_c_029_read_only_and_allow_writes_are_mutually_exclusive(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-029: ``--read-only`` and ``--allow-writes`` cannot both be set.

    Traceability
    ------------
    Issue #31
    """
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "Notebook",
                "--ui-db",
                str(synthetic_ui_db),
                "--db",
                str(fresh_duckdb),
                "--output",
                str(tmp_workdir / "out.html"),
                "--read-only",
                "--allow-writes",
                "--yes",
            ]
        )

    assert exc_info.value.code == 2


def test_ut_c_030_read_only_flag_is_passed_to_executor(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-030: ``--read-only`` completes the export normally.

    Notes
    -----
    The ``synthetic_ui_db`` fixture notebook includes a failing cell, so
    since issue #33 the CLI exits with ``ExitCode.CELL_ERROR`` by default;
    this test asserts the export still completes (rather than crashing)
    under ``--read-only``.

    Traceability
    ------------
    Issue #31, #33
    """
    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--read-only",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.CELL_ERROR


def test_ut_c_031_allow_writes_abort_completes_export_without_partial_commit(
    tmp_workdir: Path,
) -> None:
    """UT-C-031: ``--allow-writes`` never partial-commits after an abort.

    Notes
    -----
    Before the issue #32 fix, an error that aborted the transaction still
    hit a final ``COMMIT`` on an aborted transaction; here it is asserted
    end to end through the CLI: the export completes (writes an HTML file)
    and the target database file shows no committed table afterward. Since
    issue #33, the CLI exits with ``ExitCode.CELL_ERROR`` by default because
    the notebook includes a failing cell.

    Traceability
    ------------
    Issue #32, #33
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    try:
        ui_db = build_ui_db(
            [
                _notebook_spec(
                    "AbortWrites",
                    _sql_cell("CREATE TABLE abort_source (id INTEGER PRIMARY KEY)"),
                    _sql_cell("INSERT INTO abort_source VALUES (1)"),
                    _sql_cell("INSERT INTO abort_source VALUES (1)"),
                    _sql_cell("SELECT 2 AS skipped_after_abort"),
                )
            ],
            tmp_workdir,
        )
    except NotImplementedError as error:
        pytest.skip(str(error))

    target_db = tmp_workdir / "target.duckdb"
    import duckdb

    duckdb.connect(str(target_db)).close()

    exit_code = main(
        [
            "AbortWrites",
            "--ui-db",
            str(ui_db),
            "--db",
            str(target_db),
            "--output",
            str(tmp_workdir / "out.html"),
            "--allow-writes",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.CELL_ERROR
    assert (tmp_workdir / "out.html").exists()

    with duckdb.connect(str(target_db)) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = 'abort_source'
            """,
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_ut_c_027_abandoned_report_requires_cell_error_exit(tmp_path: Path) -> None:
    """UT-C-027: ``report.abandoned`` True maps to ``ExitCode.CELL_ERROR``.

    Notes
    -----
    Replaces the old brittle substring check on error messages
    (``_is_abandoned_result_message``) with a structured
    ``ExecutionReport.abandoned`` flag.

    Traceability
    ------------
    Issue #28
    """
    del tmp_path
    report = ExecutionReport(
        cell_results=[
            CellResult(
                status=CellStatus.OK,
                columns=[],
                rows=[],
                truncated=False,
                affected_rows=None,
                error_message=None,
            ),
        ],
        warnings=[],
        used_memory_fallback=False,
        abandoned=True,
    )

    assert (
        _cell_error_exit_required(
            report, stop_on_error=False, no_fail_on_cell_error=False
        )
        is True
    )


def test_ut_c_022_default_output_path_uses_notebook_name(
    tmp_workdir: Path,
) -> None:
    """UT-C-022: Missing ``-o`` resolves to sanitized notebook-name HTML.

    Notes
    -----
    Traceability: design doc chapter 7.
    """
    output = resolve_output_path(None, "Sales / Cost: Q1", None)

    assert output == tmp_workdir / "Sales_Cost_Q1.html"
    assert output.is_absolute()


def test_ut_c_017_main_returns_output_path_rejected(tmp_workdir: Path) -> None:
    """UT-C-017: Returns exit code 3 for rejected output paths."""
    exit_code = main(
        [
            "Notebook",
            "--output",
            str(tmp_workdir.parent / "outside.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.OUTPUT_PATH_REJECTED


def test_ut_c_018_main_returns_ui_db_access_failed_for_unreadable_ui_db(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-018: Returns exit code 4 when ui.db cannot be accessed."""
    synthetic_ui_db.write_bytes(b"not a duckdb database")

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.UI_DB_ACCESS_FAILED


def test_ut_c_019_main_returns_confirmation_declined_when_user_refuses(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-C-019: Returns exit code 5 when the user declines confirmation."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
        ]
    )

    assert exit_code == ExitCode.CONFIRMATION_DECLINED


def test_ut_c_020_main_list_prints_notebooks(
    synthetic_ui_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-020: Prints notebook names, IDs, and update times for ``--list``."""
    exit_code = main(["--ui-db", str(synthetic_ui_db), "--list"])

    captured = capsys.readouterr()
    assert exit_code == ExitCode.OK
    assert "Notebook" in captured.out
    assert "ID" in captured.out
    assert "Updated" in captured.out


def test_ut_c_021_main_list_versions_prints_versions(
    synthetic_ui_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-021: Prints version IDs and creation times for ``--list-versions``."""
    exit_code = main(["Notebook", "--ui-db", str(synthetic_ui_db), "--list-versions"])

    captured = capsys.readouterr()
    assert exit_code == ExitCode.OK
    assert "Version" in captured.out
    assert "Created" in captured.out


@pytest.fixture
def duplicate_name_ui_db(tmp_path: Path) -> Path:
    """Build a synthetic ui.db with two notebooks sharing the same name.

    Parameters
    ----------
    tmp_path
        Temporary directory where the generated database should be written.

    Returns
    -------
    pathlib.Path
        Path to a generated ui.db file containing two "Duplicate" notebooks.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    try:
        return build_ui_db(
            [
                {
                    "name": "Duplicate",
                    "notebook_id": "nb-duplicate-cli-a",
                    "updated_at": "2026-07-05T02:00:00Z",
                    "versions": [
                        {
                            "version_id": "1",
                            "created_at": "2026-07-05T02:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 'a'"}],
                        }
                    ],
                },
                {
                    "name": "Duplicate",
                    "notebook_id": "nb-duplicate-cli-b",
                    "updated_at": "2026-07-05T03:00:00Z",
                    "versions": [
                        {
                            "version_id": "1",
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


def test_ut_c_023_main_notebook_id_exports_duplicate_name_without_positional(
    duplicate_name_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-023: --notebook-id exports a duplicate-name notebook, no positional.

    Notes
    -----
    Traceability: design doc 4.1 section, 7 section.
    """
    from duckdb_ui_notebook_export.reader import list_notebooks

    notebooks = list_notebooks(duplicate_name_ui_db)
    target = next(
        notebook
        for notebook in notebooks
        if notebook.name == "Duplicate" and notebook.updated_at.hour == 3
    )

    exit_code = main(
        [
            "--ui-db",
            str(duplicate_name_ui_db),
            "--notebook-id",
            target.notebook_id,
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--yes",
        ]
    )

    assert exit_code == ExitCode.OK
    assert (tmp_workdir / "out.html").exists()


@pytest.mark.parametrize("value", ["0", "-5"])
def test_ut_c_035_max_rows_rejects_non_positive_values(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-035: ``--max-rows`` rejects zero and negative values.

    Traceability
    ------------
    Issue #37
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["Notebook", "--max-rows", value, "--yes"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--max-rows" in captured.err


@pytest.mark.parametrize("value", ["0", "-1"])
def test_ut_c_036_cell_timeout_rejects_non_positive_values(
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-036: ``--cell-timeout`` rejects zero and negative values.

    Traceability
    ------------
    Issue #37
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["Notebook", "--cell-timeout", value, "--yes"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--cell-timeout" in captured.err


def test_ut_c_037_interrupt_grace_rejects_non_positive_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-037: ``--interrupt-grace`` rejects zero (and non-positive values).

    Traceability
    ------------
    Issue #37
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["Notebook", "--interrupt-grace", "0", "--yes"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--interrupt-grace" in captured.err


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-rows", "1"),
        ("--max-rows", "500"),
        ("--cell-timeout", "0.1"),
        ("--cell-timeout", "300"),
        ("--interrupt-grace", "0.1"),
        ("--interrupt-grace", "30"),
    ],
)
def test_ut_c_038_numeric_validators_accept_valid_values(
    flag: str,
    value: str,
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-038: Valid ``--max-rows``/``--cell-timeout``/``--interrupt-grace``
    values are accepted and the export still completes.

    Traceability
    ------------
    Issue #37
    """
    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            flag,
            value,
            "--no-fail-on-cell-error",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.OK


def test_ut_c_039_interrupt_grace_reaches_execute_notebook(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-C-039: ``--interrupt-grace`` is forwarded to ``execute_notebook``.

    Traceability
    ------------
    Issue #37
    """
    import duckdb_ui_notebook_export.cli as cli_module

    captured_kwargs: dict[str, object] = {}
    original_execute_notebook = cli_module.execute_notebook

    def _capture_execute_notebook(notebook, db, **kwargs):
        captured_kwargs.update(kwargs)
        return original_execute_notebook(notebook, db, **kwargs)

    monkeypatch.setattr(cli_module, "execute_notebook", _capture_execute_notebook)

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(tmp_workdir / "out.html"),
            "--interrupt-grace",
            "12.5",
            "--no-fail-on-cell-error",
            "--yes",
        ]
    )

    assert exit_code == ExitCode.OK
    assert captured_kwargs["interrupt_grace"] == 12.5


def test_ut_c_040_main_prints_final_output_path_on_success(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-040: A successful export prints exactly the final path to stdout.

    Traceability
    ------------
    Issue #35
    """
    output = tmp_workdir / "out.html"

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(output),
            "--no-fail-on-cell-error",
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == ExitCode.OK
    assert captured.out == f"{output}\n"


def test_ut_c_041_main_warns_and_prints_deduped_path_when_target_exists(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-C-041: Prints the deduped path and warns on stderr before writing.

    Traceability
    ------------
    Issue #35
    """
    output = tmp_workdir / "out.html"
    output.write_text("existing", encoding="utf-8")
    deduped = tmp_workdir / "out-1.html"

    exit_code = main(
        [
            "Notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--db",
            str(fresh_duckdb),
            "--output",
            str(output),
            "--no-fail-on-cell-error",
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == ExitCode.OK
    assert captured.out == f"{deduped}\n"
    assert "output_path_deduplicated" in captured.err
    assert str(output) in captured.err
    assert str(deduped) in captured.err
    assert deduped.exists()
