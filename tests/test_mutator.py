"""Tests for hermes_a365.mutator — the v0.2 thin CLI wrapper."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from hermes_a365.mutator import (
    A365_CLI_BINARY,
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    A365CliMutator,
    AADSTSError,
    CliInvocationError,
    Mutator,
    RunResult,
    _run_streaming,
    get_mutator,
)

# ---------------------------------------------------------------------------
# RunResult shape
# ---------------------------------------------------------------------------


class TestRunResult:
    def test_combined_strips_and_concatenates(self) -> None:
        r = RunResult(argv=["a365"], returncode=0, stdout="hello\n", stderr="warn\n")
        assert r.combined == "hello\nwarn"

    def test_combined_blank_returns_empty(self) -> None:
        r = RunResult(argv=["a365"], returncode=0, stdout="   ", stderr="")
        assert r.combined == ""


# ---------------------------------------------------------------------------
# AADSTSError catalogue
# ---------------------------------------------------------------------------


class TestAADSTSError:
    def test_carries_code_and_message(self) -> None:
        e = AADSTSError("AADSTS500011", "license not propagated")
        assert e.code == "AADSTS500011"
        assert "license not propagated" in str(e)

    def test_well_known_codes_exposed(self) -> None:
        # Apply paths import these constants directly; pin them.
        assert AADSTS_LICENSE_NOT_PROPAGATED == "AADSTS500011"
        assert AADSTS_CONSENT_REQUIRED == "AADSTS90094"


# ---------------------------------------------------------------------------
# A365CliMutator.run — happy path
# ---------------------------------------------------------------------------


class TestA365CliMutatorRun:
    def test_success_returns_run_result(self) -> None:
        m = A365CliMutator()
        m.available = True  # bypass PATH check
        with patch("hermes_a365.mutator._run_streaming", return_value=(0, "ok\n")):
            result = m.run(["a365", "setup", "blueprint", "--agent-name", "x"])
        assert isinstance(result, RunResult)
        assert result.returncode == 0
        assert result.stdout == "ok\n"
        # Slice 18j: stderr is merged into stdout via subprocess STDOUT redirection.
        assert result.stderr == ""
        assert result.argv == ["a365", "setup", "blueprint", "--agent-name", "x"]

    def test_unavailable_raises_cli_error(self) -> None:
        m = A365CliMutator()
        m.available = False
        with pytest.raises(CliInvocationError, match="not on PATH"):
            m.run(["a365", "--help"])


# ---------------------------------------------------------------------------
# Error path: AADSTS extraction
# ---------------------------------------------------------------------------


class TestAADSTSExtraction:
    def test_aadsts_code_anywhere_raises_aadsts_error(self) -> None:
        m = A365CliMutator()
        m.available = True
        bad = "ERROR AADSTS500011: tenant license has not propagated yet"
        with (
            patch("hermes_a365.mutator._run_streaming", return_value=(2, bad)),
            pytest.raises(AADSTSError) as excinfo,
        ):
            m.run(["a365", "setup", "blueprint"])
        assert excinfo.value.code == "AADSTS500011"

    def test_aadsts_with_preceding_chatter_caught(self) -> None:
        m = A365CliMutator()
        m.available = True
        bad = "Some chatter\nAADSTS90094: admin consent required"
        with (
            patch("hermes_a365.mutator._run_streaming", return_value=(1, bad)),
            pytest.raises(AADSTSError) as excinfo,
        ):
            m.run(["a365", "setup", "permissions", "bot"])
        assert excinfo.value.code == "AADSTS90094"

    def test_non_aadsts_failure_raises_cli_invocation_error(self) -> None:
        m = A365CliMutator()
        m.available = True
        with (
            patch("hermes_a365.mutator._run_streaming", return_value=(7, "weird crash")),
            pytest.raises(CliInvocationError) as excinfo,
        ):
            m.run(["a365", "setup", "blueprint"])
        assert excinfo.value.returncode == 7
        assert "weird crash" in excinfo.value.output

    def test_stdin_input_threads_through_to_run_streaming(self) -> None:
        """Slice 18w: cleanup needs to feed `y\\n` to the subprocess.

        The kwarg must reach `_run_streaming` unchanged.
        """
        m = A365CliMutator()
        m.available = True
        with patch("hermes_a365.mutator._run_streaming") as runner:
            runner.return_value = (0, "")
            m.run(["a365", "cleanup", "azure", "--agent-name", "x"], stdin_input="y\n")
        assert runner.call_args.kwargs["stdin_input"] == "y\n"


# ---------------------------------------------------------------------------
# _run_streaming smoke (real subprocess)
# ---------------------------------------------------------------------------


class TestRunStreamingStdin:
    """Slice 18w real-subprocess smoke for the new stdin pipe."""

    def test_stdin_input_reaches_subprocess(self) -> None:
        rc, out = _run_streaming(
            [sys.executable, "-c", "print(input().strip().upper())"],
            timeout=10.0,
            stdin_input="hello\n",
        )
        assert rc == 0
        assert "HELLO" in out

    def test_no_stdin_input_means_inherited_stdin(self) -> None:
        # The default path — stdin not connected to a pipe — must keep
        # working for `setup` flows that emit device-code prompts.
        rc, out = _run_streaming(
            [sys.executable, "-c", "print('no stdin needed')"],
            timeout=5.0,
        )
        assert rc == 0
        assert "no stdin needed" in out


# ---------------------------------------------------------------------------
# Sensitive (captured) runs — #111 / CS-003
# ---------------------------------------------------------------------------


class TestSensitiveRun:
    """`run(..., sensitive=True)` captures output without echoing it.

    Real-subprocess tests (no patching) — the point is what reaches the
    parent's actual stdout/stderr, so `capfd` watches the file
    descriptors themselves. All secret material is fake.
    """

    def test_sensitive_output_not_echoed(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        m = A365CliMutator()
        m.available = True
        result = m.run(
            [sys.executable, "-c", "print('SECRET-MARKER-123')"], sensitive=True
        )
        out, err = capfd.readouterr()
        assert "SECRET-MARKER-123" not in out
        assert "SECRET-MARKER-123" not in err
        assert "SECRET-MARKER-123" in result.stdout

    def test_default_run_still_streams(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # Pin the contrast: the non-sensitive path deliberately echoes
        # in real time (device-code prompts, CLI progress — slice 18j).
        m = A365CliMutator()
        m.available = True
        m.run([sys.executable, "-c", "print('VISIBLE-MARKER-456')"])
        out, _ = capfd.readouterr()
        assert "VISIBLE-MARKER-456" in out

    def test_sensitive_failure_detail_suppressed(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # A failing sensitive command may have already emitted credential
        # material; the exception text callers print must not carry it.
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run(
                [
                    sys.executable,
                    "-c",
                    "print('SECRET-MARKER-789'); raise SystemExit(3)",
                ],
                sensitive=True,
            )
        assert "SECRET-MARKER-789" not in str(excinfo.value)
        assert "suppressed" in str(excinfo.value)
        out, err = capfd.readouterr()
        assert "SECRET-MARKER-789" not in out + err

    def test_sensitive_aadsts_code_still_surfaced(self) -> None:
        # AADSTS detection keeps working on the captured output; only the
        # raw detail is withheld from the exception.
        m = A365CliMutator()
        m.available = True
        with pytest.raises(AADSTSError) as excinfo:
            m.run(
                [
                    sys.executable,
                    "-c",
                    "print('AADSTS500011 blah SECRET-MARKER-000'); raise SystemExit(1)",
                ],
                sensitive=True,
            )
        assert excinfo.value.code == "AADSTS500011"
        assert "SECRET-MARKER-000" not in str(excinfo.value)

    def test_sensitive_stdin_input_threads_through(self) -> None:
        m = A365CliMutator()
        m.available = True
        result = m.run(
            [sys.executable, "-c", "print(input().strip().upper())"],
            stdin_input="quiet\n",
            sensitive=True,
        )
        assert "QUIET" in result.stdout


# ---------------------------------------------------------------------------
# Mutator protocol
# ---------------------------------------------------------------------------


class _FakeMutator:
    """Minimal protocol-compatible fake — used by every v0.2 apply test."""

    def __init__(self) -> None:
        self.available = True
        self.calls: list[list[str]] = []
        self.scripted: list[RunResult | Exception] = []

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        stdin_input: str | None = None,
        sensitive: bool = False,
    ) -> RunResult:
        self.calls.append(list(argv))
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


class TestMutatorProtocol:
    def test_fake_satisfies_protocol_statically(self) -> None:
        # If this binds, _FakeMutator structurally matches the Mutator protocol.
        m: Mutator = _FakeMutator()
        # Round-trip a call.
        result = m.run(["a365", "--help"])
        assert isinstance(result, RunResult)


# ---------------------------------------------------------------------------
# get_mutator default
# ---------------------------------------------------------------------------


class TestGetMutator:
    def test_returns_a365cli_mutator(self) -> None:
        assert isinstance(get_mutator(), A365CliMutator)

    def test_a365_cli_binary_constant_pinned(self) -> None:
        assert A365_CLI_BINARY == "a365"
