"""Tests for hermes_a365.secrets_provider — the #19 secrets-at-rest seam."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

from hermes_a365.keychain import KeychainError, account_name
from hermes_a365.secrets_provider import (
    KeychainSecretsProvider,
    SecretsProvider,
    resolve_secret,
    set_default_provider,
)

TENANT = "22222222-2222-2222-2222-222222222222"
BLUEPRINT_APP = "11111111-1111-1111-1111-111111111111"
BF_APP = "33333333-3333-3333-3333-333333333333"


@dataclass
class FakeBackend:
    """Mirrors tests/test_keychain.py's FakeBackend contract."""

    name: str = "fake"
    items: dict[str, str] = field(default_factory=dict)
    raise_on_get: Exception | None = None

    def store(self, account: str, secret: str) -> None:
        self.items[account] = secret

    def get(self, account: str) -> str | None:
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.items.get(account)

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


# ---------------------------------------------------------------------------
# Precedence: the existing plaintext source ALWAYS wins
# ---------------------------------------------------------------------------


def test_existing_value_wins_over_provider() -> None:
    # #19 core contract: wiring the provider in must not change which
    # credential a running deployment uses. The keychain holds a DIFFERENT
    # value; the plaintext one still wins.
    backend = FakeBackend(items={account_name(TENANT, BLUEPRINT_APP): "keychain-secret"})
    provider = KeychainSecretsProvider(backend=backend)

    got = resolve_secret(
        TENANT, BLUEPRINT_APP, existing="plaintext-secret", provider=provider
    )

    assert got == "plaintext-secret"


def test_provider_fills_a_miss() -> None:
    backend = FakeBackend(items={account_name(TENANT, BLUEPRINT_APP): "keychain-secret"})
    provider = KeychainSecretsProvider(backend=backend)

    assert resolve_secret(TENANT, BLUEPRINT_APP, existing="", provider=provider) == (
        "keychain-secret"
    )
    assert resolve_secret(TENANT, BLUEPRINT_APP, existing=None, provider=provider) == (
        "keychain-secret"
    )


def test_rotation_safety_stale_keychain_never_beats_fresh_plaintext() -> None:
    # `register --apply` rotates the blueprint secret into the generated
    # config. A stale keychain entry must not silently defeat that.
    backend = FakeBackend(items={account_name(TENANT, BLUEPRINT_APP): "OLD-rotated-out"})
    provider = KeychainSecretsProvider(backend=backend)

    assert resolve_secret(
        TENANT, BLUEPRINT_APP, existing="NEW-rotated-in", provider=provider
    ) == "NEW-rotated-in"


def test_both_secrets_are_keyed_independently() -> None:
    # Path A and Path B are distinct (tenant, app_id) keys — one provider
    # serves both without a schema change.
    backend = FakeBackend(
        items={
            account_name(TENANT, BLUEPRINT_APP): "blueprint-secret",
            account_name(TENANT, BF_APP): "bf-secret",
        }
    )
    provider = KeychainSecretsProvider(backend=backend)

    assert resolve_secret(TENANT, BLUEPRINT_APP, existing="", provider=provider) == (
        "blueprint-secret"
    )
    assert resolve_secret(TENANT, BF_APP, existing="", provider=provider) == "bf-secret"


def test_absent_from_both_tiers_returns_empty_string() -> None:
    provider = KeychainSecretsProvider(backend=FakeBackend())
    assert resolve_secret(TENANT, BF_APP, existing="", provider=provider) == ""


# ---------------------------------------------------------------------------
# Degradation: an unusable key or backend is a MISS, never a crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tenant", "app_id"),
    [("", BF_APP), (TENANT, ""), ("", "")],
)
def test_empty_key_is_a_miss_not_a_crash(tenant: str, app_id: str) -> None:
    # Path-A-only operators legitimately run with bf_app_id="". account_name
    # raises ValueError on an empty component, so the seam must degrade.
    backend = FakeBackend(items={account_name(TENANT, BF_APP): "bf-secret"})
    provider = KeychainSecretsProvider(backend=backend)

    assert resolve_secret(tenant, app_id, existing="", provider=provider) == ""


