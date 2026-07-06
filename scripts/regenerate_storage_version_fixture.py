"""Regenerate the newer-storage-version fixture used by UT-R-013.

test design doc 2.4 section requires a database file under
``tests/fixtures/storage_version/`` whose DuckDB storage version is newer
than what the oldest supported ``duckdb`` package (pyproject.toml's floor)
can read, so that UT-R-013 can exercise the real
``StorageVersionMismatchError`` path instead of skipping.

This script must therefore run under a ``duckdb`` build *newer* than the
project floor. When no newer stable release exists on PyPI yet, a nightly
dev build works, because ``STORAGE_VERSION 'latest'`` on a dev build writes
the in-development format (version number 999), which no stable release can
read::

    uv run --no-project --isolated --with duckdb --prerelease allow \\
        python scripts/regenerate_storage_version_fixture.py

The file is written with the DuckDB UI ``ui.db`` schema (design doc 6.3#9)
so it looks like a real UI database that happens to be too new, matching
the failure users actually hit. After regenerating, confirm the fixture is
rejected by the project environment::

    uv run pytest tests/test_reader.py -k ut_r_013

UT-R-013 skips itself (with a regeneration notice) once the project's
``duckdb`` becomes new enough to open the fixture; that skip is the signal
to re-run this script under an even newer build (test design doc 2.4).
"""

from pathlib import Path

import duckdb

FIXTURE_DIR = Path(__file__).resolve().parent.parent / (
    "tests/fixtures/storage_version"
)
FIXTURE_PATH = FIXTURE_DIR / "ui_newer_storage.db"

UI_DB_DDL = """
CREATE TABLE IF NOT EXISTS fixture.notebooks(
  id UUID NOT NULL PRIMARY KEY,
  name VARCHAR NOT NULL,
  created TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS fixture.notebook_versions(
  notebook_id UUID NOT NULL,
  version INTEGER NOT NULL,
  title VARCHAR NOT NULL,
  json VARCHAR NOT NULL,
  created TIMESTAMP NOT NULL,
  expires TIMESTAMP
);
"""


def main() -> None:
    """Write the fixture with the newest storage format this build supports.

    Returns
    -------
    None
        The fixture file is (re)created on disk.

    Raises
    ------
    duckdb.Error
        Raised when the running DuckDB build cannot attach or write the
        fixture database.
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    connection = duckdb.connect()
    try:
        connection.execute(
            # Smallest allowed block size keeps the committed binary small.
            f"ATTACH '{FIXTURE_PATH.as_posix()}' AS fixture "
            "(STORAGE_VERSION 'latest', BLOCK_SIZE 16384)"
        )
        for statement in UI_DB_DDL.strip().split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            """
            INSERT INTO fixture.notebooks
            VALUES (uuid(), 'storage-version-fixture', now())
            """
        )
        connection.execute("DETACH fixture")
    finally:
        connection.close()

    print(f"duckdb version used: {duckdb.__version__}")
    print(f"fixture written: {FIXTURE_PATH}")
    print(
        "Verify rejection under the project environment: "
        "uv run pytest tests/test_reader.py -k ut_r_013"
    )


if __name__ == "__main__":
    main()
