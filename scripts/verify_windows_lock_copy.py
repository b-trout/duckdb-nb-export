"""Verify locked-file copy behavior for the snapshot reader (design doc 6.2#8).

The default reader path copies ``ui.db`` + ``ui.db.wal`` while DuckDB UI
holds a read-write connection on them. On Linux this is verified to work
(design doc 6.3#5, AT-005). Windows file locking is different enough that
the same copy could fail outright, which is why Windows is outside the
Phase 1 support target until this script settles the question (GitHub
issue #5).

Run on the platform under test::

    uv run python scripts/verify_windows_lock_copy.py

Exit code 0 means every step passed (OS copy of body+WAL while locked,
read-only open of the copy, and the package's ``copy_ui_db`` snapshot
path). Any failure exits 1 after printing a RESULT line per step, so a CI
job stays readable either way.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import duckdb

from duckdb_ui_notebook_export.reader import copy_ui_db

_RESULTS: list[tuple[str, bool, str]] = []


def _record(step: str, ok: bool, detail: str = "") -> None:
    """Record and print one verification step result.

    Parameters
    ----------
    step
        Short step label.
    ok
        Whether the step passed.
    detail
        Optional failure detail or extra context.

    Returns
    -------
    None
        The result is appended to the module-level result list.
    """
    _RESULTS.append((step, ok, detail))
    status = "PASS" if ok else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    print(f"RESULT {status}: {step}{suffix}", flush=True)


def _start_lock_holder(db_path: Path) -> subprocess.Popen[str]:
    """Start a subprocess holding a RW connection with a WAL-only change.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file to create and hold open.

    Returns
    -------
    subprocess.Popen[str]
        Running subprocess. The caller must terminate it via stdin close.

    Raises
    ------
    RuntimeError
        Raised if the subprocess does not report readiness.
    """
    child = textwrap.dedent(
        """
        import duckdb
        import sys

        con = duckdb.connect(sys.argv[1])
        con.execute("CREATE TABLE marker(id INTEGER, label VARCHAR)")
        con.execute("INSERT INTO marker VALUES (1, 'copied')")
        print("READY", flush=True)
        sys.stdin.read()
        con.close()
        """
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child, str(db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.stdout is None:
        raise RuntimeError("lock holder has no stdout pipe")
    ready = proc.stdout.readline().strip()
    if ready != "READY":
        raise RuntimeError(f"lock holder failed to start: {ready!r}")
    return proc


def main() -> int:
    """Run the locked-copy verification steps and summarize.

    Returns
    -------
    int
        Zero when every step passed, one otherwise.
    """
    print(f"platform: {sys.platform}")
    print(f"python: {sys.version.split()[0]}")
    print(f"duckdb: {duckdb.__version__}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="lock-copy-verify-"))
    source = tmp_dir / "ui.db"
    wal = tmp_dir / "ui.db.wal"
    copied_dir = tmp_dir / "manual-copy"
    copied_dir.mkdir()

    holder = _start_lock_holder(source)
    try:
        if wal.exists():
            _record("WAL file exists while writer is open", True)
        else:
            _record(
                "WAL file exists while writer is open",
                False,
                "no ui.db.wal; WAL-set copy premise does not hold here",
            )

        try:
            shutil.copy2(source, copied_dir / "ui.db")
            if wal.exists():
                shutil.copy2(wal, copied_dir / "ui.db.wal")
            _record("OS copy of locked ui.db (+wal)", True)
        except OSError as error:
            _record("OS copy of locked ui.db (+wal)", False, repr(error))

        copied_db = copied_dir / "ui.db"
        if copied_db.exists():
            try:
                with duckdb.connect(str(copied_db), read_only=True) as con:
                    rows = con.execute("SELECT id, label FROM marker").fetchall()
                _record(
                    "read-only open of the copied snapshot",
                    rows == [(1, "copied")],
                    f"rows={rows!r}",
                )
            except Exception as error:
                _record("read-only open of the copied snapshot", False, repr(error))

        try:
            snapshot = copy_ui_db(source, tmp_dir / "reader-copy")
            with duckdb.connect(str(snapshot), read_only=True) as con:
                rows = con.execute("SELECT id, label FROM marker").fetchall()
            _record(
                "package copy_ui_db snapshot path",
                rows == [(1, "copied")],
                f"rows={rows!r}",
            )
        except Exception as error:
            _record("package copy_ui_db snapshot path", False, repr(error))
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=10)

    failed = [step for step, ok, _ in _RESULTS if not ok]
    print()
    if failed:
        print(f"SUMMARY: {len(failed)} step(s) failed: {failed}")
        return 1
    print("SUMMARY: all locked-copy steps passed on this platform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
