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
import logging
import math
import os
import re
import sys
import tempfile
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
    TargetDatabaseError,
    UiDbAccessError,
)
from duckdb_ui_notebook_export.executor import (
    _URI_SCHEME_PATTERN,
    CellStatus,
    ExecutionReport,
    display_target_database,
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
from duckdb_ui_notebook_export.renderer import (
    ExportMetadata,
    mask_secrets,
    render_html,
)

LOGGER = structlog.get_logger()
_UNSAFE_FILENAME_PATTERN = re.compile(r"[\s/\\:]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


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


def _sanitized_beyond_whitespace(name: str, sanitized_name: str) -> bool:
    """Return whether sanitization changed more than whitespace to underscores.

    Parameters
    ----------
    name
        Original notebook name.
    sanitized_name
        Result of ``sanitize_filename(name)``.

    Returns
    -------
    bool
        True when ``sanitized_name`` differs from a variant of ``name`` that
        only replaces whitespace runs with underscores (and strips leading
        and trailing underscores); False when the only change was
        whitespace substitution.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    Spaces in notebook names are common and expected to become underscores
    in the output filename, so that alone should not warn. Path separators
    (``/``, ``\\``) or colons being replaced indicates a more surprising
    rename and should still warn.
    """
    whitespace_only_variant = _WHITESPACE_PATTERN.sub("_", name).strip("_")
    return sanitized_name != whitespace_only_variant


def dedupe_output_path(path: Path) -> Path:
    """Reserve and return a free output path by appending a numeric suffix.

    Parameters
    ----------
    path
        Desired output path.

    Returns
    -------
    pathlib.Path
        ``path`` when it did not exist, otherwise ``<name>-N.html``. The
        returned path exists on disk as an empty reservation file.

    Raises
    ------
    OSError
        Raised when the reservation file cannot be created (for example the
        parent directory is not writable).

    Notes
    -----
    Instead of probing with ``exists()`` (which leaves a window where a
    concurrent process can claim the same name), each candidate is reserved
    by creating it with ``open("x")`` (create-exclusive). The empty
    reservation file is later replaced atomically by ``_write_html``'s
    ``os.replace``; the caller owns cleaning up the reservation if the
    write never happens (issue #62). The parent directory is created when
    missing so the reservation can be placed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    candidate = path
    counter = 0
    while True:
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            counter += 1
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        else:
            return candidate


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
        if _sanitized_beyond_whitespace(notebook_name, sanitized_name):
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


_CELL_PREVIEW_MAX_LINES = 2
_CELL_PREVIEW_MAX_CHARS = 160


def _target_db_display(target_db: str) -> str:
    """Return a credential-safe display form of the target database.

    Parameters
    ----------
    target_db
        Resolved target database connect string.

    Returns
    -------
    str
        ``:memory:`` unchanged; URI-style connect strings reduced to their
        scheme (for example ``md: (URI)``); plain paths as given.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    URI connect strings can embed credentials (for example
    ``postgres://user:password@host/db``), so only the scheme is shown.
    Plain paths are printed in full: the terminal is the user's own, unlike
    exported HTML. The scheme detection uses the same pattern as the
    executor's local-path check.
    """
    if target_db == ":memory:":
        return target_db
    match = _URI_SCHEME_PATTERN.match(target_db)
    if match is not None:
        return f"{match.group(0)} (URI)"
    return target_db


def _cell_preview(sql: str) -> str:
    """Return a masked, truncated one-glance preview of a cell's SQL.

    Parameters
    ----------
    sql
        Raw cell SQL text.

    Returns
    -------
    str
        ``mask_secrets``-sanitized SQL reduced to at most the first two
        non-empty lines and at most 160 characters, with `` …`` appended
        when anything was cut.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    Masking runs first so that even a truncated preview can never leak a
    ``CREATE SECRET`` parameter value into the terminal (issue #50).
    """
    masked = mask_secrets(sql)
    lines = [line.strip() for line in masked.splitlines() if line.strip()]
    truncated = len(lines) > _CELL_PREVIEW_MAX_LINES
    preview = "\n".join(lines[:_CELL_PREVIEW_MAX_LINES])
    if len(preview) > _CELL_PREVIEW_MAX_CHARS:
        preview = preview[:_CELL_PREVIEW_MAX_CHARS]
        truncated = True
    if truncated:
        preview += " …"
    return preview


def confirm_execution(
    cells: list[Cell],
    *,
    target_db_display: str,
    write_mode: str,
    output_path: Path,
    notebook_name: str,
    version_id: str,
    assume_yes: bool,
) -> bool:
    """Confirm that notebook cells should be executed.

    Parameters
    ----------
    cells
        Cells whose SQL previews should be shown before execution.
    target_db_display
        Credential-safe display form of the target database (see
        ``_target_db_display``).
    write_mode
        Human-readable write-mode label (see ``_write_mode_display``).
    output_path
        Resolved HTML output path.
    notebook_name
        Human-readable notebook name.
    version_id
        Selected notebook version identifier.
    assume_yes
        Whether confirmation should be skipped.

    Returns
    -------
    bool
        True when execution is confirmed, False when it is declined
        (including when the prompt is ended with EOF, e.g. Ctrl-D).

    Raises
    ------
    KeyboardInterrupt
        Propagated from ``input`` when the user presses Ctrl-C; the CLI
        entry point maps it to ``ExitCode.INTERRUPTED``.

    Notes
    -----
    In a non-TTY environment with ``assume_yes=False``, this function must
    return False instead of raising ``UiDbAccessError`` or ``SystemExit``. The
    caller maps that result to ``ExitCode.CONFIRMATION_DECLINED``. EOF at the
    prompt is treated the same as answering "n" (issue #45); previously the
    ``EOFError`` escaped as a raw traceback with exit code 1. Cell SQL is
    shown as a masked, truncated preview rather than in full, and the header
    names the notebook, version, cell count, target database, write mode,
    and output path so the user can judge the blast radius before answering
    (issue #50).
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False

    sys.stdout.write("About to execute notebook cells:\n")
    sys.stdout.write(f"  Notebook:        {notebook_name}\n")
    sys.stdout.write(f"  Version:         {version_id}\n")
    sys.stdout.write(f"  Cells:           {len(cells)}\n")
    sys.stdout.write(f"  Target database: {target_db_display}\n")
    sys.stdout.write(f"  Write mode:      {write_mode}\n")
    sys.stdout.write(f"  Output path:     {output_path}\n")

    for index, cell in enumerate(cells, start=1):
        sys.stdout.write(f"\n[{index}]\n{_cell_preview(cell.sql)}\n")

    try:
        response = input("Continue with notebook execution? [y/N] ")
    except EOFError:
        return False
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


def _write_mode_display(*, allow_writes: bool, read_only: bool) -> str:
    """Return a human-readable write-mode label for HTML metadata and prompts.

    Parameters
    ----------
    allow_writes
        Whether ``--allow-writes`` was passed.
    read_only
        Whether ``--read-only`` was passed.

    Returns
    -------
    str
        ``"writes committed (--allow-writes)"``, ``"read-only"``, or
        ``"rollback (default)"``.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    ``--allow-writes`` and ``--read-only`` are mutually exclusive at the
    argparse level, so at most one of them is ever True.
    """
    if allow_writes:
        return "writes committed (--allow-writes)"
    if read_only:
        return "read-only"
    return "rollback (default)"


def _cell_error_exit_required(
    report: ExecutionReport,
    *,
    stop_on_error: bool,
    no_fail_on_cell_error: bool,
) -> bool:
    """Return whether execution results should map to cell-error exit code.

    Parameters
    ----------
    report
        Notebook execution report.
    stop_on_error
        Whether CLI execution requested early stop on cell failure. This no
        longer changes the exit-code outcome (any non-OK cell result already
        triggers ``ExitCode.CELL_ERROR`` by default); it is accepted for
        backward-compatible call signatures and documentation purposes only.
    no_fail_on_cell_error
        Whether ``--no-fail-on-cell-error`` was passed, restoring the
        pre-#33 behavior of exiting 0 despite plain cell failures.

    Returns
    -------
    bool
        True when the CLI should return ``ExitCode.CELL_ERROR``.

    Notes
    -----
    Timeouts and abandoned execution always require ``ExitCode.CELL_ERROR``,
    even with ``--no-fail-on-cell-error``. Without that flag, any cell
    result that is not ``CellStatus.OK`` also requires ``ExitCode.CELL_ERROR``
    by default (issue #33).
    """
    del stop_on_error
    if report.abandoned:
        return True
    if any(result.status is CellStatus.TIMEOUT for result in report.cell_results):
        return True
    if no_fail_on_cell_error:
        return False
    return any(result.status is not CellStatus.OK for result in report.cell_results)


_ERROR_MESSAGE_TRUNCATE_LENGTH = 300


def _report_cell_failures(report: ExecutionReport, output_path: Path) -> None:
    """Log one ERROR event per failed cell, plus a summary, to stderr.

    Parameters
    ----------
    report
        Notebook execution report.
    output_path
        Final HTML output path, named in the summary event so readers know
        where to find full details.

    Returns
    -------
    None
        Log events are written to stderr through the direct stderr logger.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    This makes cell failures visible on stderr even though the export
    itself completes and writes HTML; previously a non-OK cell result was
    only visible in the rendered HTML, and the process exit code (2) gave
    no on-screen indication of what failed. Called before the CLI decides
    the final exit code, regardless of ``--no-fail-on-cell-error`` (only
    ``_cell_error_exit_required`` decides whether the process exit code
    reflects the failures).
    """
    logger = _direct_stderr_logger()
    failed_count = 0
    for index, result in enumerate(report.cell_results, start=1):
        if result.status is CellStatus.OK:
            continue
        failed_count += 1
        error_message = result.error_message or ""
        if len(error_message) > _ERROR_MESSAGE_TRUNCATE_LENGTH:
            error_message = error_message[:_ERROR_MESSAGE_TRUNCATE_LENGTH] + "..."
        logger.error(
            "cell_failed",
            cell_index=index,
            status=result.status.value,
            error_message=error_message,
        )

    if failed_count == 0:
        return

    logger.warning(
        "cells_failed_summary",
        failed_count=failed_count,
        total_count=len(report.cell_results),
        error=f"details in {output_path}",
    )


def _write_html(path: Path, html: str) -> None:
    """Write rendered HTML to disk atomically using UTF-8.

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

    Raises
    ------
    OSError
        Raised when the temporary file cannot be written or the atomic
        replace fails; the temporary file is removed before re-raising.

    Notes
    -----
    The document is first written to a temporary file in the same directory
    and then moved onto ``path`` with ``os.replace``, so a reader never
    observes a partially written file and an existing file (including the
    empty reservation created by ``dedupe_output_path``, or the previous
    export under ``--force``) is replaced atomically (issue #62).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(html)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _positive_int_arg(value: str) -> int:
    """Parse and validate an argparse integer argument that must be positive.

    Parameters
    ----------
    value
        Raw command-line argument text.

    Returns
    -------
    int
        Parsed integer value.

    Raises
    ------
    argparse.ArgumentTypeError
        Raised when ``value`` is not an integer, or is less than 1.
    """
    try:
        parsed = int(value)
    except ValueError as error:
        message = f"invalid int value: {value!r}"
        raise argparse.ArgumentTypeError(message) from error
    if parsed < 1:
        message = f"must be a positive integer (>= 1), got {value!r}"
        raise argparse.ArgumentTypeError(message)
    return parsed


def _nb_version_arg(value: str) -> str:
    """Parse and validate the ``--nb-version`` argument as an integer string.

    Parameters
    ----------
    value
        Raw command-line argument text.

    Returns
    -------
    str
        The validated version identifier, unchanged (``load_notebook``
        expects a string and converts it internally).

    Raises
    ------
    argparse.ArgumentTypeError
        Raised when ``value`` does not parse as an integer.

    Notes
    -----
    ``--nb-version`` selects a ``notebook_versions.version`` row, which is
    always an integer in DuckDB UI's stored schema. Previously a
    non-integer value reached ``load_notebook``, which raised
    ``UiDbAccessError`` and mapped to exit code 4 ("ui.db access failed"),
    even though the problem was an invalid argument, not a ``ui.db``
    problem. Rejecting it here instead produces a standard argparse usage
    error (exit code 2), consistent with ``--max-rows`` and
    ``--cell-timeout`` (see GitHub issue #48).
    """
    try:
        int(value)
    except ValueError as error:
        message = f"invalid int value: {value!r}"
        raise argparse.ArgumentTypeError(message) from error
    return value


def _positive_float_arg(value: str) -> float:
    """Parse and validate an argparse float argument that must be positive.

    Parameters
    ----------
    value
        Raw command-line argument text.

    Returns
    -------
    float
        Parsed float value.

    Raises
    ------
    argparse.ArgumentTypeError
        Raised when ``value`` is not a finite float, or is not greater than 0.
    """
    try:
        parsed = float(value)
    except ValueError as error:
        message = f"invalid float value: {value!r}"
        raise argparse.ArgumentTypeError(message) from error
    if not math.isfinite(parsed) or parsed <= 0:
        message = f"must be a positive, finite number, got {value!r}"
        raise argparse.ArgumentTypeError(message)
    return parsed


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

    Notes
    -----
    ``KeyboardInterrupt`` raised anywhere inside the CLI flow (the
    confirmation prompt, notebook execution, rendering, or writing) is
    caught here, logged as a single short ``interrupted`` error event on
    stderr (no traceback), and mapped to ``ExitCode.INTERRUPTED`` (130,
    the shell convention of ``128 + SIGINT``) per issue #45.
    """
    try:
        return _run(argv)
    except KeyboardInterrupt:
        _direct_stderr_logger().error(
            "interrupted",
            error="Interrupted by user (Ctrl-C).",
        )
        return int(ExitCode.INTERRUPTED)


def _run(argv: list[str] | None = None) -> int:
    """Parse arguments and run the export flow.

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
    KeyboardInterrupt
        Propagated to ``main``, which maps it to ``ExitCode.INTERRUPTED``.

    Notes
    -----
    Logging is configured with ``force=True`` after argument parsing, once
    ``-q``/``--quiet`` and ``-v``/``--verbose`` are known, so the CLI's
    chosen level always wins over any earlier default ``configure_logging``
    call (for example one made by a library import or a previous call in
    the same process).
    """
    parser = argparse.ArgumentParser(
        prog="duckdb-nb-export",
        description="Export a DuckDB UI notebook to static HTML.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} (duckdb {duckdb.__version__})",
        help="Show the tool version (and the DuckDB version in use) and exit.",
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
        type=_nb_version_arg,
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
        type=_positive_int_arg,
        default=1000,
        help="Maximum rows to render per cell (default: %(default)s).",
    )
    parser.add_argument(
        "--cell-timeout",
        type=_positive_float_arg,
        default=300.0,
        help="Per-cell execution timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--interrupt-grace",
        type=_positive_float_arg,
        default=30.0,
        help="Seconds to wait after a timeout interrupt before abandoning "
        "execution (default: %(default)s).",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop processing after the first cell error.",
    )
    parser.add_argument(
        "--no-fail-on-cell-error",
        action="store_true",
        help="Exit 0 even when individual cells fail (previous default). "
        "Timeouts and abandoned execution still exit 2.",
    )
    write_mode_group = parser.add_mutually_exclusive_group()
    write_mode_group.add_argument(
        "--allow-writes",
        action="store_true",
        help="Commit notebook changes instead of rolling them back.",
    )
    write_mode_group.add_argument(
        "--read-only",
        action="store_true",
        help="Open the target database in DuckDB read-only mode for a "
        "stronger no-writes guarantee. Notebook cells that create or "
        "modify tables will fail.",
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it exists, instead of writing "
        "to a numeric-suffixed sibling path.",
    )
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only show ERROR-level log events on stderr. Mutually "
        "exclusive with -v/--verbose.",
    )
    verbosity_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show DEBUG-level log events on stderr in addition to the "
        "default INFO level. Mutually exclusive with -q/--quiet.",
    )
    args = parser.parse_args(argv)

    if args.quiet:
        log_level = logging.ERROR
    elif args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    _logging.configure_logging(level=log_level, force=True)

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
        target_db, used_memory_fallback = resolve_target_db(notebook, args.db)
        if not confirm_execution(
            notebook.cells,
            target_db_display=_target_db_display(target_db),
            write_mode=_write_mode_display(
                allow_writes=args.allow_writes,
                read_only=args.read_only,
            ),
            output_path=output_path,
            notebook_name=notebook.name,
            version_id=notebook.version_id,
            assume_yes=args.yes,
        ):
            LOGGER.error(
                "confirmation_required",
                error="Execution confirmation required; pass --yes to run "
                "non-interactively.",
            )
            return int(ExitCode.CONFIRMATION_DECLINED)

        report = execute_notebook(
            notebook,
            target_db,
            allow_writes=args.allow_writes,
            read_only=args.read_only,
            max_rows=args.max_rows,
            cell_timeout=args.cell_timeout,
            interrupt_grace=args.interrupt_grace,
            stop_on_error=args.stop_on_error,
            no_external_access=args.no_external_access,
            used_memory_fallback=used_memory_fallback,
        )
        metadata = ExportMetadata(
            exported_at_utc=_utc_now_z(),
            duckdb_version=duckdb.__version__,
            notebook_version_id=notebook.version_id,
            tool_version=__version__,
            warnings=report.warnings,
            target_database=display_target_database(target_db),
            write_mode=_write_mode_display(
                allow_writes=args.allow_writes,
                read_only=args.read_only,
            ),
        )
        html = render_html(notebook, report, metadata)
        reservation: Path | None = None
        if args.force:
            final_output_path = output_path
        else:
            final_output_path = dedupe_output_path(output_path)
            reservation = final_output_path
            if final_output_path != output_path:
                _direct_stderr_logger().warning(
                    "output_path_deduplicated",
                    requested=str(output_path),
                    actual=str(final_output_path),
                )
        try:
            _write_html(final_output_path, html)
        except BaseException:
            # Remove the empty name reservation created by
            # dedupe_output_path when the write onto it never happened.
            if reservation is not None:
                reservation.unlink(missing_ok=True)
            raise
        sys.stdout.write(f"{final_output_path}\n")
        _report_cell_failures(report, final_output_path)
        if _cell_error_exit_required(
            report,
            stop_on_error=args.stop_on_error,
            no_fail_on_cell_error=args.no_fail_on_cell_error,
        ):
            return int(ExitCode.CELL_ERROR)
        return int(ExitCode.OK)
    except (StorageVersionMismatchError, UiDbAccessError) as error:
        LOGGER.error("ui_db_access_failed", error=f"ui.db access failed: {error}")
        return int(ExitCode.UI_DB_ACCESS_FAILED)
    except OutputPathError as error:
        LOGGER.error("output_path_rejected", error=str(error))
        return int(ExitCode.OUTPUT_PATH_REJECTED)
    except TargetDatabaseError as error:
        LOGGER.error("target_database_missing", error=str(error))
        return int(ExitCode.EXECUTION_FAILED)
    except ExporterError as error:
        LOGGER.error("execution_failed", error=str(error))
        return int(ExitCode.EXECUTION_FAILED)
    except (duckdb.Error, OSError) as error:
        LOGGER.error("execution_failed", error=str(error))
        return int(ExitCode.EXECUTION_FAILED)


if __name__ == "__main__":
    sys.exit(main())