def test_empty_key_still_returns_the_existing_value() -> None:
    provider = KeychainSecretsProvider(backend=FakeBackend())
    assert resolve_secret("", "", existing="from-env", provider=provider) == "from-env"


def test_unavailable_backend_degrades_to_miss() -> None:
    # No `security`/`secret-tool` binary, or an unsupported platform:
    # keychain.get_secret raises KeychainError. That is a miss, not a
    # failure — the caller keeps whatever plaintext value it had.
    provider = KeychainSecretsProvider(
        backend=FakeBackend(raise_on_get=KeychainError("no backend on PATH"))
    )

    assert resolve_secret(TENANT, BF_APP, existing="", provider=provider) == ""
    assert resolve_secret(TENANT, BF_APP, existing="env", provider=provider) == "env"


def test_provider_that_raises_does_not_break_the_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Third-party pluggable providers must never take down a runtime
    # credential read; the failure is logged (visibly) and we fall back.
    class ExplodingProvider:
        name = "exploding"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            raise RuntimeError("vault unreachable")

    caplog.set_level(logging.WARNING)
    assert resolve_secret(TENANT, BF_APP, existing="", provider=ExplodingProvider()) == ""
    assert "exploding" in caplog.text


# ---------------------------------------------------------------------------
# #5d — no secret value may reach logs
# ---------------------------------------------------------------------------


def test_no_secret_value_is_logged_on_any_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sup3r-s3cret-value"
    caplog.set_level(logging.DEBUG)

    hit = KeychainSecretsProvider(
        backend=FakeBackend(items={account_name(TENANT, BF_APP): secret})
    )
    assert resolve_secret(TENANT, BF_APP, existing="", provider=hit) == secret

    broken = KeychainSecretsProvider(backend=FakeBackend(raise_on_get=KeychainError("x")))
    resolve_secret(TENANT, BF_APP, existing="", provider=broken)
    resolve_secret("", "", existing="", provider=broken)

    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Pluggability seam
# ---------------------------------------------------------------------------


def test_shipped_default_provider_is_keychain_backed(
    _isolate_secrets_provider,
) -> None:
    # conftest pins a null provider suite-wide for hermeticity, so assert on
    # the import-time default it captured rather than the live global.
    shipped = _isolate_secrets_provider
    assert isinstance(shipped, KeychainSecretsProvider)
    assert shipped.name == "os-keychain"


def test_set_default_provider_swaps_the_backend() -> None:
    class Vaultish:
        name = "vaultish"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return "from-vault" if app_id == BF_APP else None

    set_default_provider(Vaultish())
    # No explicit provider= — the call site picks up the swapped default.
    assert resolve_secret(TENANT, BF_APP, existing="") == "from-vault"
    assert resolve_secret(TENANT, BLUEPRINT_APP, existing="") == ""


def test_custom_provider_satisfies_the_protocol() -> None:
    class Minimal:
        name = "minimal"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return None

    assert isinstance(Minimal(), SecretsProvider)
    assert isinstance(KeychainSecretsProvider(), SecretsProvider)


# ---------------------------------------------------------------------------
# Coverage the round-1 red-team found missing
#
# This one locks behaviour that was already correct but untested — it passes
# against the pre-fix tree too. It is not a regression lock; don't read it as
# guarding a bug fix.
# ---------------------------------------------------------------------------


def test_provider_value_error_branch_is_reached() -> None:
    # Round-1 finding: the ValueError branch in KeychainSecretsProvider was
    # never exercised — resolve_secret short-circuits on an EMPTY key before
    # the provider is called, so only a non-empty-but-invalid key (a "/",
    # which account_name rejects) reaches it. Test the provider directly.
    provider = KeychainSecretsProvider(backend=FakeBackend())
    assert provider.resolve("con/toso", BF_APP) is None


# ---------------------------------------------------------------------------
# #19 red-team round-1 regressions — each of these FAILS against eefcbae
# ---------------------------------------------------------------------------


