"""Command-line interface for DuckDB UI notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes CLI helper functions and ``main``.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
This module owns CLI argument parsing, output path validation, confirmation,
and orchestration across the reader, executor, and renderer layers.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import structlog

from duckdb_ui_notebook_export import __version__, _logging
from duckdb_ui_notebook_export.exceptions import (
    AmbiguousNotebookError,
    ExitCode,
    ExporterError,
    NotebookNotFoundError,
    OutputPathError,
    StorageVersionMismatchError,
    UiDbAccessError,
)
from duckdb_ui_notebook_export.executor import (
    CellStatus,
    ExecutionReport,
    execute_notebook,
    resolve_target_db,
)
from duckdb_ui_notebook_export.models import Cell, NotebookInfo, VersionInfo
from duckdb_ui_notebook_export.reader import (
    DEFAULT_UI_DB_PATH,
    list_notebooks,
    list_versions,
    load_notebook,
)
from duckdb_ui_notebook_export.renderer import ExportMetadata, render_html

LOGGER = structlog.get_logger()
_UNSAFE_FILENAME_PATTERN = re.compile(r"[\s/\\:]+")


def sanitize_filename(name: str) -> str:
    """Sanitize a notebook name for use as an HTML filename.

    Parameters
    ----------
    name
        Notebook name or filename stem to sanitize.

    Returns
    -------
    str
        Sanitized filename component with invalid characters and whitespace
        replaced by underscores.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.
    """
    return _UNSAFE_FILENAME_PATTERN.sub("_", name).strip("_")


def dedupe_output_path(path: Path) -> Path:
    """Return a non-existing output path by appending a numeric suffix.

    Parameters
    ----------
    path
        Desired output path.

    Returns
    -------
    pathlib.Path
        ``path`` when it does not exist, otherwise ``<name>-N.html``.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.
    """
    if not path.exists():
        return path

    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def resolve_output_path(
    output: str | None,
    notebook_name: str,
    output_dir: str | None,
) -> Path:
    """Resolve and validate the final HTML output path.

    Parameters
    ----------
    output
        Explicit output path from ``-o`` or ``--output``.
    notebook_name
        Notebook name used for the default filename.
    output_dir
        Optional allowed base directory and default output directory.

    Returns
    -------
    pathlib.Path
        Normalized output path under the allowed base directory.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.OutputPathError
        Raised when the normalized output path escapes the allowed base.

    Notes
    -----
    Validation must use symlink-resolved absolute paths, not string matching.
    """
    _logging.configure_logging()
    base = Path(output_dir) if output_dir is not None else Path.cwd()
    resolved_base = base.expanduser().resolve()

    if output is None:
        sanitized_name = sanitize_filename(notebook_name)
        if sanitized_name != notebook_name:
            _direct_stderr_logger().warning(
                "notebook_name_sanitized_for_output",
                original_name=notebook_name,
                sanitized_name=sanitized_name,
            )
        raw_path = resolved_base / f"{sanitized_name}.html"
    else:
        output_path = Path(output).expanduser()
        raw_path = (
            output_path if output_path.is_absolute() else resolved_base / output_path
        )

    resolved_path = raw_path.resolve()
    if not _is_relative_to(resolved_path, resolved_base):
        raise OutputPathError(
            f"Output path {resolved_path} is outside allowed base {resolved_base}."
        )
    return resolved_path


def confirm_execution(cells: list[Cell], *, assume_yes: bool) -> bool:
    """Confirm that notebook cells should be executed.

    Parameters
    ----------
    cells
        Cells whose SQL should be shown to the user before execution.
    assume_yes
        Whether confirmation should be skipped.

    Returns
    -------
    bool
        True when execution is confirmed, False when it is declined.

    Raises
    ------
    EOFError
        Raised by ``input`` when an interactive prompt cannot read a response.

    Notes
    -----
    In a non-TTY environment with ``assume_yes=False``, this function must
    return False instead of raising ``UiDbAccessError`` or ``SystemExit``. The
    caller maps that result to ``ExitCode.CONFIRMATION_DECLINED``.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False

    sys.stdout.write("The following SQL cells will be executed:\n")
    for index, cell in enumerate(cells, start=1):
        sys.stdout.write(f"\n[{index}]\n{cell.sql}\n")

    response = input("Continue with notebook execution? [y/N] ")
    return response.strip().lower() in {"y", "yes"}


