"""Synthetic DuckDB UI database builders for tests."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import duckdb

_UUID_NAMESPACE = uuid.UUID("d1ffdc0d-0000-4000-8000-000000000001")
_NON_CURRENT_EXPIRES = "2999-01-01T00:00:00Z"


def _coerce_notebook_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        return uuid.uuid5(_UUID_NAMESPACE, value)


def _coerce_version_number(version_id: str, index: int) -> int:
    if version_id.isdecimal():
        return int(version_id)
    return index


def _build_notebook_json(version: int, cells: list[dict]) -> str:
    serialized_cells = []
    for cell_id, cell in enumerate(cells, start=1):
        cell_type = cell["cell_type"]
        if cell_type != "sql":
            raise NotImplementedError(
                "stored notebook format v3 has no representation for cell type "
                f"{cell_type!r}"
            )

        serialized_cells.append(
            {
                "query": cell.get("sql"),
                "cellId": cell_id,
                "isActive": True,
                "runMode": "default",
            }
        )

    return json.dumps(
        {
            "notebookSerializationFormat": 3,
            "cells": serialized_cells,
            "currentDatabase": None,
            "viewMode": {"mode": "default"},
            "version": version,
        },
        separators=(",", ":"),
    )


def build_ui_db(notebooks, dest_dir):
    """Build a synthetic DuckDB UI ``ui.db`` fixture.

    Parameters
    ----------
    notebooks : list[dict]
        Notebook specifications. Each item has ``"name"`` (str),
        ``"notebook_id"`` (str), optional ``"updated_at"`` (ISO 8601 string),
        and ``"versions"`` (list[dict]). Each version has ``"version_id"``
        (str), ``"created_at"`` (ISO 8601 string), and ``"cells"``
        (list[dict]). Each cell has ``"cell_type"`` (str) and ``"sql"``
        (str or None).
    dest_dir : pathlib.Path or str
        Destination directory where ``ui.db`` should be written.

    Returns
    -------
    pathlib.Path
        Path to the generated ``ui.db`` file.

    Raises
    ------
    NotImplementedError
        Raised when a cell has a ``"cell_type"`` other than ``"sql"`` because
        stored notebook format v3 has no representation for chart or other
        non-SQL cell types.

    Notes
    -----
    Non-UUID notebook identifiers are mapped to deterministic UUIDv5 values.
    Numeric version identifiers are stored as their integer value; non-numeric
    version identifiers are stored as one-based positions within the version
    list. The original version identifier is preserved in
    ``notebook_versions.title``.
    """
    dest_path = Path(dest_dir)
    dest_path.mkdir(parents=True, exist_ok=True)
    ui_db_path = dest_path / "ui.db"
    ui_db_path.unlink(missing_ok=True)
    ui_db_path.with_name("ui.db.wal").unlink(missing_ok=True)

    with duckdb.connect(str(ui_db_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notebooks(
              id UUID NOT NULL PRIMARY KEY,
              name VARCHAR NOT NULL,
              created TIMESTAMP NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notebook_versions(
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS current_notebook_id(
              id UUID NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS has_onboarded AS SELECT false AS has_onboarded"
        )

        for notebook in notebooks:
            notebook_id = _coerce_notebook_uuid(notebook["notebook_id"])
            versions = notebook["versions"]
            created = notebook.get("updated_at") or versions[0]["created_at"]

            connection.execute(
                "INSERT INTO notebooks(id, name, created) VALUES (?, ?, ?)",
                [str(notebook_id), notebook["name"], created],
            )

            last_version_index = len(versions)
            for index, version_spec in enumerate(versions, start=1):
                version_id = version_spec["version_id"]
                version = _coerce_version_number(version_id, index)
                expires = None
                if index != last_version_index:
                    expires = _NON_CURRENT_EXPIRES

                connection.execute(
                    """
                    INSERT INTO notebook_versions(
                      notebook_id,
                      version,
                      title,
                      json,
                      created,
                      expires
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(notebook_id),
                        version,
                        version_id,
                        _build_notebook_json(version, version_spec["cells"]),
                        version_spec["created_at"],
                        expires,
                    ],
                )

    return ui_db_path
