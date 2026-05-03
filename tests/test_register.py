"""Tests for scripts/register.py.

Every test uses an in-memory ``FakeMutator`` and ``FakeKeychain`` plus a
``tmp_path`` for ``HERMES_HOME``; nothing here ever calls the real ``a365``
CLI or touches the OS keychain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from register import (
    AADSTSError,
    ApplyResult,
    RegisterError,
    RegisterInputs,
    RegisterPlan,
    apply_register_plan,
    build_register_plan,
    write_env_atomic,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    """Minimal QuerySource — only ``query_app_by_name`` matters for register."""

    available: bool = True
    apps_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return self.apps_by_name.get(name)

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return None

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


@dataclass
class FakeMutator:
    """Records every call; returns scripted responses or raises scripted errors.

    Set ``setup_app_responses`` keyed by tier (or by call sequence using
    ``setup_app_sequence``) and ``setup_app_errors`` to inject AADSTS errors.
    """

    available: bool = True
    setup_app_responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    setup_app_errors: list[Exception] = field(default_factory=list)
    fic_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:
        self.calls.append(("setup_app", {"tier": tier, "name": name}))
        if self.setup_app_errors:
            err = self.setup_app_errors.pop(0)
            raise err
        return self.setup_app_responses[tier]

    def fic_configure(self, *, app_id: str) -> None:
        self.calls.append(("fic_configure", {"app_id": app_id}))
        if self.fic_error is not None:
            err, self.fic_error = self.fic_error, None
            raise err


@dataclass
class FakeKeychain:
    """In-memory KeychainBackend; records writes."""

    name: str = "fake"
    store_calls: list[tuple[str, str]] = field(default_factory=list)
    items: dict[str, str] = field(default_factory=dict)

    def store(self, account: str, secret: str) -> None:
        self.store_calls.append((account, secret))
        self.items[account] = secret

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


# Recorded sleep calls — used by retry tests.
class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# RegisterInputs validation
# ---------------------------------------------------------------------------


class TestRegisterInputs:
    def test_valid(self) -> None:
        inp = RegisterInputs(app_name="Hermes Inbox", tenant_id="contoso.onmicrosoft.com")
        assert inp.t1_name == "Hermes Inbox"
        assert inp.t2_name == "Hermes Inbox-conf"

    def test_empty_app_name(self) -> None:
        with pytest.raises(ValueError, match="app_name"):
            RegisterInputs(app_name="", tenant_id="contoso.onmicrosoft.com")

    def test_empty_tenant(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            RegisterInputs(app_name="X", tenant_id="")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _inputs() -> RegisterInputs:
    return RegisterInputs(
        app_name="Hermes Inbox",
        tenant_id="contoso.onmicrosoft.com",
        cli_variant="a365-dotnet",
    )


class TestBuildRegisterPlan:
    def test_create_when_neither_app_exists(self) -> None:
        plan = build_register_plan(_inputs(), query_source=FakeQuerySource())
        assert plan.t1.action == "create"
        assert plan.t2.action == "create"
        assert plan.has_abort is False
        assert plan.is_noop is False

    def test_noop_when_both_apps_match(self) -> None:
        qs = FakeQuerySource(
            apps_by_name={
                "Hermes Inbox": {
                    "appId": "t1id",
                    "displayName": "Hermes Inbox",
                    "tier": 1,
                    "isMultiTenant": True,
                },
                "Hermes Inbox-conf": {
                    "appId": "t2id",
                    "displayName": "Hermes Inbox-conf",
                    "tier": 2,
                    "isMultiTenant": False,
                    "ficConfigured": True,
                },
            },
        )
        plan = build_register_plan(_inputs(), query_source=qs)
        assert plan.t1.action == "noop"
        assert plan.t2.action == "noop"
        assert plan.is_noop is True
        assert plan.t1_existing_app_id == "t1id"
        assert plan.t2_existing_app_id == "t2id"

    def test_patch_when_t2_fic_missing(self) -> None:
        qs = FakeQuerySource(
            apps_by_name={
                "Hermes Inbox-conf": {
                    "appId": "t2id",
                    "displayName": "Hermes Inbox-conf",
                    "tier": 2,
                    "isMultiTenant": False,
                    "ficConfigured": False,
                },
            },
        )
        plan = build_register_plan(_inputs(), query_source=qs)
        assert plan.t2.action == "patch"
        assert plan.t2.diff == {"fic_configured": (False, True)}

    def test_abort_on_t1_tier_mismatch(self) -> None:
        # Existing T1-named app is actually tier 2 — refuse to mutate.
        qs = FakeQuerySource(
            apps_by_name={
                "Hermes Inbox": {
                    "appId": "wrong",
                    "displayName": "Hermes Inbox",
                    "tier": 2,
                    "isMultiTenant": True,
                },
            },
        )
        plan = build_register_plan(_inputs(), query_source=qs)
        assert plan.t1.action == "abort"
        assert plan.has_abort is True

    def test_unavailable_query_source_assumes_create(self) -> None:
        plan = build_register_plan(_inputs(), query_source=FakeQuerySource(available=False))
        assert plan.t1.action == "create"
        assert plan.t2.action == "create"


class TestPlanRender:
    def test_human_create_path(self) -> None:
        plan = build_register_plan(_inputs(), query_source=FakeQuerySource())
        text = plan.render_human()
        assert "[plan] hermes a365 register" in text
        assert "T1 first-party" in text
        assert "would create" in text
        assert "OS keychain" in text

    def test_human_abort_omits_followups(self) -> None:
        qs = FakeQuerySource(
            apps_by_name={
                "Hermes Inbox": {
                    "appId": "wrong",
                    "displayName": "Hermes Inbox",
                    "tier": 2,
                    "isMultiTenant": True,
                },
            },
        )
        plan = build_register_plan(_inputs(), query_source=qs)
        text = plan.render_human()
        assert "abort" in text.lower()
        # Followup lines should not appear when aborting.
        assert "OS keychain" not in text


# ---------------------------------------------------------------------------
# write_env_atomic
# ---------------------------------------------------------------------------


class TestWriteEnvAtomic:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / ".env"
        merged = write_env_atomic(target, {"A": "1", "B": "2"})
        assert merged == {"A": "1", "B": "2"}
        assert target.read_text() == "A=1\nB=2\n"

    def test_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        target.write_text("EXISTING=keep\nOTHER=also\n")
        merged = write_env_atomic(target, {"A365_APP_ID": "xyz"})
        assert merged == {"EXISTING": "keep", "OTHER": "also", "A365_APP_ID": "xyz"}
        # Output is sorted, deterministic.
        assert target.read_text() == "A365_APP_ID=xyz\nEXISTING=keep\nOTHER=also\n"

    def test_overwrites_same_key(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        target.write_text("A=old\n")
        write_env_atomic(target, {"A": "new"})
        assert target.read_text() == "A=new\n"

    def test_no_partial_write_artifacts(self, tmp_path: Path) -> None:
        # After a successful write, the .tmp sibling must not exist.
        target = tmp_path / ".env"
        write_env_atomic(target, {"A": "1"})
        assert not (tmp_path / ".env.tmp").exists()


# ---------------------------------------------------------------------------
# apply_register_plan
# ---------------------------------------------------------------------------


def _create_create_plan() -> RegisterPlan:
    """Both apps absent → create+create plan."""
    return build_register_plan(_inputs(), query_source=FakeQuerySource())


def _noop_plan(t1_id: str = "t1id", t2_id: str = "t2id") -> RegisterPlan:
    qs = FakeQuerySource(
        apps_by_name={
            "Hermes Inbox": {
                "appId": t1_id,
                "displayName": "Hermes Inbox",
                "tier": 1,
                "isMultiTenant": True,
            },
            "Hermes Inbox-conf": {
                "appId": t2_id,
                "displayName": "Hermes Inbox-conf",
                "tier": 2,
                "isMultiTenant": False,
                "ficConfigured": True,
            },
        },
    )
    return build_register_plan(_inputs(), query_source=qs)


class TestApplyHappyPath:
    def test_create_create_invokes_setup_for_both_tiers(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1-aaa", "secret": None},
                2: {"appId": "t2-bbb", "secret": "shh"},
            },
        )
        keychain = FakeKeychain()
        result = apply_register_plan(
            plan,
            mutator=mutator,
            keychain=keychain,
            hermes_home=tmp_path,
        )
        assert isinstance(result, ApplyResult)
        assert result.t1_app_id == "t1-aaa"
        assert result.t2_app_id == "t2-bbb"
        assert result.t2_secret_stored is True
        assert result.fic_configured is True
        assert result.consent_deferred is False
        # Mutator was called for T1, T2, and FIC — order matters.
        assert [c[0] for c in mutator.calls] == ["setup_app", "setup_app", "fic_configure"]
        assert mutator.calls[0][1] == {"tier": 1, "name": "Hermes Inbox"}
        assert mutator.calls[1][1] == {"tier": 2, "name": "Hermes Inbox-conf"}
        assert mutator.calls[2][1] == {"app_id": "t2-bbb"}

    def test_secret_stored_under_tenant_appid(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1", "secret": None},
                2: {"appId": "t2", "secret": "topsecret"},
            },
        )
        keychain = FakeKeychain()
        apply_register_plan(plan, mutator=mutator, keychain=keychain, hermes_home=tmp_path)
        assert keychain.store_calls == [("contoso.onmicrosoft.com.t2", "topsecret")]

    def test_env_file_written_with_required_keys(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1", "secret": None},
                2: {"appId": "t2", "secret": "s"},
            },
        )
        result = apply_register_plan(
            plan, mutator=mutator, keychain=FakeKeychain(), hermes_home=tmp_path
        )
        env_path = tmp_path / ".env"
        assert env_path.exists()
        text = env_path.read_text()
        assert "A365_TENANT_ID=contoso.onmicrosoft.com" in text
        assert "A365_APP_ID=t2" in text
        assert "A365_CLI_VARIANT=a365-dotnet" in text
        assert result.env_written["A365_APP_ID"] == "t2"


class TestApplyIdempotency:
    def test_noop_plan_does_not_call_mutator(self, tmp_path: Path) -> None:
        plan = _noop_plan()
        mutator = FakeMutator()
        keychain = FakeKeychain()
        result = apply_register_plan(plan, mutator=mutator, keychain=keychain, hermes_home=tmp_path)
        assert mutator.calls == []
        assert keychain.store_calls == []
        assert result.t1_app_id == "t1id"
        assert result.t2_app_id == "t2id"
        assert result.t2_secret_stored is False
        # FIC was already configured per the actual state — apply records that.
        assert result.fic_configured is False
        # Env file is still rewritten so a re-run after manual edit converges.
        assert (tmp_path / ".env").exists()


class TestApplyAbort:
    def test_refuses_when_plan_aborts(self, tmp_path: Path) -> None:
        qs = FakeQuerySource(
            apps_by_name={
                "Hermes Inbox": {
                    "appId": "wrong",
                    "displayName": "Hermes Inbox",
                    "tier": 2,  # tier mismatch with desired tier 1
                    "isMultiTenant": True,
                },
            },
        )
        plan = build_register_plan(_inputs(), query_source=qs)
        with pytest.raises(RegisterError, match="refusing to apply"):
            apply_register_plan(
                plan,
                mutator=FakeMutator(),
                keychain=FakeKeychain(),
                hermes_home=tmp_path,
            )


class TestApplyAADSTS500011Retry:
    def test_retries_until_success(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1", "secret": None},
                2: {"appId": "t2", "secret": "s"},
            },
            # First two attempts on T1 fail with license-not-propagated, third succeeds.
            setup_app_errors=[
                AADSTSError("AADSTS500011", "license"),
                AADSTSError("AADSTS500011", "license"),
            ],
        )
        sleeper = _SleepRecorder()
        result = apply_register_plan(
            plan,
            mutator=mutator,
            keychain=FakeKeychain(),
            hermes_home=tmp_path,
            retries=3,
            backoff=30.0,
            sleep_fn=sleeper,
        )
        assert result.t1_app_id == "t1"
        # Two backoffs between three attempts.
        assert sleeper.calls == [30.0, 30.0]

    def test_raises_after_retry_exhausted(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_errors=[AADSTSError("AADSTS500011", "license") for _ in range(4)],
        )
        sleeper = _SleepRecorder()
        with pytest.raises(AADSTSError) as excinfo:
            apply_register_plan(
                plan,
                mutator=mutator,
                keychain=FakeKeychain(),
                hermes_home=tmp_path,
                retries=3,
                backoff=30.0,
                sleep_fn=sleeper,
            )
        assert excinfo.value.code == "AADSTS500011"
        # 3 sleeps (between attempts 1→2, 2→3, 3→4); attempt 4 raises.
        assert sleeper.calls == [30.0, 30.0, 30.0]

    def test_other_aadsts_codes_not_retried(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_errors=[AADSTSError("AADSTS65001", "permission denied")],
        )
        sleeper = _SleepRecorder()
        with pytest.raises(AADSTSError) as excinfo:
            apply_register_plan(
                plan,
                mutator=mutator,
                keychain=FakeKeychain(),
                hermes_home=tmp_path,
                retries=3,
                backoff=30.0,
                sleep_fn=sleeper,
            )
        assert excinfo.value.code == "AADSTS65001"
        assert sleeper.calls == []


class TestApplyConsentDeferred:
    def test_aadsts90094_on_fic_configure_does_not_fail(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1", "secret": None},
                2: {"appId": "t2", "secret": "s"},
            },
            fic_error=AADSTSError("AADSTS90094", "admin consent required"),
        )
        result = apply_register_plan(
            plan,
            mutator=mutator,
            keychain=FakeKeychain(),
            hermes_home=tmp_path,
        )
        assert result.consent_deferred is True
        assert result.fic_configured is False
        # Apps were still created; the run is recoverable via `hermes a365 consent`.
        assert result.t1_app_id == "t1"
        assert result.t2_app_id == "t2"


class TestApplyMutatorRaisesNonAADSTS:
    def test_propagates_arbitrary_runtime_error(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_errors=[RuntimeError("network down")],
        )
        with pytest.raises(RuntimeError, match="network down"):
            apply_register_plan(
                plan,
                mutator=mutator,
                keychain=FakeKeychain(),
                hermes_home=tmp_path,
            )


class TestEmptyAppIdResponses:
    def test_missing_appid_in_response_raises(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "", "secret": None},
            },
        )
        with pytest.raises(RegisterError, match="T1 setup app returned no appId"):
            apply_register_plan(
                plan, mutator=mutator, keychain=FakeKeychain(), hermes_home=tmp_path
            )

    def test_missing_secret_for_t2_raises(self, tmp_path: Path) -> None:
        plan = _create_create_plan()
        mutator = FakeMutator(
            setup_app_responses={
                1: {"appId": "t1", "secret": None},
                2: {"appId": "t2", "secret": None},
            },
        )
        with pytest.raises(RegisterError, match="T2 setup app returned no client secret"):
            apply_register_plan(
                plan, mutator=mutator, keychain=FakeKeychain(), hermes_home=tmp_path
            )