def _is_relative_to(path: Path, base: Path) -> bool:
    """Return whether ``path`` is contained by ``base``.

    Parameters
    ----------
    path
        Candidate resolved path.
    base
        Resolved allowed base directory.

    Returns
    -------
    bool
        True when ``path`` is equal to or below ``base``.
    """
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _direct_stderr_logger() -> structlog.BoundLogger:
    """Return a structlog logger that writes directly to current stderr.

    Returns
    -------
    structlog.BoundLogger
        Logger bound to the active ``sys.stderr`` stream.
    """
    return structlog.wrap_logger(structlog.PrintLogger(sys.stderr))


def _write_notebook_table(notebooks: Iterable[NotebookInfo]) -> None:
    """Write notebook metadata as a simple table to stdout.

    Parameters
    ----------
    notebooks
        Notebook metadata records to display.

    Returns
    -------
    None
        The table is written to stdout.
    """
    sys.stdout.write("Notebook\tID\tUpdated\n")
    for notebook in notebooks:
        sys.stdout.write(
            f"{notebook.name}\t{notebook.notebook_id}\t{notebook.updated_at}\n"
        )


def _write_version_table(versions: Iterable[VersionInfo]) -> None:
    """Write notebook version metadata as a simple table to stdout.

    Parameters
    ----------
    versions
        Version metadata records to display.

    Returns
    -------
    None
        The table is written to stdout.
    """
    sys.stdout.write("Version\tCreated\n")
    for version in versions:
        sys.stdout.write(f"{version.version_id}\t{version.created_at}\n")


