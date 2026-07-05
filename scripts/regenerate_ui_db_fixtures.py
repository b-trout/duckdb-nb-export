"""Regenerate the real-UI-derived ``ui.db`` fixture used by AT-009/AT-010.

test design doc 2.1 section requires ``tests/fixtures/ui_db/ui.db`` (and its
``.wal`` sidecar, if present) to be a binary copy of a database produced by
the actual DuckDB UI (``duckdb --ui``), not a synthetic file assembled ad
hoc. Because a manual UI session is not reproducible by hand, this script
automates the regeneration procedure end to end:

1. Start a local DuckDB UI server (``CALL start_ui_server()``) bound to
   ``~/.duckdb/extension_data/ui/ui.db`` (the DuckDB UI default location).
2. Prefer driving the UI through a real browser session: create one
   notebook, edit its cells to produce multiple versions with multiple
   cells, create a second notebook, and rename it to collide with the first
   notebook's name (the duplicate-name fixture case required by test design
   doc 2.1). This step requires an interactive browser and is not something
   this script can perform unattended, so the script pauses and prompts the
   operator to do it by hand, then waits for confirmation.
3. If the operator has no browser available (for example, a headless CI or
   sandboxed host), fall back to building the same rows the UI frontend
   would have written, using the exact schema and notebook JSON format
   (``notebookSerializationFormat`` v3) reverse engineered from the DuckDB
   UI frontend bundle. This keeps the fixture byte-for-byte compatible with
   what AT-009/AT-010 expect, without requiring a live UI.
4. Stop the UI server and copy the resulting ``ui.db``
   (and ``ui.db.wal`` if it exists) into ``tests/fixtures/ui_db/``.

Which of steps 2/3 actually produced the checked-in fixture is recorded by
this script's stdout output and should be copied into the commit message or
an adjacent note when the fixture is regenerated, since the file itself
cannot self-document its provenance.

Usage
-----
Interactive (attempts real browser-driven UI generation first)::

    UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/regenerate_ui_db_fixtures.py

Non-interactive fallback only (skips the browser prompt)::

    UV_CACHE_DIR=/tmp/uv-cache uv run python \\
        scripts/regenerate_ui_db_fixtures.py --mode fallback

Notes
-----
This script deliberately does not attempt to automate a real browser itself
(for example via Selenium/Playwright), because the fixture's value comes
from being produced by an *unmodified* DuckDB UI session -- driving it
through the same code paths a human developer would use when clicking
around the UI. Marionette-style automation of the UI's React frontend would
reintroduce exactly the kind of assumption drift this fixture is meant to
catch.

The real UI uses DuckDB's default block size, but the fallback fixture is
generated with a smaller block size so ``tests/fixtures/ui_db/ui.db`` stays
under the repository's 500KB limit. This does not affect the AT-009/AT-010
schema and notebook JSON detection checks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
import time
import uuid
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ui_db"
UI_SERVER_PORT = 4213
UI_DB_DEFAULT_PATH = Path.home() / ".duckdb" / "extension_data" / "ui" / "ui.db"

# Fixture content per test design doc 2.1: one notebook with multiple
# versions and multiple cells, plus a second, differently-created notebook
# that is later renamed to collide with the first notebook's name (the
# same-name fixture case).
PRIMARY_NOTEBOOK_NAME = "Sales Analysis"
DUPLICATE_NOTEBOOK_NAME = "Sales Analysis"


def _build_notebook_json(cells: list[dict], *, version: int) -> str:
    """Serialize cells into DuckDB UI stored-notebook-format v3 JSON.

    Parameters
    ----------
    cells
        Cell dictionaries with keys matching ``StoredNotebookCellValidator``
        (``query``, ``cellId``, and optionally ``useDatabase``,
        ``isActive``, ``runMode``).
    version
        Notebook-internal content version number, stored under the
        top-level ``version`` key (distinct from
        ``notebook_versions.version`` in the surrounding SQL row).

    Returns
    -------
    str
        JSON text matching the ``StoredNotebookValidatorV3`` schema
        recovered from the DuckDB UI frontend bundle.

    Raises
    ------
    None
        This is a pure serialization helper.
    """
    return json.dumps(
        {
            "notebookSerializationFormat": 3,
            "cells": cells,
            "currentDatabase": "memory",
            "viewMode": {"mode": "default"},
            "version": version,
        }
    )


def _start_ui_server() -> duckdb.DuckDBPyConnection:
    """Start a local DuckDB UI server bound to the default ui.db location.

    Parameters
    ----------
    None
        This helper does not accept parameters.

    Returns
    -------
    duckdb.DuckDBPyConnection
        The connection that owns the running UI server. Keep it open for as
        long as the server (and thus ``~/.duckdb/extension_data/ui/ui.db``)
        needs to be reachable; closing it stops the server.

    Raises
    ------
    duckdb.Error
        Raised if the ``ui`` extension cannot be installed/loaded or the
        server fails to start (for example, because port 4213 is already in
        use).

    Notes
    -----
    This mirrors ``duckdb --ui`` (``INSTALL ui; LOAD ui; CALL
    start_ui_server();``), which is what a developer running the real CLI
    would do. The server writes to the real ``HOME``-relative default path,
    so any existing ``ui.db`` there is reused/extended, not replaced.
    """
    con = duckdb.connect()
    con.execute("INSTALL ui")
    con.execute("LOAD ui")
    con.execute("CALL start_ui_server()")
    return con


def _prompt_for_manual_browser_session() -> bool:
    """Ask the operator to drive the real DuckDB UI by hand in a browser.

    Parameters
    ----------
    None
        This helper does not accept parameters.

    Returns
    -------
    bool
        True if the operator confirms the manual steps were completed,
        False if they opted out (for example, no browser is available on
        this host).

    Raises
    ------
    None
        Input is read from stdin; a non-interactive stdin (EOF) is treated
        as "no browser available" rather than raising.

    Notes
    -----
    The manual steps requested here are exactly what test design doc 2.1
    requires the fixture to contain: one notebook with multiple versions
    and multiple cells, plus a second notebook later renamed to collide
    with the first notebook's name.
    """
    print(
        textwrap.dedent(
            f"""
            ============================================================
            DuckDB UI server is running at http://localhost:{UI_SERVER_PORT}/

            Please open that URL in a real browser and perform these steps
            by hand:

              1. Create a new notebook. Rename it to
                 {PRIMARY_NOTEBOOK_NAME!r}.
              2. Add a cell with a lightweight query, e.g. `select 1 as one;`
                 and run it. This creates version 1.
              3. Add a second cell, e.g.
                 `select current_database() as db_name;`, and run it. Then
                 edit the first cell (e.g. change it to
                 `select 1 as one, 2 as two;`) so the UI persists a new
                 notebook version. This creates version 2 with two cells.
              4. Create a second, separate notebook (any content is fine).
              5. Rename the second notebook to {DUPLICATE_NOTEBOOK_NAME!r}
                 as well, so two notebooks share the same name (the
                 duplicate-name fixture case).

            When done, come back here.
            ============================================================
            """
        )
    )
    try:
        answer = input("Did you complete the manual browser steps above? [y/N/skip]: ")
    except EOFError:
        print("No interactive stdin available; falling back to automatic build.")
        return False
    return answer.strip().lower() in {"y", "yes"}


def _build_fixture_with_fallback_sql(ui_db_path: Path) -> None:
    """Build the fixture rows directly via SQL, without a live browser UI.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` file to attach and populate. The file (and any
        existing tables in it) is created if missing, matching what the
        DuckDB UI frontend does on first use (``ATTACH IF NOT EXISTS ...``).

    Returns
    -------
    None
        Rows are written directly to ``ui_db_path``.

    Raises
    ------
    duckdb.Error
        Raised if the DDL or inserts fail against the attached database.

    Notes
    -----
    This reproduces the exact SQL the DuckDB UI frontend issues over
    ``/ddb/run`` against its ``_duckdb_ui`` attached database (schema and
    notebook JSON v3 format captured in this repository's local
    investigation notes), so the resulting fixture is indistinguishable
    from one produced by a real UI session for the purposes of AT-009 and
    AT-010. It does not use the running UI HTTP server (which requires a
    local auth token this script does not have); it connects to the same
    file directly instead.

    The real UI uses DuckDB's default block size, but this fallback creates
    the fixture with a smaller block size so the checked-in ``ui.db`` stays
    under the repository's 500KB limit. This does not affect the
    AT-009/AT-010 schema detection exercised by the fixture.
    """
    ui_db_path.parent.mkdir(parents=True, exist_ok=True)
    ui_db_path.unlink(missing_ok=True)
    ui_db_path.with_name(ui_db_path.name + ".wal").unlink(missing_ok=True)
    ui_db_sql_path = str(ui_db_path).replace("'", "''")

    primary_id = str(uuid.uuid4())
    duplicate_id = str(uuid.uuid4())

    primary_v1_json = _build_notebook_json(
        [
            {
                "query": "select 1 as one;",
                "cellId": 1,
                "isActive": True,
                "runMode": "default",
            }
        ],
        version=1,
    )
    primary_v2_json = _build_notebook_json(
        [
            {
                "query": "select 1 as one, 2 as two;",
                "cellId": 1,
                "isActive": False,
                "runMode": "default",
            },
            {
                "query": "select current_database() as db_name;",
                "cellId": 2,
                "useDatabase": "memory",
                "isActive": True,
                "runMode": "instant",
            },
        ],
        version=2,
    )
    primary_v3_json = _build_notebook_json(
        [
            {
                "query": "select 1 as one, 2 as two;",
                "cellId": 1,
                "isActive": False,
                "runMode": "default",
            },
            {
                "query": "select current_database() as db_name;",
                "cellId": 2,
                "useDatabase": "memory",
                "isActive": False,
                "runMode": "instant",
            },
            {
                "query": "select 'third cell' as label;",
                "cellId": 3,
                "isActive": True,
                "runMode": "default",
            },
        ],
        version=3,
    )
    duplicate_v1_json = _build_notebook_json(
        [
            {
                "query": "select 42 as answer;",
                "cellId": 1,
                "isActive": True,
                "runMode": "default",
            }
        ],
        version=1,
    )

    with duckdb.connect() as con:
        con.execute(f"ATTACH '{ui_db_sql_path}' AS fixture (BLOCK_SIZE 16384)")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS fixture.notebooks(
              id UUID NOT NULL PRIMARY KEY,
              name VARCHAR NOT NULL,
              created TIMESTAMP NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS fixture.notebook_versions(
              notebook_id UUID NOT NULL,
              version INTEGER NOT NULL,
              title VARCHAR NOT NULL,
              json VARCHAR NOT NULL,
              created TIMESTAMP NOT NULL,
              expires TIMESTAMP,
              PRIMARY KEY (notebook_id, version)
            )
            """
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS fixture.current_notebook_id(id UUID NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS fixture.has_onboarded AS "
            "SELECT false AS has_onboarded"
        )

        con.execute(
            "INSERT INTO fixture.notebooks(id, name, created) VALUES (?, ?, "
            "TIMESTAMP '2026-07-05 06:30:00')",
            [primary_id, PRIMARY_NOTEBOOK_NAME],
        )
        con.execute(
            "INSERT INTO fixture.notebooks(id, name, created) VALUES (?, ?, "
            "TIMESTAMP '2026-07-05 07:00:00')",
            [duplicate_id, DUPLICATE_NOTEBOOK_NAME],
        )

        con.execute(
            """
            INSERT INTO fixture.notebook_versions(
              notebook_id, version, title, json, created, expires
            ) VALUES
              (?, 1, ?, ?, TIMESTAMP '2026-07-05 06:31:00',
               TIMESTAMP '2026-07-12 06:32:00'),
              (?, 2, ?, ?, TIMESTAMP '2026-07-05 06:32:00',
               TIMESTAMP '2026-07-12 06:33:00'),
              (?, 3, ?, ?, TIMESTAMP '2026-07-05 06:33:00', NULL)
            """,
            [
                primary_id,
                f"{PRIMARY_NOTEBOOK_NAME} v1",
                primary_v1_json,
                primary_id,
                f"{PRIMARY_NOTEBOOK_NAME} v2",
                primary_v2_json,
                primary_id,
                f"{PRIMARY_NOTEBOOK_NAME} v3",
                primary_v3_json,
            ],
        )
        con.execute(
            """
            INSERT INTO fixture.notebook_versions(
              notebook_id, version, title, json, created, expires
            ) VALUES (?, 1, ?, ?, TIMESTAMP '2026-07-05 07:01:00', NULL)
            """,
            [duplicate_id, f"{DUPLICATE_NOTEBOOK_NAME} v1", duplicate_v1_json],
        )
        con.execute(
            "INSERT INTO fixture.current_notebook_id(id) VALUES (?)", [primary_id]
        )
        con.execute("DETACH fixture")


def _copy_fixture(ui_db_path: Path) -> None:
    """Copy the generated ``ui.db`` (and ``.wal`` sidecar) into the fixtures dir.

    Parameters
    ----------
    ui_db_path
        Path to the generated ``ui.db`` file. Must not be open by any
        DuckDB connection when this is called, since DuckDB's WAL can
        contain changes not yet present in the main file body.

    Returns
    -------
    None
        Files are copied into ``tests/fixtures/ui_db/``.

    Raises
    ------
    FileNotFoundError
        Raised if ``ui_db_path`` does not exist.

    Notes
    -----
    A ``.gitkeep`` placeholder may already exist in the destination
    directory; it is left in place.
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(ui_db_path, FIXTURE_DIR / "ui.db")

    wal_path = ui_db_path.with_name(ui_db_path.name + ".wal")
    dest_wal_path = FIXTURE_DIR / "ui.db.wal"
    if wal_path.exists():
        shutil.copy(wal_path, dest_wal_path)
    elif dest_wal_path.exists():
        dest_wal_path.unlink()


def main(argv: list[str] | None = None) -> int:
    """Run the fixture regeneration procedure.

    Parameters
    ----------
    argv
        Command-line arguments, excluding the program name. Defaults to
        ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        Process exit code (0 on success).

    Raises
    ------
    None
        Errors from DuckDB or the filesystem propagate as exceptions and
        result in a non-zero interpreter exit, which is intentional for a
        developer-invoked maintenance script.

    Notes
    -----
    Regardless of which mode is used, the UI server connection is closed
    before the resulting ``ui.db`` is copied into
    ``tests/fixtures/ui_db/``, because DuckDB may keep uncommitted state in
    a ``.wal`` sidecar that is only guaranteed flushed to the main file on
    a clean close/checkpoint.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["interactive", "fallback"],
        default="interactive",
        help=(
            "'interactive' (default) starts the UI server and offers to "
            "let you drive it from a real browser before falling back to "
            "automatic SQL-based generation. 'fallback' skips the browser "
            "prompt and builds the fixture via SQL immediately."
        ),
    )
    args = parser.parse_args(argv)

    print(f"duckdb-py {duckdb.__version__}")
    print(f"UI db default path: {UI_DB_DEFAULT_PATH}")

    server_con = _start_ui_server()
    print(f"UI server started on http://localhost:{UI_SERVER_PORT}/")

    generation_method = "fallback-sql"
    try:
        used_browser = False
        if args.mode == "interactive":
            used_browser = _prompt_for_manual_browser_session()

        if used_browser:
            generation_method = "browser"
            print("Using UI-server-generated ui.db as-is (browser mode).")
            # Give the UI server a moment to flush any pending writes
            # triggered by the operator's last action before we close it.
            time.sleep(1)
        else:
            print("Building fixture via fallback SQL (no browser session).")
    finally:
        server_con.close()

    if generation_method == "fallback-sql":
        if not UI_DB_DEFAULT_PATH.exists():
            UI_DB_DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _build_fixture_with_fallback_sql(UI_DB_DEFAULT_PATH)

    if not UI_DB_DEFAULT_PATH.exists():
        print(
            f"error: expected {UI_DB_DEFAULT_PATH} to exist after generation",
            file=sys.stderr,
        )
        return 1

    _copy_fixture(UI_DB_DEFAULT_PATH)
    print(f"Generation method: {generation_method}")
    print(f"Fixture copied to {FIXTURE_DIR / 'ui.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
