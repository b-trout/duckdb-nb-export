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
    confirm_execution,
    dedupe_output_path,
    main,
    resolve_output_path,
    sanitize_filename,
)
from duckdb_ui_notebook_export.exceptions import ExitCode, OutputPathError
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
    """Build a synthetic ui.db, skipping while blocked by design doc 6.2#1.

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
    except NotImplementedError:
        pytest.skip("blocked by design doc 6.2#1: notebook JSON schema unknown")


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


def test_ut_c_013_main_returns_ok_when_export_completes(
    synthetic_ui_db: Path,
    fresh_duckdb: Path,
    tmp_workdir: Path,
) -> None:
    """UT-C-013: Returns exit code 0 when export completes."""
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

    assert exit_code == ExitCode.OK


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
