"""Tests for scripts/fic_rotate.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fic_rotate import (
    FicRotateError,
    FicRotateResult,
    apply_fic_rotate,
    plan_fic_rotate,
)
from register import AADSTSError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
    available: bool = True
    fic_rotate_response: dict[str, Any] = field(default_factory=dict)
    fic_rotate_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def fic_rotate(self, *, app_id: str) -> dict[str, Any]:
        self.calls.append(("fic_rotate", {"app_id": app_id}))
        if self.fic_rotate_error is not None:
            err, self.fic_rotate_error = self.fic_rotate_error, None
            raise err
        return self.fic_rotate_response

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def create_instance(  # pragma: no cover
        self, *, blueprint_slug: str, instance_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def deploy(  # pragma: no cover
        self, *, instance_id: str, channels: list[str]
    ) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class FakeKeychain:
    name: str = "fake"
    items: dict[str, str] = field(default_factory=dict)
    store_calls: list[tuple[str, str]] = field(default_factory=list)

    def store(self, account: str, secret: str) -> None:
        self.store_calls.append((account, secret))
        self.items[account] = secret

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TENANT = "contoso.onmicrosoft.com"
_APP_ID = "9e2d1f73-3c5b-49a1-bf2d-77a812f5c4e0"


def _seed_skill_env(tmp_path: Path, **overrides: str) -> None:
    base = {
        "A365_TENANT_ID": _TENANT,
        "A365_APP_ID": _APP_ID,
        "A365_CLI_VARIANT": "a365-dotnet",
    }
    base.update(overrides)
    text = "".join(f"{k}={v}\n" for k, v in sorted(base.items()))
    (tmp_path / ".env").write_text(text)


# ---------------------------------------------------------------------------
# plan_fic_rotate
# ---------------------------------------------------------------------------


class TestPlan:
    def test_returns_tenant_and_app_id(self, tmp_path: Path) -> None:
        _seed_skill_env(tmp_path)
        tenant, app_id = plan_fic_rotate(hermes_home=tmp_path)
        assert tenant == _TENANT
        assert app_id == _APP_ID

    def test_missing_skill_env_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(FicRotateError, match="run `hermes a365 register`"):
            plan_fic_rotate(hermes_home=tmp_path)

    def test_missing_required_keys_fails_clean(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("OTHER=x\n")
        with pytest.raises(FicRotateError, match="missing required keys"):
            plan_fic_rotate(hermes_home=tmp_path)


# ---------------------------------------------------------------------------
# apply_fic_rotate
# ---------------------------------------------------------------------------


class TestApply:
    def test_calls_mutator_with_app_id_and_stores_secret(self) -> None:
        mutator = FakeMutator(fic_rotate_response={"secret": "new-shh", "expires": "2026-08-04"})
        keychain = FakeKeychain()
        result = apply_fic_rotate(
            tenant_id=_TENANT, app_id=_APP_ID, mutator=mutator, keychain=keychain
        )
        assert isinstance(result, FicRotateResult)
        assert result.rotated is True
        assert result.new_secret_stored is True
        assert result.expires == "2026-08-04"
        assert mutator.calls == [("fic_rotate", {"app_id": _APP_ID})]
        # Secret was stored under <tenant>.<appId>.
        assert keychain.store_calls == [(f"{_TENANT}.{_APP_ID}", "new-shh")]

    def test_response_without_secret_raises(self) -> None:
        mutator = FakeMutator(fic_rotate_response={"expires": "2026-08-04"})
        keychain = FakeKeychain()
        with pytest.raises(FicRotateError, match="no secret"):
            apply_fic_rotate(
                tenant_id=_TENANT,
                app_id=_APP_ID,
                mutator=mutator,
                keychain=keychain,
            )
        # Keychain must NOT have been touched.
        assert keychain.store_calls == []

    def test_aadsts_error_propagates_without_storing(self) -> None:
        mutator = FakeMutator(
            fic_rotate_error=AADSTSError("AADSTS70043", "refresh expired"),
        )
        keychain = FakeKeychain()
        with pytest.raises(AADSTSError) as excinfo:
            apply_fic_rotate(
                tenant_id=_TENANT,
                app_id=_APP_ID,
                mutator=mutator,
                keychain=keychain,
            )
        assert excinfo.value.code == "AADSTS70043"
        assert keychain.store_calls == []

    def test_messages_include_restart_reminder(self) -> None:
        mutator = FakeMutator(fic_rotate_response={"secret": "s"})
        keychain = FakeKeychain()
        result = apply_fic_rotate(
            tenant_id=_TENANT, app_id=_APP_ID, mutator=mutator, keychain=keychain
        )
        assert any("activity bridge" in m for m in result.messages)
        assert any("OS keychain" in m for m in result.messages)

    def test_clientSecret_alias_is_accepted(self) -> None:
        # Some CLI variants may emit the field as ``clientSecret`` rather than
        # ``secret``. Accept both.
        mutator = FakeMutator(fic_rotate_response={"clientSecret": "alt"})
        keychain = FakeKeychain()
        result = apply_fic_rotate(
            tenant_id=_TENANT, app_id=_APP_ID, mutator=mutator, keychain=keychain
        )
        assert result.new_secret_stored is True
        assert keychain.items[f"{_TENANT}.{_APP_ID}"] == "alt"
