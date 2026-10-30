"""Pytest configuration and shared fixtures.

Tests import from the installed ``hermes_a365`` package — run
``pip install -e .[dev]`` (or ``uv sync --all-extras``) before
invoking pytest. Wires up ``--update-golden`` for regenerating
golden fixtures.
"""

from __future__ import annotations

import logging

import pytest

pytest_plugins = ["test_activity_bridge"]


@pytest.fixture(autouse=True)
def _reset_hermes_a365_logger_level() -> None:
    """Keep ``register()`` from pinning ``hermes_a365`` across shuffled tests."""
    pkg = logging.getLogger("hermes_a365")
    pkg.setLevel(logging.NOTSET)
    yield
    pkg.setLevel(logging.NOTSET)


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden fixtures from current rendering output",
    )


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """Give every test a fresh ``$HOME`` / ``$HERMES_HOME`` and cwd (#118).

    Many adapter tests build ``Agent365Adapter`` with the *default*
    conversations path, which resolves to
    ``Path.home()/.hermes/agents/<slug>/conversations.json``. Without
    isolation those tests read and write the developer's real
    ``~/.hermes`` and leak registry state across tests — so a shuffled
    run (``pytest-randomly``) makes registry-count assertions like
    ``TestPruneConversations`` order-dependent and flaky. A per-test tmp
    home makes every test hermetic and stops the suite touching the real
    home dir, including the operator's ``~/.hermes/.env``.

    The working directory is isolated for the same reason. Status, doctor,
    verify, and the adapter's generated-config default read
    ``./a365.bot-service.config.json`` and ``./a365.generated.config.json``.
    A checkout that contains those files (a live-walk machine) would
    otherwise change ``tests/test_status.py::test_agent_scope_adds_activity_bridge``
    from skipped Path B to a real sidecar probe. Tests that set
    ``HOME``/``HERMES_HOME`` or ``chdir`` themselves still win (autouse
    runs first).
    """
    home = tmp_path_factory.mktemp("home")
    work = tmp_path_factory.mktemp("cwd")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.chdir(work)


@pytest.fixture(autouse=True)
def _isolate_secrets_provider():  # type: ignore[no-untyped-def]
    """Pin a null secrets provider for every test (#19).

    ``secrets_provider`` installs a live OS-keychain provider at import
    time, so any test that exercises a wired secret read would otherwise
    shell out to the *developer's real keychain* (``security`` /
    ``secret-tool``) — non-hermetic, slow, and able to make a passing
    suite depend on whatever happens to be stored on that machine. Same
    motivation as ``_isolate_hermes_home`` above.

    Tests that want provider behaviour pass ``provider=`` explicitly or
    call ``set_default_provider`` themselves; this fixture restores the
    original afterwards either way, including on exceptions.
    """
    from hermes_a365.secrets_provider import default_provider, set_default_provider

    class _NullProvider:
        name = "null-test-provider"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return None

    original = default_provider()
    set_default_provider(_NullProvider())
    try:
        # Yielding the captured value lets a test assert what the SHIPPED
        # import-time default is, which this fixture would otherwise hide.
        yield original
    finally:
        set_default_provider(original)
