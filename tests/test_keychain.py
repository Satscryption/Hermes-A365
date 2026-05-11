"""Tests for hermes_a365.keychain (renamed from secrets.py in slice 19b
to avoid shadowing the stdlib ``secrets`` module on the pythonpath —
fastapi / starlette import ``from secrets import token_hex`` and were
picking up our module before the stdlib).

All tests use a ``FakeBackend`` or monkeypatched ``subprocess.run`` so the
real keychain is never touched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

import hermes_a365.keychain as secrets_mod
from hermes_a365.keychain import (
    SERVICE,
    KeychainBackend,
    KeychainError,
    LinuxBackend,
    MacOSBackend,
    account_name,
    delete_secret,
    get_backend,
    get_secret,
    main,
    store_secret,
)

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeBackend:
    name: str = "fake"
    data: dict[str, str] = field(default_factory=dict)
    store_calls: list[tuple[str, str]] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)

    def store(self, account: str, secret: str) -> None:
        self.store_calls.append((account, secret))
        self.data[account] = secret

    def get(self, account: str) -> str | None:
        self.get_calls.append(account)
        return self.data.get(account)

    def delete(self, account: str) -> bool:
        self.delete_calls.append(account)
        return self.data.pop(account, None) is not None


# Static check: FakeBackend satisfies the KeychainBackend Protocol.
_: KeychainBackend = FakeBackend()


# ---------------------------------------------------------------------------
# Account naming
# ---------------------------------------------------------------------------


class TestAccountName:
    def test_basic(self) -> None:
        assert (
            account_name("contoso.onmicrosoft.com", "abc-123") == "contoso.onmicrosoft.com.abc-123"
        )

    @pytest.mark.parametrize("tenant,app_id", [("", "x"), ("x", ""), ("", "")])
    def test_empty_rejected(self, tenant: str, app_id: str) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            account_name(tenant, app_id)

    def test_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain"):
            account_name("ten/ant", "app")
        with pytest.raises(ValueError, match="must not contain"):
            account_name("tenant", "ap/p")


# ---------------------------------------------------------------------------
# Public API with FakeBackend
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_round_trip(self) -> None:
        backend = FakeBackend()
        store_secret("contoso.onmicrosoft.com", "app-1", "shh", backend=backend)
        assert get_secret("contoso.onmicrosoft.com", "app-1", backend=backend) == "shh"

    def test_get_missing_returns_none(self) -> None:
        backend = FakeBackend()
        assert get_secret("t", "a", backend=backend) is None

    def test_delete_returns_true_when_exists(self) -> None:
        backend = FakeBackend()
        store_secret("t", "a", "shh", backend=backend)
        assert delete_secret("t", "a", backend=backend) is True
        assert get_secret("t", "a", backend=backend) is None

    def test_delete_returns_false_when_missing(self) -> None:
        backend = FakeBackend()
        assert delete_secret("t", "a", backend=backend) is False

    def test_store_rejects_empty_secret(self) -> None:
        backend = FakeBackend()
        with pytest.raises(ValueError, match="empty secret"):
            store_secret("t", "a", "", backend=backend)
        assert backend.store_calls == []

    def test_store_overwrites(self) -> None:
        backend = FakeBackend()
        store_secret("t", "a", "first", backend=backend)
        store_secret("t", "a", "second", backend=backend)
        assert get_secret("t", "a", backend=backend) == "second"


# ---------------------------------------------------------------------------
# macOS backend (mocked subprocess)
# ---------------------------------------------------------------------------


def _mock_completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestMacOSBackend:
    def test_store_invokes_security_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            recorded.append(argv)
            return _mock_completed(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        MacOSBackend().store("acct", "shh")
        assert recorded[0][:3] == ["security", "add-generic-password", "-U"]
        assert "-s" in recorded[0] and SERVICE in recorded[0]
        assert "-a" in recorded[0] and "acct" in recorded[0]
        assert "-w" in recorded[0] and "shh" in recorded[0]

    def test_store_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(1, stderr="boom"),
        )
        with pytest.raises(KeychainError, match="add-generic-password failed"):
            MacOSBackend().store("acct", "shh")

    def test_get_strips_trailing_newline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(0, stdout="shhh\n"),
        )
        assert MacOSBackend().get("acct") == "shhh"

    def test_get_not_found_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(44, stderr="not found"),
        )
        assert MacOSBackend().get("acct") is None

    def test_get_other_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(2, stderr="permission denied"),
        )
        with pytest.raises(KeychainError, match="find-generic-password failed"):
            MacOSBackend().get("acct")

    def test_delete_existed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _mock_completed(0))
        assert MacOSBackend().delete("acct") is True

    def test_delete_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _mock_completed(44))
        assert MacOSBackend().delete("acct") is False


# ---------------------------------------------------------------------------
# Linux backend (mocked subprocess)
# ---------------------------------------------------------------------------


class TestLinuxBackend:
    def test_store_pipes_secret_via_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: dict[str, object] = {}

        def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            recorded["argv"] = argv
            recorded["input"] = kw.get("input")
            return _mock_completed(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        LinuxBackend().store("acct", "shh")
        argv = recorded["argv"]
        assert isinstance(argv, list)
        # Secret must NOT appear in argv (would be process-table visible).
        assert "shh" not in argv
        # Secret must be passed via stdin.
        assert recorded["input"] == "shh"
        assert argv[:2] == ["secret-tool", "store"]

    def test_get_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(0, stdout="shhh"),
        )
        assert LinuxBackend().get("acct") == "shhh"

    def test_get_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _mock_completed(1, stdout=""),
        )
        assert LinuxBackend().get("acct") is None

    def test_delete_when_existed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First call: get → returns secret. Second call: clear → rc 0.
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[1] == "lookup":
                return _mock_completed(0, stdout="shhh")
            return _mock_completed(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert LinuxBackend().delete("acct") is True
        assert any(a[1] == "clear" for a in calls)

    def test_delete_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            if argv[1] == "lookup":
                return _mock_completed(1, stdout="")
            return _mock_completed(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert LinuxBackend().delete("acct") is False


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestGetBackend:
    def test_macos_picks_macos_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            secrets_mod.shutil,
            "which",
            lambda binary: "/usr/bin/security" if binary == "security" else None,
        )
        assert get_backend().name == "macos-security"

    def test_macos_missing_security_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets_mod.sys, "platform", "darwin")
        monkeypatch.setattr(secrets_mod.shutil, "which", lambda _: None)
        with pytest.raises(KeychainError, match="`security` command not found"):
            get_backend()

    def test_linux_picks_libsecret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets_mod.sys, "platform", "linux")
        monkeypatch.setattr(
            secrets_mod.shutil,
            "which",
            lambda binary: "/usr/bin/secret-tool" if binary == "secret-tool" else None,
        )
        assert get_backend().name == "libsecret"

    def test_linux_missing_secret_tool_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets_mod.sys, "platform", "linux")
        monkeypatch.setattr(secrets_mod.shutil, "which", lambda _: None)
        with pytest.raises(KeychainError, match="secret-tool"):
            get_backend()

    def test_windows_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(secrets_mod.sys, "platform", "win32")
        with pytest.raises(KeychainError, match="unsupported platform"):
            get_backend()


# ---------------------------------------------------------------------------
# CLI end-to-end (with FakeBackend injected via get_backend monkeypatch)
# ---------------------------------------------------------------------------


class TestCLI:
    def _patch_backend(self, monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
        backend = FakeBackend()
        monkeypatch.setattr(secrets_mod, "get_backend", lambda: backend)
        return backend

    def test_store_via_explicit_value(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backend = self._patch_backend(monkeypatch)
        rc = main(
            [
                "store",
                "--tenant",
                "contoso",
                "--app-id",
                "abc",
                "--secret",
                "shh",
            ]
        )
        assert rc == 0
        assert backend.data == {"contoso.abc": "shh"}
        err = capsys.readouterr().err
        assert "stored" in err
        assert "shh" not in err  # secret must never appear in user-facing output

    def test_store_via_stdin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        backend = self._patch_backend(monkeypatch)
        monkeypatch.setattr(secrets_mod.sys, "stdin", _StringIO("from-stdin\n"))
        rc = main(
            [
                "store",
                "--tenant",
                "contoso",
                "--app-id",
                "abc",
                "--secret",
                "-",
            ]
        )
        assert rc == 0
        assert backend.data == {"contoso.abc": "from-stdin"}

    def test_get_emits_secret_to_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        backend = self._patch_backend(monkeypatch)
        backend.data["contoso.abc"] = "shh"
        rc = main(["get", "--tenant", "contoso", "--app-id", "abc"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.rstrip("\n") == "shh"

    def test_get_missing_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._patch_backend(monkeypatch)
        rc = main(["get", "--tenant", "contoso", "--app-id", "abc"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_delete(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        backend = self._patch_backend(monkeypatch)
        backend.data["contoso.abc"] = "shh"
        rc = main(["delete", "--tenant", "contoso", "--app-id", "abc"])
        assert rc == 0
        assert "contoso.abc" not in backend.data

    def test_backend_unavailable_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def boom() -> KeychainBackend:
            raise KeychainError("backend unavailable for testing")

        monkeypatch.setattr(secrets_mod, "get_backend", boom)
        rc = main(["get", "--tenant", "contoso", "--app-id", "abc"])
        assert rc == 2
        assert "backend unavailable" in capsys.readouterr().err


class _StringIO:
    """Minimal stdin replacement for tests; only `read()` is used by the CLI."""

    def __init__(self, value: str) -> None:
        self._value = value

    def read(self) -> str:
        return self._value
