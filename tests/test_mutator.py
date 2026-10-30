"""Tests for hermes_a365.mutator — the v0.2 thin CLI wrapper."""

from __future__ import annotations

import asyncio
import gc
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_a365.mutator import (
    _MAX_LINE_PENDING_CHARS,
    _MAX_TOTAL_CAPTURE_BYTES,
    A365_CLI_BINARY,
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    A365CliMutator,
    AADSTSError,
    CliInvocationError,
    Mutator,
    RunResult,
    _LineBuffer,
    _Pump,
    _reap,
    _run_streaming,
    get_mutator,
)

_OPAQUE_GRAPH_SECRET = "SYNTHETIC~ClientCredential42"


def _install_az_rest_shim_for_mutator_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, response_body: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    az = bin_dir / "az"
    az.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "rest --method GET" ]; then\n'
        f"  printf '%s\\n' '{response_body}'\n"
        "  exit 1\n"
        "fi\n"
        "echo unexpected az >&2\n"
        "exit 1\n"
    )
    az.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _iter_exception_chain(exc: BaseException | None) -> Iterable[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException] = []
    if exc is not None:
        pending.append(exc)
    while pending:
        current = pending.pop()
        oid = id(current)
        if oid in seen:
            continue
        seen.add(oid)
        yield current
        ctx = current.__context__
        cause = current.__cause__
        if ctx is not None and id(ctx) not in seen:
            pending.append(ctx)
        if cause is not None and id(cause) not in seen:
            pending.append(cause)


def _scan_value_for_marker(value: Any, marker: str, *, _seen: set[int]) -> None:
    if value is None:
        return
    oid = id(value)
    if oid in _seen:
        return
    if isinstance(value, BaseException):
        for link in _iter_exception_chain(value):
            _scan_value_for_marker(link, marker, _seen=_seen)
        return
    if isinstance(value, (str, bytes, bytearray)):
        text = value.decode() if isinstance(value, (bytes, bytearray)) else value
        assert marker not in text, repr(value)[:200]
        return
    _seen.add(oid)
    if isinstance(value, re.Match):
        _scan_value_for_marker(value.string, marker, _seen=_seen)
        return
    if isinstance(value, dict):
        for item in value.values():
            _scan_value_for_marker(item, marker, _seen=_seen)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        if isinstance(value, list) and len(value) >= 3 and value[1] == "-c":
            return
        for item in value:
            _scan_value_for_marker(item, marker, _seen=_seen)
        return


