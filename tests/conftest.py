"""Pytest configuration and shared fixtures.

Tests import from the installed ``hermes_a365`` package — run
``pip install -e .[dev]`` (or ``uv sync --all-extras``) before
invoking pytest. Wires up ``--update-golden`` for regenerating
golden fixtures.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden fixtures from current rendering output",
    )


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """Give every test a fresh, empty ``$HOME`` / ``$HERMES_HOME`` (#118).

    Many adapter tests build ``Agent365Adapter`` with the *default*
    conversations path, which resolves to
    ``Path.home()/.hermes/agents/<slug>/conversations.json``. Without
    isolation those tests read and write the developer's real
    ``~/.hermes`` and leak registry state across tests — so a shuffled
    run (``pytest-randomly``) makes registry-count assertions like
    ``TestPruneConversations`` order-dependent and flaky. A per-test tmp
    home makes every test hermetic and stops the suite touching the real
    home dir. Tests that set ``HOME``/``HERMES_HOME`` themselves, or pass
    an explicit path, still win (autouse runs first).
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