def _utc_now_z() -> str:
    """Return the current UTC timestamp in ISO 8601 ``Z`` form.

    Returns
    -------
    str
        UTC timestamp with a trailing ``Z``.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _cell_error_exit_required(report: ExecutionReport, *, stop_on_error: bool) -> bool:
    """Return whether execution results should map to cell-error exit code.

    Parameters
    ----------
    report
        Notebook execution report.
    stop_on_error
        Whether CLI execution requested early stop on cell failure.

    Returns
    -------
    bool
        True when the CLI should return ``ExitCode.CELL_ERROR``.
    """
    if report.abandoned:
        return True
    if any(result.status is CellStatus.TIMEOUT for result in report.cell_results):
        return True
    if stop_on_error:
        return any(result.status is not CellStatus.OK for result in report.cell_results)
    return False


def _write_html(path: Path, html: str) -> None:
    """Write rendered HTML to disk using UTF-8.

    Parameters
    ----------
    path
        Destination file path.
    html
        Rendered HTML document.

    Returns
    -------
    None
        The file is written to disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the ``duckdb-nb-export`` command-line interface.

    Parameters
    ----------
    argv
        Optional argument vector without the program name. ``None`` uses
        ``sys.argv`` via ``argparse``.

    Returns
    -------
    int
        Process exit code matching ``ExitCode`` values.

    Raises
    ------
    SystemExit
        Raised by ``argparse`` for help text or invalid arguments.
    """
    _logging.configure_logging()
    parser = argparse.ArgumentParser(
        prog="duckdb-nb-export",
        description="Export a DuckDB UI notebook to static HTML.",
    )
    parser.add_argument(
        "notebook_name",
        nargs="?",
        help="Notebook name to export. Optional when --list or --notebook-id is used.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output HTML path. Defaults to '<notebook-name>.html' under the "
        "allowed base directory.",
    )
    parser.add_argument(
        "--output-dir",
        help="Allowed base directory and default output directory. Defaults "
        "to the current directory.",
    )
    parser.add_argument(
        "--db",
        help="Target DuckDB database path for notebook re-execution. "
        "Defaults to a path resolved from notebook metadata, then ':memory:'.",
    )
    parser.add_argument(
        "--ui-db",
        default=str(DEFAULT_UI_DB_PATH),
        help="Path to DuckDB UI ui.db. Defaults to "
        f"'{DEFAULT_UI_DB_PATH}' (<HOME>/.duckdb/extension_data/ui/ui.db).",
    )
    parser.add_argument(
        "--nb-version",
        help="Notebook version identifier to export.",
    )
    parser.add_argument(
        "--notebook-id",
        help="Notebook ID to disambiguate notebooks that share the same "
        "name (see --list). When given, notebook_name may be omitted and "
        "takes priority over notebook_name for resolution.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List notebooks and exit.",
    )
    parser.add_argument(
        "--list-versions",
        action="store_true",
        help="List versions for the selected notebook and exit.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=1000,
        help="Maximum rows to render per cell (default: %(default)s).",
    )
    parser.add_argument(
        "--cell-timeout",
        type=float,
        default=300.0,
        help="Per-cell execution timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop processing after the first cell error.",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Commit notebook changes instead of rolling them back.",
    )
    parser.add_argument(
        "--no-external-access",
        action="store_true",
        help="Disable DuckDB external access during execution.",
    )
    parser.add_argument(
        "--require-ui-closed",
        action="store_true",
        help="Open ui.db directly and require DuckDB UI to be closed.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the execution confirmation prompt.",
    )
    args = parser.parse_args(argv)

    if not args.list and args.notebook_name is None and args.notebook_id is None:
        parser.error(
            "notebook_name is required unless --list or --notebook-id is used."
        )

    try:
        if args.list:
            _write_notebook_table(list_notebooks(Path(args.ui_db)))
            return int(ExitCode.OK)

        if args.list_versions:
            _write_version_table(
                list_versions(
                    Path(args.ui_db),
                    args.notebook_name,
                    notebook_id=args.notebook_id,
                )
            )
            return int(ExitCode.OK)

        if args.notebook_name is not None:
            output_path = resolve_output_path(
                args.output,
                args.notebook_name,
                args.output_dir,
            )
    except OutputPathError as error:
        LOGGER.error("output_path_rejected", error=str(error))
        return int(ExitCode.OUTPUT_PATH_REJECTED)
    except (NotebookNotFoundError, AmbiguousNotebookError) as error:
        LOGGER.error("notebook_not_found", error=str(error))
        return int(ExitCode.NOTEBOOK_NOT_FOUND)
    except (StorageVersionMismatchError, UiDbAccessError) as error:
        LOGGER.error("ui_db_access_failed", error=f"ui.db access failed: {error}")
        return int(ExitCode.UI_DB_ACCESS_FAILED)

    try:
        notebook = load_notebook(
            Path(args.ui_db),
            args.notebook_name,
            version_id=args.nb_version,
            require_ui_closed=args.require_ui_closed,
            notebook_id=args.notebook_id,
        )
    except (NotebookNotFoundError, AmbiguousNotebookError) as error:
        LOGGER.error("notebook_not_found", error=str(error))
        return int(ExitCode.NOTEBOOK_NOT_FOUND)
    except (StorageVersionMismatchError, UiDbAccessError) as error:
        LOGGER.error("ui_db_access_failed", error=f"ui.db access failed: {error}")
        return int(ExitCode.UI_DB_ACCESS_FAILED)

    if args.notebook_name is None:
        try:
            output_path = resolve_output_path(
                args.output,
                notebook.name,
                args.output_dir,
            )
        except OutputPathError as error:
            LOGGER.error("output_path_rejected", error=str(error))
            return int(ExitCode.OUTPUT_PATH_REJECTED)

    try:
        if not confirm_execution(notebook.cells, assume_yes=args.yes):
            LOGGER.error(
                "confirmation_required",
                error="Execution confirmation required; pass --yes to run "
                "non-interactively.",
            )
            return int(ExitCode.CONFIRMATION_DECLINED)

        target_db, _used_memory_fallback = resolve_target_db(notebook, args.db)
        report = execute_notebook(
            notebook,
            target_db,
            allow_writes=args.allow_writes,
            max_rows=args.max_rows,
            cell_timeout=args.cell_timeout,
            stop_on_error=args.stop_on_error,
            no_external_access=args.no_external_access,
        )
        metadata = ExportMetadata(
            exported_at_utc=_utc_now_z(),
            duckdb_version=duckdb.__version__,
            notebook_version_id=notebook.version_id,
            tool_version=__version__,
            warnings=report.warnings,
        )
        html = render_html(notebook, report, metadata)
        final_output_path = dedupe_output_path(output_path)
        _write_html(final_output_path, html)
        if _cell_error_exit_required(report, stop_on_error=args.stop_on_error):
            return int(ExitCode.CELL_ERROR)
        return int(ExitCode.OK)
    except (StorageVersionMismatchError, UiDbAccessError) as error:
        LOGGER.error("ui_db_access_failed", error=f"ui.db access failed: {error}")
        return int(ExitCode.UI_DB_ACCESS_FAILED)
    except OutputPathError as error:
        LOGGER.error("output_path_rejected", error=str(error))
        return int(ExitCode.OUTPUT_PATH_REJECTED)
    except ExporterError as error:
        LOGGER.error("export_failed", error=str(error))
        return int(ExitCode.UI_DB_ACCESS_FAILED)
    except (duckdb.Error, OSError) as error:
        LOGGER.error("export_failed", error=str(error))
        return int(ExitCode.UI_DB_ACCESS_FAILED)


if __name__ == "__main__":
    sys.exit(main())