def _assert_marker_absent_from_failure_artifacts(exc: BaseException, marker: str) -> None:
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for link in _iter_exception_chain(exc):
        assert link.__cause__ is None
        assert link.__context__ is None
        assert marker not in str(link)
        assert marker not in repr(link)
        for key, val in vars(link).items():
            if key == "argv":
                continue
            _scan_value_for_marker(val, marker, _seen=set())
    for link in _iter_exception_chain(exc):
        tb = link.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            filename = frame.f_code.co_filename
            if filename.endswith(f"{os.sep}mutator.py") or filename.endswith(
                f"{os.sep}register.py"
            ):
                gc.collect()
                argv_local = frame.f_locals.get("argv")
                for name, value in frame.f_locals.items():
                    if name in ("argv",):
                        continue
                    _scan_value_for_marker(value, marker, _seen=set())
                for ref in gc.get_referents(frame):
                    if ref is argv_local:
                        continue
                    if isinstance(ref, str) and isinstance(argv_local, list) and ref in argv_local:
                        continue
                    _scan_value_for_marker(ref, marker, _seen=set())
            tb = tb.tb_next


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
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_sensitive_aadsts_failure_drops_child_output_from_artifacts(self) -> None:
        marker = "AADSTS-CHAIN-MARKER-77"
        m = A365CliMutator()
        m.available = True
        with pytest.raises(AADSTSError) as excinfo:
            m.run(
                [
                    sys.executable,
                    "-c",
                    f"print({marker!r}); print('AADSTS500011 license'); raise SystemExit(1)",
                ],
                sensitive=True,
            )
        assert excinfo.value.code == "AADSTS500011"
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_sensitive_timeout_drops_child_output_from_artifacts(
        self, capfd: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        marker = "TIMEOUT-CHAIN-MARKER-66"
        script_path = tmp_path / "hang.py"
        script_path.write_text(
            f"import time\nprint({marker!r})\ntime.sleep(5)\n",
            encoding="utf-8",
        )
        m = A365CliMutator()
        m.available = True
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            m.run([sys.executable, str(script_path)], sensitive=True, timeout=0.4)
        assert excinfo.value.__context__ is None
        assert not excinfo.value.output
        assert marker not in str(excinfo.value)
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)
        out, err = capfd.readouterr()
        assert marker not in out + err

    def test_sensitive_graph_error_uses_fixed_message_not_server_body(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import json

        body = json.dumps(
            {
                "error": {
                    "code": "AccessDenied",
                    "message": f"secret={_OPAQUE_GRAPH_SECRET} leaked",
                }
            }
        )
        _install_az_rest_shim_for_mutator_test(tmp_path, monkeypatch, response_body=body)
        monkeypatch.setenv("DOTNET_ROOT", "/nonexistent")
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run(
                ["az", "rest", "--method", "GET", "--uri", "https://graph.microsoft.test/v1.0/x"],
                sensitive=True,
            )
        assert excinfo.value.graph_error is not None
        assert excinfo.value.graph_error[0] == "AccessDenied"
        assert _OPAQUE_GRAPH_SECRET not in excinfo.value.graph_error[1]
        assert excinfo.value.graph_error[1] == "Access was denied."
        _assert_marker_absent_from_failure_artifacts(excinfo.value, _OPAQUE_GRAPH_SECRET)


# ---------------------------------------------------------------------------
# Subprocess lifetime and scrubbing (#271 slice 1)
# ---------------------------------------------------------------------------


def _child_script_reads_sentinel(sentinel_path: Path) -> Path:
    script = sentinel_path.with_name("child_read_sentinel.py")
    script.write_text(
        "import sys\n"
        f"sys.stdout.write(open({str(sentinel_path)!r}, encoding='utf-8').read())\n"
        "sys.stdout.write('\\n')\n",
        encoding="utf-8",
    )
    return script


class TestMutatorSubprocessScrubbing:
    def test_stream_exception_has_empty_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "STREAM-EXC-SENTINEL-991"
        sentinel_path = tmp_path / "sent.txt"
        sentinel_path.write_text(marker, encoding="utf-8")
        script = _child_script_reads_sentinel(sentinel_path)
        real_feed = _LineBuffer.feed

        def feed(self: _LineBuffer, chunk: bytes) -> list[str]:
            lines = real_feed(self, chunk)
            if marker in self.pending or any(marker in line for line in lines):
                raise RuntimeError("injected stream failure")
            return lines

        monkeypatch.setattr(_LineBuffer, "feed", feed)
        release_calls: list[BaseException] = []
        import hermes_a365.mutator as mutator_mod

        real_release = mutator_mod._release

        def release_spy(exc: BaseException) -> None:
            release_calls.append(exc)
            real_release(exc)

        monkeypatch.setattr(mutator_mod, "_release", release_spy)
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run([sys.executable, str(script)], sensitive=True)
        assert excinfo.value.sensitive is True
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None
        assert release_calls, "expected _release while handling the injected stream error"
        assert len(release_calls) >= 2, "expected _release from run and from subprocess cleanup"
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_overflow_pending_and_chunk_are_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "OVERFLOW-PENDING-MARKER-44"
        sentinel_path = tmp_path / "sent.txt"
        sentinel_path.write_text(marker, encoding="utf-8")
        script = tmp_path / "overflow_child.py"
        script.write_text(
            "import sys\n"
            f"sys.stdout.write(open({str(sentinel_path)!r}, encoding='utf-8').read())\n"
            f"sys.stdout.write('{'x' * (_MAX_LINE_PENDING_CHARS + 1)}')\n",
            encoding="utf-8",
        )
        m = A365CliMutator()
        m.available = True
        clear_calls: list[int] = []
        feed_clears: list[int] = []
        real_clear = _LineBuffer.clear

        def clear_spy(self: _LineBuffer) -> None:
            import inspect

            clear_calls.append(1)
            if any(frame.function == "feed" for frame in inspect.stack()[1:8]):
                feed_clears.append(1)
            real_clear(self)

        monkeypatch.setattr(_LineBuffer, "clear", clear_spy)
        with pytest.raises(CliInvocationError) as excinfo:
            m.run([sys.executable, str(script)], sensitive=True)
        assert feed_clears, "expected pending buffer clear inside feed before overflow"
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_total_capture_cap_overflow(self, tmp_path: Path) -> None:
        line_chars = 200 * 1024
        line_count = (_MAX_TOTAL_CAPTURE_BYTES // line_chars) + 2
        script = tmp_path / "total_cap_child.py"
        script.write_text(
            "import sys\n"
            + "".join(
                f"sys.stdout.write({('a' * line_chars)!r})\n" for _ in range(line_count)
            ),
            encoding="utf-8",
        )
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError, match="capture size limit"):
            m.run([sys.executable, str(script)], sensitive=False)
        import hermes_a365.mutator as mutator_mod

        assert mutator_mod._MAX_TOTAL_CAPTURE_BYTES == 4 * 1024 * 1024

    def test_cancelled_error_scrubs_and_reaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "CANCEL-SENTINEL-882"
        sentinel_path = tmp_path / "sent.txt"
        sentinel_path.write_text(marker, encoding="utf-8")
        script = _child_script_reads_sentinel(sentinel_path)
        import hermes_a365.mutator as mutator_mod

        original_emit = mutator_mod._try_emit_device_login_line

        def emit_hook(line: str, *, emitted: set[tuple[str, str]], eof: bool = False) -> None:
            if marker in line:
                raise asyncio.CancelledError()
            original_emit(line, emitted=emitted, eof=eof)

        monkeypatch.setattr(mutator_mod, "_try_emit_device_login_line", emit_hook)
        m = A365CliMutator()
        m.available = True
        with pytest.raises(asyncio.CancelledError) as excinfo:
            m.run([sys.executable, str(script)], sensitive=True)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_keyboard_interrupt_scrubs_and_reaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "KBI-SENTINEL-771"
        sentinel_path = tmp_path / "sent.txt"
        sentinel_path.write_text(marker, encoding="utf-8")
        script = _child_script_reads_sentinel(sentinel_path)
        import hermes_a365.mutator as mutator_mod

        original_emit = mutator_mod._try_emit_device_login_line

        def emit_hook(line: str, *, emitted: set[tuple[str, str]], eof: bool = False) -> None:
            if marker in line:
                raise KeyboardInterrupt()
            original_emit(line, emitted=emitted, eof=eof)

        monkeypatch.setattr(mutator_mod, "_try_emit_device_login_line", emit_hook)
        m = A365CliMutator()
        m.available = True
        with pytest.raises(KeyboardInterrupt) as excinfo:
            m.run([sys.executable, str(script)], sensitive=True)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)

    def test_sensitive_failure_drops_stdin_local(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        marker = "STDIN-LOCAL-MARKER-665"
        script = tmp_path / "stdin_echo_fail.py"
        script.write_text(
            "import sys\ntext = sys.stdin.read()\nsys.stdout.write(text)\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        m = A365CliMutator()
        m.available = True
        with pytest.raises(CliInvocationError) as excinfo:
            m.run(
                [sys.executable, str(script)],
                sensitive=True,
                stdin_input=f"{marker}\n",
            )
        _assert_marker_absent_from_failure_artifacts(excinfo.value, marker)
        out, err = capfd.readouterr()
        assert marker not in out + err

    def test_reap_on_overflow_timeout_and_cancel(self, tmp_path: Path) -> None:
        real_popen = subprocess.Popen
        proc_refs: list[subprocess.Popen[bytes]] = []

        def popen_wrap(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)
            proc_refs.append(proc)
            return proc

        def assert_child_reaped() -> None:
            proc = proc_refs[-1]
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(proc.pid, 0)

        m = A365CliMutator()
        m.available = True

        hang = tmp_path / "hang.py"
        hang.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        with (
            patch("hermes_a365.mutator.subprocess.Popen", popen_wrap),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            m.run([sys.executable, str(hang)], timeout=0.5)
        assert_child_reaped()

        overflow = tmp_path / "overflow_reap.py"
        overflow.write_text(
            f"import sys\nsys.stdout.write('{'y' * (_MAX_LINE_PENDING_CHARS + 1)}')\n",
            encoding="utf-8",
        )
        proc_refs.clear()
        with (
            patch("hermes_a365.mutator.subprocess.Popen", popen_wrap),
            pytest.raises(CliInvocationError),
        ):
            m.run([sys.executable, str(overflow)], sensitive=True, timeout=5.0)
        assert_child_reaped()

    def test_reap_order_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list[str] = []
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.close.side_effect = lambda: order.append("stdout.close")
        stderr.close.side_effect = lambda: order.append("stderr.close")
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdin = None
        proc.stdout = stdout
        proc.stderr = stderr
        thread = MagicMock()
        thread.is_alive.return_value = False
        thread.join.side_effect = lambda timeout=0: order.append("thread.join")
        stop = threading.Event()
        pump = _Pump(
            proc=proc,
            argv=["x"],
            timeout=1.0,
            streams=("stdout", "stderr"),
            echo="all",
            sensitive=False,
            stop=stop,
            out_q=queue.Queue(),
            threads=[thread],
            buffers={"stdout": _LineBuffer(), "stderr": _LineBuffer()},
            eof_seen={"stdout": True, "stderr": True},
            emitted_logins=set(),
            captured=[],
            deadline=0.0,
            thread_streams=[(thread, "stdout")],
        )
        monkeypatch.setattr(os, "name", "posix")
        _reap(pump)
        join_at = order.index("thread.join")
        close_at = order.index("stdout.close")
        assert join_at < close_at

    def test_reap_order_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list[str] = []
        stdout = MagicMock()
        stderr = MagicMock()
        stdout.close.side_effect = lambda: order.append("stdout.close")
        stderr.close.side_effect = lambda: order.append("stderr.close")
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdin = None
        proc.stdout = stdout
        proc.stderr = stderr
        thread = MagicMock()
        thread.is_alive.return_value = False
        thread.join.side_effect = lambda timeout=0: order.append("thread.join")
        stop = threading.Event()
        pump = _Pump(
            proc=proc,
            argv=["x"],
            timeout=1.0,
            streams=("stdout", "stderr"),
            echo="all",
            sensitive=False,
            stop=stop,
            out_q=queue.Queue(),
            threads=[thread],
            buffers={"stdout": _LineBuffer(), "stderr": _LineBuffer()},
            eof_seen={"stdout": True, "stderr": True},
            emitted_logins=set(),
            captured=[],
            deadline=0.0,
            thread_streams=[(thread, "stdout")],
        )
        monkeypatch.setattr(os, "name", "nt")
        _reap(pump)
        close_at = order.index("stdout.close")
        join_at = order.index("thread.join")
        assert close_at < join_at


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
