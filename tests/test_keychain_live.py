"""Optional live OS-keychain round-trips (#126).

Enabled only when ``HERMES_A365_LIVE_KEYCHAIN=1`` (CI macOS / Linux jobs).
Uses synthetic credentials and always deletes them.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from hermes_a365.keychain import delete_secret, get_secret, store_secret

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_A365_LIVE_KEYCHAIN") != "1",
    reason="set HERMES_A365_LIVE_KEYCHAIN=1 to exercise the real keychain",
)


@pytest.fixture()
def synthetic_ids() -> tuple[str, str]:
    return (f"ci-{uuid.uuid4().hex[:12]}", f"app-{uuid.uuid4().hex[:12]}")


@pytest.fixture()
def macos_store_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "darwin":
        monkeypatch.setenv("HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI", "1")


def test_live_keychain_round_trip(
    synthetic_ids: tuple[str, str], macos_store_opt_in: None
) -> None:
    tenant, app_id = synthetic_ids
    secret = f"ci-secret-{uuid.uuid4().hex}"
    try:
        store_secret(tenant, app_id, secret)
        assert get_secret(tenant, app_id) == secret
    finally:
        delete_secret(tenant, app_id)
    assert get_secret(tenant, app_id) is None
