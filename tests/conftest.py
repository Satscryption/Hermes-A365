"""Pytest configuration and shared fixtures.

Tests import from the installed ``hermes_a365`` package — run
``pip install -e .[dev]`` (or ``uv sync --all-extras``) before
invoking pytest. Wires up ``--update-golden`` for regenerating
golden fixtures.
"""

from __future__ import annotations


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden fixtures from current rendering output",
    )
