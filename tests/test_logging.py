"""Unit tests for shared structlog configuration.

Parameters
----------
None
    This module is imported by pytest.

Returns
-------
None
    Importing this module registers logging tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest
import structlog

from duckdb_ui_notebook_export import _logging


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None]:
    """Reset structlog global configuration and the module's cached state.

    Returns
    -------
    None
        The global structlog configuration and the module's ``_CONFIGURED``
        flag are reset before and after each test.

    Notes
    -----
    ``configure_logging`` caches whether it already ran. Tests that call it
    with different arguments (level, force) must not observe a stale
    configuration left behind by an earlier test or CLI call.
    """
    _logging.reset_for_testing()
    structlog.reset_defaults()
    yield
    _logging.reset_for_testing()
    structlog.reset_defaults()


def test_ut_l_001_should_use_colors_false_when_no_color_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-L-001: ``NO_COLOR`` set disables colors even on a tty stderr."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")

    assert _logging._should_use_colors() is False


def test_ut_l_002_should_use_colors_false_when_no_color_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-L-002: An empty-string ``NO_COLOR`` still counts as set.

    Notes
    -----
    Per no-color.org, the presence of the ``NO_COLOR`` variable disables
    color, regardless of its value.
    """
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "")

    assert _logging._should_use_colors() is False


def test_ut_l_003_should_use_colors_false_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-L-003: Non-tty stderr disables colors even without ``NO_COLOR``."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    assert _logging._should_use_colors() is False


def test_ut_l_004_should_use_colors_true_on_tty_without_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-L-004: A tty stderr without ``NO_COLOR`` enables colors."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    assert _logging._should_use_colors() is True


def test_ut_l_005_configure_logging_disables_ansi_codes_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-L-005: No ANSI escape codes reach stderr when it is not a tty.

    Traceability
    ------------
    Issue #46
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    _logging.configure_logging()
    _logging.get_logger().warning("some_event", key="value")

    captured = capsys.readouterr()
    assert "\x1b[" not in captured.err
    assert "some_event" in captured.err


def test_ut_l_006_configure_logging_disables_ansi_codes_when_no_color_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-L-006: No ANSI escape codes reach stderr when ``NO_COLOR`` is set.

    Traceability
    ------------
    Issue #46
    """
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")

    _logging.configure_logging()
    _logging.get_logger().warning("some_event", key="value")

    captured = capsys.readouterr()
    assert "\x1b[" not in captured.err
    assert "some_event" in captured.err


def test_ut_l_007_direct_stderr_logger_honors_no_color(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-L-007: The direct stderr logger also honors ``NO_COLOR``/tty rules.

    Notes
    -----
    ``cli._direct_stderr_logger`` bypasses ``configure_logging`` but shares
    the global structlog processor configuration, so it must also avoid
    emitting ANSI codes under the same conditions.

    Traceability
    ------------
    Issue #46
    """
    from duckdb_ui_notebook_export.cli import _direct_stderr_logger

    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    _logging.configure_logging()

    _direct_stderr_logger().warning("direct_event", key="value")

    captured = capsys.readouterr()
    assert "\x1b[" not in captured.err
    assert "direct_event" in captured.err


def test_ut_l_008_configure_logging_force_updates_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-L-008: ``force=True`` lets a later call change the active level.

    Traceability
    ------------
    Issue #55
    """
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    _logging.configure_logging(level=logging.INFO)
    _logging.configure_logging(level=logging.ERROR, force=True)
    logger = _logging.get_logger()
    logger.warning("should_be_suppressed")
    logger.error("should_be_shown")

    captured = capsys.readouterr()
    assert "should_be_suppressed" not in captured.err
    assert "should_be_shown" in captured.err


def test_ut_l_009_configure_logging_without_force_stays_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT-L-009: Without ``force``, a later call keeps the first configuration."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    _logging.configure_logging(level=logging.ERROR)
    _logging.configure_logging(level=logging.INFO)
    logger = _logging.get_logger()
    logger.warning("should_stay_suppressed")

    captured = capsys.readouterr()
    assert "should_stay_suppressed" not in captured.err
