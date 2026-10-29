"""Tests for hermes_a365.mutator — the v0.2 thin CLI wrapper."""

from __future__ import annotations

import os
import subprocess
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


def _assert_no_marker_in_exception(exc: BaseException, marker: str) -> None:
    assert marker not in str(exc)
    assert all(marker not in str(arg) for arg in exc.args)
    skip = frozenset({"argv"})
    for key, value in vars(exc).items():
        if key in skip or value is None:
            continue
        assert marker not in str(value), key


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

    def test_sensitive_output_not_echoed(self, capfd: pytest.CaptureFixture[str]) -> None:
        m = A365CliMutator()
        m.available = True
        result = m.run([sys.executable, "-c", "print('SECRET-MARKER-123')"], sensitive=True)
        out, err = capfd.readouterr()
        assert "SECRET-MARKER-123" not in out
        assert "SECRET-MARKER-123" not in err
        assert "SECRET-MARKER-123" in result.stdout

    def test_default_run_still_streams(self, capfd: pytest.CaptureFixture[str]) -> None:
        # Pin the contrast: the non-sensitive path deliberately echoes
        # in real time (device-code prompts, CLI progress — slice 18j).
        m = A365CliMutator()
        m.available = True
        m.run([sys.executable, "-c", "print('VISIBLE-MARKER-456')"])
        out, _ = capfd.readouterr()
        assert "VISIBLE-MARKER-456" in out

    def test_sensitive_failure_detail_suppressed(self, capfd: pytest.CaptureFixture[str]) -> None:
        marker = "SECRET-MARKER-789"
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run(
                [
                    sys.executable,
                    "-c",
                    f"print({marker!r}); raise SystemExit(3)",
                ],
                sensitive=True,
            )
        _assert_no_marker_in_exception(excinfo.value, marker)
        assert excinfo.value.captured_output is None
        assert excinfo.value.sensitive is True
        assert "suppressed" in str(excinfo.value)
        out, err = capfd.readouterr()
        assert marker not in out + err

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

    def test_sensitive_streams_device_code_not_secrets(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        marker = "SENTINEL-BP-SECRET-256"
        device_line = (
            "To sign in, use a web browser to open "
            "https://login.microsoft.com/device and enter the code "
            "ABCD1234 to authenticate."
        )
        script = (
            f"print({device_line!r}); "
            f"print({marker!r}); "
            "import sys; sys.stderr.write('stderr-' + "
            f"{marker!r} + '\\n')"
        )
        m = A365CliMutator()
        m.available = True
        result = m.run([sys.executable, "-c", script], sensitive=True)
        out, err = capfd.readouterr()
        assert marker not in out
        assert marker not in err
        assert marker in result.stdout
        assert "To sign in, open https://login.microsoft.com/device" in out
        assert "ABCD1234" in out
        assert device_line not in out


class TestAdversarialSensitiveSubprocess:
    """Subprocess regressions for sensitive echo and timeout handling."""

    _SECRET = "FAKE_SECRET"
    _CODE = "ABCD1234"
    _PROMPT = (
        "To sign in, use a web browser to open https://login.microsoft.com/device "
        "and enter the code ABCD1234 to authenticate."
    )
    _PROMPT_HYPHEN = (
        "To sign in, use a web browser to open https://login.microsoft.com/device "
        "and enter the code ABCD-EFGH to authenticate."
    )
    _PROMPT_NINE = (
        "To sign in, use a web browser to open https://login.microsoft.com/device "
        "and enter the code ABCDEFGH1 to authenticate."
    )

    def _run_cap(
        self, script: str, capfd: pytest.CaptureFixture[str], *, timeout: float = 10.0
    ) -> tuple[RunResult, str, str]:
        m = A365CliMutator()
        m.available = True
        result = m.run([sys.executable, "-c", script], sensitive=True, timeout=timeout)
        out, err = capfd.readouterr()
        assert self._SECRET not in out + err
        return result, out, err

    @pytest.mark.parametrize(
        "payload",
        [
            "device code: secret=FAKE_SECRET "
            + "https://login.microsoft.com/device enter the code ABCD1234",
            '{"password":"FAKE_SECRET","message":"device code"} '
            + "https://login.microsoft.com/device enter the code ABCD1234",
            "Client secret created: FAKE_SECRET — "
            + "https://login.microsoft.com/device enter the code ABCD1234",
            "https://login.microsoft.com/device.evil enter the code ABCD1234",
            "https://evil.microsoft.com/device enter the code ABCD1234",
            "To sign in, use a web browser to open https://login.microsoft.com/device "
            "and enter the code SECRET12longcredential",
        ],
    )
    def test_adversarial_lines_never_emit_device_login(
        self,
        payload: str,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        script = f"print({payload!r})"
        _, out, _ = self._run_cap(script, capfd)
        assert "To sign in, open" not in out
        assert self._CODE not in out

    def test_valid_hyphen_and_nine_char_codes(self, capfd: pytest.CaptureFixture[str]) -> None:
        script = f"print({self._PROMPT_HYPHEN!r}); print({self._PROMPT_NINE!r})"
        _, out, _ = self._run_cap(script, capfd)
        assert "ABCD-EFGH" in out
        assert "ABCDEFGH1" in out

    def test_adversarial_ansi_full_line(self, capfd: pytest.CaptureFixture[str]) -> None:
        script = (
            "import sys\n"
            "sys.stdout.write('\\x1b[1m' + "
            f"{self._PROMPT!r} + '\\x1b[0m\\n')\n"
            "sys.stderr.write('stderr-FAKE_SECRET\\n')\n"
        )
        _, out, err = self._run_cap(script, capfd)
        assert self._SECRET not in out + err
        assert self._CODE in out

    def test_adversarial_long_line_rejected(self, capfd: pytest.CaptureFixture[str]) -> None:
        filler = "x" * 5000
        payload = filler + " " + self._PROMPT
        script = f"print({payload!r})"
        _, out, _ = self._run_cap(script, capfd)
        assert "To sign in, open" not in out

    def test_adversarial_unterminated_partial_times_out(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = (
            "import sys,time\n"
            "sys.stdout.write('https://login.microsoft.com/device enter the code PARTIALNO')\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        m = A365CliMutator()
        m.available = True
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            m.run([sys.executable, "-c", script], sensitive=True, timeout=0.4)
        assert not excinfo.value.output
        out, err = capfd.readouterr()
        assert "PARTIAL" not in out + err
        assert "To sign in, open" not in out + err

    _DELAYED_LINE_PREFIX = (
        "To sign in, use a web browser to open https://login.microsoft.com/device "
        "and enter the code ABCD1234"
    )

    def test_delayed_stdout_continuation_rejects_premature_eof_emit(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Child exits while a descendant still holds stdout and extends the line."""
        if not hasattr(os, "fork"):
            pytest.skip("POSIX fork required")
        script = (
            "import os, sys, time\n"
            f"prefix = {self._DELAYED_LINE_PREFIX!r}\n"
            "if os.fork() == 0:\n"
            "    time.sleep(0.4)\n"
            "    os.write(sys.stdout.fileno(), b'EXTRA\\n')\n"
            "    os._exit(0)\n"
            "os.write(sys.stdout.fileno(), prefix.encode())\n"
            "os._exit(0)\n"
        )
        _, out, _ = self._run_cap(script, capfd, timeout=5.0)
        assert "To sign in, open" not in out
        assert self._CODE not in out

    def test_delayed_stderr_continuation_rejects_premature_eof_emit(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Same as stdout, but the partial prompt is written to stderr."""
        if not hasattr(os, "fork"):
            pytest.skip("POSIX fork required")
        script = (
            "import os, sys, time\n"
            f"prefix = {self._DELAYED_LINE_PREFIX!r}\n"
            "if os.fork() == 0:\n"
            "    time.sleep(0.4)\n"
            "    os.write(sys.stderr.fileno(), b'EXTRA\\n')\n"
            "    os._exit(0)\n"
            "os.write(sys.stderr.fileno(), prefix.encode())\n"
            "os._exit(0)\n"
        )
        _, out, err = self._run_cap(script, capfd, timeout=5.0)
        merged = out + err
        assert "To sign in, open" not in merged
        assert self._CODE not in merged

    def test_chunk_split_mid_code_emits_only_after_full_line(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = (
            "import sys,time\n"
            "sys.stdout.write('To sign in, use a web browser to open "
            "https://login.microsoft.com/device and enter the code AB')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.15)\n"
            "sys.stdout.write('CD1234 to authenticate.\\n')\n"
            "sys.stdout.flush()\n"
        )
        _, out, _ = self._run_cap(script, capfd)
        assert self._CODE in out

    def test_same_line_extra_chars_after_code_rejected(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        bad = (
            "To sign in, use a web browser to open https://login.microsoft.com/device "
            "and enter the code ABCD1234EXTRA"
        )
        script = f"print({bad!r})"
        _, out, _ = self._run_cap(script, capfd)
        assert "To sign in, open" not in out

    def test_banner_and_prompt_in_one_chunk_while_child_alive(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = (
            "import sys,time\n"
            "sys.stdout.write('LOADING...\\n" + self._PROMPT + "\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.3)\n"
            "print('FAKE_SECRET')\n"
        )
        _, out, _ = self._run_cap(script, capfd)
        assert "LOADING" not in out
        assert self._CODE in out

    def test_sensitive_failure_no_secret_in_exception_attrs(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = f"print({self._PROMPT!r}); print({self._SECRET!r}); raise SystemExit(2)"
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run([sys.executable, "-c", script], sensitive=True)
        _assert_no_marker_in_exception(excinfo.value, self._SECRET)
        assert excinfo.value.captured_output is None

    def test_streaming_single_chunk_prompt_visible(self, capfd: pytest.CaptureFixture[str]) -> None:
        script = (
            "import sys,time\n"
            "sys.stdout.write('LOADING...\\n" + self._PROMPT + "\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.2)\n"
        )
        rc, _ = _run_streaming([sys.executable, "-c", script], timeout=5.0)
        assert rc == 0
        out, _ = capfd.readouterr()
        assert "LOADING" in out
        assert "ABCD1234" in out

    def test_streaming_stdout_stderr_order(self, capfd: pytest.CaptureFixture[str]) -> None:
        script = (
            "import sys\n"
            "sys.stdout.write('1stdout\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('2stderr\\n')\n"
            "sys.stderr.flush()\n"
            "sys.stdout.write('3stdout\\n')\n"
        )
        _, combined = _run_streaming([sys.executable, "-c", script], timeout=5.0)
        out, _ = capfd.readouterr()
        merged = out if out else combined
        idx1 = merged.find("1stdout")
        idx2 = merged.find("2stderr")
        idx3 = merged.find("3stdout")
        assert -1 not in (idx1, idx2, idx3)
        assert idx1 < idx2 < idx3

    def test_device_login_rejects_unicode_homoglyph_in_code(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # U+017F (Latin small letter long s) must not match an ASCII-only code class.
        homoglyph = "ABCD12\u017f4"
        prompt = (
            "To sign in, use a web browser to open https://login.microsoft.com/device "
            f"and enter the code {homoglyph} to authenticate."
        )
        script = f"print({prompt!r})"
        m = A365CliMutator()
        m.available = True
        m.run([sys.executable, "-c", script], sensitive=True)
        out, _ = capfd.readouterr()
        assert "To sign in, open" not in out
        assert homoglyph not in out

    def test_sensitive_failure_traceback_locals_drop_stream_text(self) -> None:
        marker = "TRACE-LOCAL-SECRET-88"
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run(
                [
                    sys.executable,
                    "-c",
                    f"print({marker!r}); raise SystemExit(1)",
                ],
                sensitive=True,
            )
        tb = excinfo.value.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if frame.f_code.co_filename.endswith(f"{os.sep}mutator.py"):
                for name, value in frame.f_locals.items():
                    if name in ("argv",):
                        continue
                    if isinstance(value, str) and marker in value:
                        pytest.fail(f"frame local {name!r} retained stream text")
            tb = tb.tb_next


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