def test_keychain_timeout_degrades_to_miss(caplog: pytest.LogCaptureFixture) -> None:
    # A hung `security`/`secret-tool` (locked keychain prompt, stuck D-Bus)
    # must not stall a credential read on the gateway's event loop.
    from subprocess import TimeoutExpired

    caplog.set_level(logging.WARNING)
    provider = KeychainSecretsProvider(
        backend=FakeBackend(raise_on_get=TimeoutExpired(cmd="security", timeout=10.0))
    )

    assert resolve_secret(TENANT, BF_APP, existing="", provider=provider) == ""
    assert resolve_secret(TENANT, BF_APP, existing="env", provider=provider) == "env"
    assert "timed out" in caplog.text


@pytest.mark.parametrize("bad", [b"bytes-secret", 12345, ["list"], {"k": "v"}])
def test_non_string_provider_return_is_refused(
    bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    # Pluggable boundary: a provider returning bytes/int must not have that
    # value carried into an OAuth POST body. Refuse rather than guess.
    class BadProvider:
        name = "bad"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return bad  # type: ignore[return-value]

    caplog.set_level(logging.WARNING)
    assert resolve_secret(TENANT, BF_APP, existing="", provider=BadProvider()) == ""
    assert "not a str" in caplog.text


_ABSENT = object()  # sentinel: don't set `name` at all


@pytest.mark.parametrize(
    "bad_name", [_ABSENT, None, "", 12345, object()], ids=lambda v: type(v).__name__
)
def test_provider_label_never_raises_on_a_missing_or_odd_name(
    bad_name: object,
) -> None:
    # Round-3 finding: `verify` interpolated `default_provider().name`
    # unguarded, so a provider written to the interface the README documented
    # (`resolve` only) crashed the bridge preflight with AttributeError. A
    # label is for humans — it must never be the thing that raises on a
    # credential path. Round 4: the sentinel distinguishes "no attribute" from
    # "name is None"; the earlier version silently skipped the assignment for
    # None and so never exercised that case at all.
    from hermes_a365.secrets_provider import provider_label

    class Custom:
        def resolve(self, tenant: str, app_id: str) -> str | None:
            return None

    p = Custom()
    if bad_name is not _ABSENT:
        p.name = bad_name  # type: ignore[attr-defined]

    assert provider_label(p) == "Custom"


def test_provider_label_survives_a_name_that_raises() -> None:
    # Round-4 finding: `getattr(active, "name", None)` swallows only
    # AttributeError, so a `name` implemented as a property doing a lazy
    # backend handshake could raise straight through the helper added to
    # stop exactly this — taking down `verify` and the setup wizard.
    from hermes_a365.secrets_provider import provider_label, resolve_secret

    class LazyName:
        @property
        def name(self) -> str:
            raise RuntimeError("backend handshake failed")

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return "still-works"

    p = LazyName()
    assert provider_label(p) == "LazyName"
    # And the credential read itself completes — the label was never
    # load-bearing.
    assert resolve_secret(TENANT, BF_APP, existing="", provider=p) == "still-works"


def test_provider_label_prefers_a_real_name() -> None:
    from hermes_a365.secrets_provider import provider_label

    assert provider_label(KeychainSecretsProvider()) == "os-keychain"


# ---------------------------------------------------------------------------
# probe_provider — diagnostics need "empty" vs "unreachable"
# ---------------------------------------------------------------------------


def test_probe_reports_a_hit() -> None:
    from hermes_a365.secrets_provider import probe_provider

    backend = FakeBackend(items={account_name(TENANT, BF_APP): "stored"})
    got = probe_provider(TENANT, BF_APP, provider=KeychainSecretsProvider(backend))

    assert got == (True, True, "os-keychain")


def test_probe_distinguishes_an_empty_store_from_an_unreachable_one() -> None:
    # Round-5 finding: the wizard collapsed every provider outcome to a bool,
    # so a LOCKED keychain read as an EMPTY one — and the operator was sent to
    # `register --apply --auto-recover-secret`, minting a secret that then
    # outranks the one they had stored all along.
    from hermes_a365.secrets_provider import probe_provider

    empty = KeychainSecretsProvider(FakeBackend())
    assert probe_provider(TENANT, BF_APP, provider=empty) == (
        False,
        True,  # the backend answered; "not stored" is trustworthy
        "os-keychain",
    )

    locked = KeychainSecretsProvider(
        FakeBackend(raise_on_get=KeychainError("User interaction is not allowed"))
    )
    assert probe_provider(TENANT, BF_APP, provider=locked) == (
        False,
        False,  # says nothing about whether a secret is stored
        "os-keychain",
    )


def test_probe_uses_the_target_failure_not_an_unrelated_sentinel() -> None:
    from hermes_a365.secrets_provider import probe_provider

    target = account_name(TENANT, BF_APP)

    class PerItemDenied(FakeBackend):
        def get(self, account: str) -> str | None:
            if account == target:
                raise KeychainError("User interaction is not allowed")
            return None

    provider = KeychainSecretsProvider(PerItemDenied())

    # A different key is readable, but that says nothing about whether the
    # target item is absent. The wizard must withhold re-mint advice.
    assert probe_provider(TENANT, BF_APP, provider=provider) == (
        False,
        False,
        "os-keychain",
    )


def test_probe_treats_a_timed_out_backend_as_unreachable() -> None:
    from subprocess import TimeoutExpired

    from hermes_a365.secrets_provider import probe_provider

    provider = KeychainSecretsProvider(
        FakeBackend(raise_on_get=TimeoutExpired(cmd="security", timeout=10.0))
    )

    assert probe_provider(TENANT, BF_APP, provider=provider)[:2] == (False, False)


def test_probe_treats_a_raising_third_party_provider_as_unreachable() -> None:
    from hermes_a365.secrets_provider import probe_provider

    class Exploding:
        name = "vaultish"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            raise RuntimeError("vault unreachable")

    assert probe_provider(TENANT, BF_APP, provider=Exploding()) == (
        False,
        False,
        "vaultish",
    )


def test_probe_treats_a_non_string_provider_result_as_unreachable() -> None:
    from hermes_a365.secrets_provider import probe_provider

    class Broken:
        name = "broken"

        def resolve(self, tenant: str, app_id: str) -> str | None:
            return b"not-a-string"  # type: ignore[return-value]

    assert probe_provider(TENANT, BF_APP, provider=Broken()) == (
        False,
        False,
        "broken",
    )


def test_probe_reports_an_empty_key_as_not_consulted() -> None:
    # Path-A-only operators have bf_app_id="". resolve_secret short-circuits,
    # so the store was never asked — that is not evidence it is empty.
    from hermes_a365.secrets_provider import probe_provider

    provider = KeychainSecretsProvider(FakeBackend())
    assert probe_provider(TENANT, "", provider=provider)[:2] == (False, False)


def test_probe_never_returns_the_secret() -> None:
    from hermes_a365.secrets_provider import probe_provider

    backend = FakeBackend(items={account_name(TENANT, BF_APP): "sup3r-s3cret"})
    got = probe_provider(TENANT, BF_APP, provider=KeychainSecretsProvider(backend))

    assert "sup3r-s3cret" not in repr(got)


def test_keychain_probe_never_returns_the_secret() -> None:
    backend = FakeBackend(items={account_name(TENANT, BF_APP): "sup3r-s3cret"})
    provider = KeychainSecretsProvider(backend)

    got = provider.probe(TENANT, BF_APP)

    assert got == (True, True)
    assert "sup3r-s3cret" not in repr(got)


def test_keychain_subprocesses_are_time_bounded() -> None:
    # Regression lock for the un-timed subprocess finding: every keychain
    # subprocess must carry a timeout now that it runs on a credential read.
    import inspect

    from hermes_a365 import keychain

    source = inspect.getsource(keychain)
    assert source.count("subprocess.run(") == source.count("timeout=")
