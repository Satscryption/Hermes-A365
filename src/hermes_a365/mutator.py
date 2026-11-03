"""Thin wrapper over the real Microsoft.Agents.A365.DevTools.Cli (binary ``a365``).

The :class:`Mutator` exposes a single :meth:`Mutator.run` that takes
an ``argv`` list. Per-script appliers build the argv themselves and
parse the captured output. This is closer to how the real CLI is
invoked, keeps the protocol stable as the CLI's flag set evolves,
and makes test fakes trivial (record argv, return scripted output).

See ``references/a365-cli-reference.md`` for the verified command
surface (CLI v1.1.171, .NET only — no npm variant).

The 2026-05-05 live walkthrough surfaced that ``a365 setup`` verbs
emit interactive device-code prompts on first run for write-scope MSAL
bootstrap. The original implementation captured stdout in a single
buffer (``subprocess.run(capture_output=True)``), which hid those
prompts until the subprocess completed — operators couldn't see the
code to enter it, the wrapper hung indefinitely, and the timeout
killed the partially-completed CLI run. Slice 18j replaces that with
:func:`_run_streaming`: line-buffered output that flows to the
operator's stdout in real time while still being captured for AADSTS
detection. Stderr is merged into stdout (subprocess ``stderr=STDOUT``)
so chronological order is preserved across the merged stream;
``RunResult.stderr`` is consequently always empty in production.

AADSTS handling lives here too: the CLI surfaces Microsoft auth errors
embedded in its stdout, so every :meth:`run` call screens for
``AADSTS<code>`` tokens and raises :class:`AADSTSError` on a non-zero
exit when one is found.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

# Verified in references/a365-cli-reference.md; only .NET ships.
A365_CLI_BINARY = "a365"

# Codes the apply paths react to specifically. Anything else surfaces as a
# generic AADSTSError (which itself is a non-zero exit with a known token).
AADSTS_LICENSE_NOT_PROPAGATED = "AADSTS500011"
AADSTS_CONSENT_REQUIRED = "AADSTS90094"
AADSTS_TOKEN_EXPIRED = "AADSTS70043"
_AADSTS_RE = re.compile(r"AADSTS\d{4,7}")


def _owner_only_child_setup() -> None:
    """POSIX child hook: secret-producing CLIs create files under umask 077."""
    os.umask(0o077)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AADSTSError(RuntimeError):
    """Raised on a non-zero CLI exit whose output contains an ``AADSTS<code>`` token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CliInvocationError(RuntimeError):
    """Raised on a non-zero CLI exit that doesn't surface an AADSTS code."""

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        output: str,
        *,
        captured_output: str | None = None,
        sensitive: bool = False,
        graph_error: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(
            f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed (rc={returncode}): {output}"
        )
        self.argv = argv
        self.returncode = returncode
        self.output = output
        self.sensitive = sensitive
        self.graph_error = graph_error if sensitive else None
        if sensitive:
            self.captured_output = None
        else:
            self.captured_output = output if captured_output is None else captured_output


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Captured outcome of a CLI invocation (success path).

    Returned from :meth:`Mutator.run`. ``stdout`` and ``stderr`` are the
    raw text streams as captured by the subprocess; appliers that want
    structured data extract it themselves (the CLI's text output isn't
    machine-readable JSON in v1.1.171).
    """

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stdout + self.stderr).strip()


# ---------------------------------------------------------------------------
# Mutator protocol
# ---------------------------------------------------------------------------


class Mutator(Protocol):
    """Run an ``a365`` invocation. Tests stub this; production shells out."""

    available: bool

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 900.0,
        stdin_input: str | None = None,
        sensitive: bool = False,
    ) -> RunResult: ...


# ---------------------------------------------------------------------------
# Concrete: A365 CLI
# ---------------------------------------------------------------------------


class A365CliMutator:
    """Default mutator — shells out to the installed ``a365`` CLI.

    Honours ``DOTNET_ROOT`` from the environment (required on macOS where
    brew installs dotnet at ``/opt/homebrew/opt/dotnet/libexec`` and the
    .NET tool host can't auto-discover it). Sets it from common locations
    if the caller hasn't already.
    """

    name = "a365-cli"

    def __init__(self) -> None:
        self.available = shutil.which(A365_CLI_BINARY) is not None
        # Best-effort DOTNET_ROOT setup on macOS so the .NET tool host
        # can find a runtime. Doesn't override an existing value.
        if "DOTNET_ROOT" not in os.environ:
            for candidate in (
                "/opt/homebrew/opt/dotnet/libexec",
                "/usr/local/opt/dotnet/libexec",
                "/usr/local/share/dotnet",
            ):
                if os.path.isdir(candidate):
                    os.environ["DOTNET_ROOT"] = candidate
                    break

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 900.0,
        stdin_input: str | None = None,
        sensitive: bool = False,
    ) -> RunResult:
        if not self.available:
            raise CliInvocationError(argv, -1, f"{A365_CLI_BINARY} not on PATH")
        action: str | None = None
        exit_code: int | None = None
        returncode = 0
        combined = stripped = ""
        match = None
        detail = None
        graph_error = None
        code = None
        try:
            if sensitive:
                returncode, combined = _run_sensitive_streaming(
                    argv, timeout=timeout, stdin_input=stdin_input
                )
            else:
                returncode, combined = _run_streaming(
                    argv, timeout=timeout, stdin_input=stdin_input
                )
            if returncode == 0:
                return RunResult(argv=list(argv), returncode=returncode, stdout=combined, stderr="")
            stripped = combined.strip()
            match = _AADSTS_RE.search(stripped)
            detail = _sensitive_failure_detail(stripped) if sensitive else stripped
            graph_error = _extract_sensitive_graph_error(stripped) if sensitive else None
            if match:
                code = match.group(0)
                action = "aadsts"
            else:
                action = "cli_invocation"
        except BaseException as exc:
            action, exit_code = _classify(exc)
            detail = graph_error = code = None
            _release(exc)
        finally:
            stdin_input = None
            combined = stripped = ""
            match = None
        assert action is not None
        _raise_clean(
            action,
            argv,
            timeout,
            exit_code,
            sensitive=sensitive,
            returncode=returncode,
            detail=detail,
            graph_error=graph_error,
            aadsts_code=code,
        )


# ---------------------------------------------------------------------------
# Subprocess helpers — streaming (default) and sensitive device-login echo
# ---------------------------------------------------------------------------

_SENSITIVE_OUTPUT_SUPPRESSED = "<output suppressed: sensitive command may emit credential material>"

_READ_CHUNK_BYTES = 4096
_MAX_LINE_BYTES = 256 * 1024
_MAX_TOTAL_CAPTURE_BYTES = 4 * 1024 * 1024
_QUEUE_MAXSIZE = 32
_REAP_TERMINATE_GRACE_SECONDS = 0.2


class _CaptureLimit(BaseException):
    """Internal signal: capture size limit exceeded after buffers were cleared."""


class _Deadline(BaseException):
    """Internal signal: wall-clock deadline hit; child will be reaped."""


class _StreamFailure(BaseException):
    """Internal signal: a reader thread exited before signalling EOF."""


def _release(exc: BaseException) -> None:
    """Drop traceback links and clear frames on an exception chain."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        oid = id(current)
        if oid in seen:
            continue
        seen.add(oid)
        ctx = current.__context__
        cause = current.__cause__
        if ctx is not None and id(ctx) not in seen:
            pending.append(ctx)
        if cause is not None and id(cause) not in seen:
            pending.append(cause)
        tb = current.__traceback__
        while tb is not None:
            with contextlib.suppress(RuntimeError):
                tb.tb_frame.clear()
            tb = tb.tb_next
        current.__traceback__ = None
        current.__context__ = None
        current.__cause__ = None
        with contextlib.suppress(Exception):
            current.__suppress_context__ = True


def _classify(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, (_Deadline, subprocess.TimeoutExpired)):
        return ("timeout", None)
    if isinstance(exc, _CaptureLimit):
        return ("capture_overflow", None)
    if isinstance(exc, _StreamFailure):
        return ("stream_failure", None)
    if isinstance(exc, KeyboardInterrupt):
        return ("keyboard_interrupt", None)
    if isinstance(exc, asyncio.CancelledError):
        return ("cancelled", None)
    if isinstance(exc, SystemExit):
        code = exc.code
        if isinstance(code, int):
            return ("system_exit", code)
        if code is None:
            return ("system_exit", 0)
        return ("system_exit", 1)
    if isinstance(exc, Exception):
        return ("unexpected", None)
    return ("unexpected", None)


def _raise_clean(
    action: str,
    argv: list[str],
    timeout: float,
    exit_code: int | None,
    *,
    sensitive: bool = False,
    returncode: int | None = None,
    detail: str | None = None,
    graph_error: tuple[str, str] | None = None,
    aadsts_code: str | None = None,
) -> None:
    if action == "timeout":
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
    if action == "keyboard_interrupt":
        raise KeyboardInterrupt()
    if action == "cancelled":
        raise asyncio.CancelledError()
    if action == "system_exit":
        if exit_code is None:
            raise SystemExit()
        raise SystemExit(exit_code)
    if action == "capture_overflow":
        if sensitive:
            raise CliInvocationError(argv, -1, _sensitive_failure_detail(""), sensitive=True)
        raise CliInvocationError(
            argv,
            -1,
            "subprocess output exceeded the capture size limit",
        )
    if action == "stream_failure":
        if sensitive:
            raise CliInvocationError(argv, -1, _sensitive_failure_detail(""), sensitive=True)
        raise OSError("subprocess output reader failed")
    if action == "unexpected":
        if sensitive:
            raise CliInvocationError(argv, -1, _sensitive_failure_detail(""), sensitive=True)
        raise CliInvocationError(argv, -1, "unexpected error while reading subprocess output")
    if action == "aadsts":
        assert aadsts_code is not None
        raise AADSTSError(aadsts_code, detail or _SENSITIVE_OUTPUT_SUPPRESSED)
    if action == "cli_invocation":
        assert returncode is not None and detail is not None
        raise CliInvocationError(
            argv,
            returncode,
            detail,
            sensitive=sensitive,
            graph_error=graph_error,
        )
    raise RuntimeError(f"unknown clean-raise action: {action!r}")


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?0-9;]*[ -/]*[@-~]")

_DEVICE_LOGIN_LINE_RE = re.compile(
    r"^To sign in, use a web browser to open (?:the page )?"
    r"(?P<url>https://login\.microsoft\.com/device|https://microsoft\.com/devicelogin)"
    r" and enter the code "
    r"(?P<code>[A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z0-9]{8}|[A-Z0-9]{9})"
    r"(?: to authenticate)?\.?\s*$",
    re.IGNORECASE | re.ASCII,
)

# Recognize only the CLI's consent-wait prefix; never echo its trailing data.
_ADMIN_CONSENT_WAIT_RE = re.compile(r"^Opening browser for admin consent(?:\.{3}|\s|$)")
_ADMIN_CONSENT_WAIT_NOTICE = (
    "a365 is waiting for admin consent (a browser may open). Complete consent, "
    "or run `hermes a365 consent` from another shell; details are in the a365 setup log.\n"
)

_SENSITIVE_GRAPH_ERROR_CODES = frozenset(
    {
        "Request_ResourceNotFound",
        "ResourceNotFound",
        "ErrorItemNotFound",
        "itemNotFound",
        "Authorization_RequestDenied",
        "AccessDenied",
        "Forbidden",
        "InsufficientPrivileges",
    }
)
_SENSITIVE_GRAPH_SAFE_MESSAGES: dict[str, str] = {
    "Request_ResourceNotFound": "The requested resource does not exist.",
    "ResourceNotFound": "The requested resource does not exist.",
    "ErrorItemNotFound": "The requested resource does not exist.",
    "itemNotFound": "The requested resource does not exist.",
    "Authorization_RequestDenied": "Insufficient privileges to complete the operation.",
    "AccessDenied": "Access was denied.",
    "Forbidden": "Access was denied.",
    "InsufficientPrivileges": "Insufficient privileges to complete the operation.",
}
_GRAPH_ERROR_CODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")

_CANONICAL_URLS = {
    "https://login.microsoft.com/device": "https://login.microsoft.com/device",
    "https://microsoft.com/devicelogin": "https://microsoft.com/devicelogin",
}


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _line_content(line: str) -> str:
    """Strip terminators; keep interior text for matching."""
    return _strip_ansi(line).rstrip("\r\n")


def _parse_device_login_line(line: str, *, eof: bool = False) -> tuple[str, str] | None:
    """Return canonical URL and user code for one complete device-login line."""
    if not eof and not line.endswith(("\n", "\r")):
        return None
    content = _line_content(line)
    if not content:
        return None
    if any(ch in content for ch in ('"', "'", "{", "}", "[", "]")):
        return None
    match = _DEVICE_LOGIN_LINE_RE.match(content)
    if not match:
        return None
    url = match.group("url")
    canonical = _CANONICAL_URLS.get(url.lower())
    if canonical is None:
        return None
    code = match.group("code").upper()
    return canonical, code


def _emit_device_login(url: str, code: str, *, emitted: set[tuple[str, str]]) -> None:
    key = (url, code)
    if key in emitted:
        return
    emitted.add(key)
    sys.stdout.write(f"To sign in, open {url} and enter the code {code}.\n")
    sys.stdout.flush()


def _try_emit_device_login_line(
    line: str, *, emitted: set[tuple[str, str]], eof: bool = False
) -> None:
    parsed = _parse_device_login_line(line, eof=eof)
    if parsed is not None:
        _emit_device_login(parsed[0], parsed[1], emitted=emitted)


def _optional_safe_failure_tail(text: str) -> str | None:
    """Return a short operator-safe suffix, or ``None`` when unsure."""
    _ = text
    return None


def _sensitive_failure_detail(stripped: str) -> str:
    tail = _optional_safe_failure_tail(stripped)
    if tail:
        return f"{_SENSITIVE_OUTPUT_SUPPRESSED} ({tail})"
    return _SENSITIVE_OUTPUT_SUPPRESSED


def _extract_sensitive_graph_error(text: str) -> tuple[str, str] | None:
    """Pull an allowlisted Graph ``error.code`` and a fixed safe message from captured text."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    code_raw = error.get("code")
    if not isinstance(code_raw, str):
        return None
    code = code_raw.strip()
    if code not in _SENSITIVE_GRAPH_ERROR_CODES:
        return None
    if not _GRAPH_ERROR_CODE_ID_RE.match(code):
        return None
    safe_message = _SENSITIVE_GRAPH_SAFE_MESSAGES.get(code)
    if safe_message is None:
        return None
    return code, safe_message


@dataclass
class _LineBuffer:
    pending: str = ""
    pending_bytes: int = 0
    total_bytes: int = 0
    _trailing_cr_bytes: int = 0
    _decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )

    def clear(self) -> None:
        self.pending = ""
        self.pending_bytes = self.total_bytes = self._trailing_cr_bytes = 0
        self._decoder.reset()

    def _decode(self, chunk: bytes, *, final: bool = False) -> None:
        previous = len(self._decoder.getstate()[0])
        text = self._decoder.decode(chunk, final=final)
        size = len(text.encode("utf-8"))
        self.pending += text
        self.pending_bytes += size
        # Include bytes still held by the incremental UTF-8 decoder.
        self.total_bytes += size + len(self._decoder.getstate()[0]) - previous

    def _check_line(self, size: int) -> None:
        if size > _MAX_LINE_BYTES:
            self.clear()
            raise _CaptureLimit()

    def feed(self, chunk: bytes) -> list[str]:
        self._decode(chunk)
        # A CR already emitted at a chunk boundary may acquire its LF now.
        if self._trailing_cr_bytes and self.pending.startswith("\n"):
            self._check_line(self._trailing_cr_bytes + 1)
        self._trailing_cr_bytes = 0
        lines: list[str] = []
        while True:
            split_at = -1
            split_len = 0
            for sep in ("\r\n", "\n", "\r"):
                idx = self.pending.find(sep)
                if idx >= 0 and (split_at < 0 or idx < split_at):
                    split_at = idx
                    split_len = len(sep)
            if split_at < 0:
                self._check_line(self.pending_bytes + len(self._decoder.getstate()[0]))
                break
            line = self.pending[: split_at + split_len]
            size = len(line.encode("utf-8"))
            self._check_line(size)
            lines.append(line)
            self.pending = self.pending[split_at + split_len :]
            self.pending_bytes -= size
            self._trailing_cr_bytes = size if line.endswith("\r") else 0
        if self.pending or self._decoder.getstate()[0]:
            self._trailing_cr_bytes = 0
        return lines

    def flush_eof(self) -> str:
        self._decode(b"", final=True)
        self._check_line(self.pending_bytes)
        tail = self.pending
        self.pending = ""
        self.pending_bytes = 0
        return tail


def _clear_capture_buffers(captured: list[str], buffers: dict[str, _LineBuffer]) -> None:
    captured.clear()
    for buffer in buffers.values():
        buffer.clear()


_OutputEvent = tuple[str, bytes | Literal["reader_failure", "stdin_failure"] | None]


def _queue_put(
    out_q: queue.Queue[_OutputEvent],
    item: _OutputEvent,
    stop: threading.Event,
) -> None:
    while True:
        try:
            out_q.put(item, timeout=0.05)
            return
        except queue.Full:
            if stop.is_set():
                return


def _pump_stream(
    fd: int,
    name: str,
    out_q: queue.Queue[_OutputEvent],
    stop: threading.Event,
) -> None:
    chunk = b""
    try:
        if os.name == "posix":
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            try:
                while not stop.is_set():
                    if not poll.poll(50):
                        continue
                    chunk = os.read(fd, _READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    _queue_put(out_q, (name, chunk), stop)
            finally:
                with contextlib.suppress(OSError, ValueError):
                    poll.unregister(fd)
        else:
            while not stop.is_set():
                chunk = os.read(fd, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                _queue_put(out_q, (name, chunk), stop)
    except BaseException as exc:
        _release(exc)
        _queue_put(out_q, (name, "reader_failure"), stop)
    else:
        _queue_put(out_q, (name, None), stop)
    finally:
        chunk = b""


def _feed_stdin(pump: _Pump, stdin_input: str) -> None:
    proc = pump.proc
    assert proc is not None and proc.stdin is not None
    pending = memoryview(stdin_input.encode("utf-8"))
    stdin_input = ""
    try:
        while pending and not pump.stop.is_set():
            if time.monotonic() >= pump.deadline:
                # Leave stdin open: closing it here would send EOF on partial
                # input. The deadline path reaps the child and closes the pipe.
                pump.stdin_truncated = True
                break
            written = proc.stdin.write(pending)
            if written is None or written <= 0:
                raise OSError("subprocess stdin write failed")
            pending = pending[written:]
    except BrokenPipeError:
        pass
    except BaseException as exc:
        _release(exc)
        _queue_put(pump.out_q, ("stdin", "stdin_failure"), pump.stop)
    finally:
        pending.release()
        pending = memoryview(b"")
        if not pump.stdin_truncated:
            with contextlib.suppress(OSError):
                proc.stdin.close()


@dataclass
class _Pump:
    proc: subprocess.Popen[bytes] | None
    argv: list[str]
    timeout: float
    streams: tuple[str, ...]
    echo: Literal["all", "device_login"]
    sensitive: bool
    stop: threading.Event
    out_q: queue.Queue[_OutputEvent]
    threads: list[threading.Thread]
    buffers: dict[str, _LineBuffer]
    eof_seen: dict[str, bool]
    emitted_logins: set[tuple[str, str]]
    captured: list[str]
    deadline: float
    thread_streams: list[tuple[threading.Thread, str]] = field(default_factory=list)
    reaped: bool = False
    capture_bytes: int = 0
    termination_started: bool = False
    stdin_truncated: bool = False


def _reap(pump: _Pump, failure: BaseException | None = None) -> None:
    interrupted: tuple[str, int | None] | None = None
    try:
        while not pump.reaped:
            try:
                _reap_resources(pump)
            except (KeyboardInterrupt, asyncio.CancelledError, SystemExit) as exc:
                if interrupted is None:
                    interrupted = _classify(exc)
                _release(exc)
                # Every operation is repeatable. A partly completed soft stop
                # resumes at hard kill, then repeats waits and resource cleanup.
    finally:
        if failure is not None:
            _release(failure)
            failure = None
    if interrupted is not None:
        _raise_clean(
            interrupted[0], pump.argv, pump.timeout, interrupted[1], sensitive=pump.sensitive
        )


def _reap_resources(pump: _Pump) -> None:
    pump.stop.set()
    proc = pump.proc
    if proc is not None:
        if proc.poll() is None and not pump.termination_started:
            pump.termination_started = True
            with contextlib.suppress(OSError):
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(_REAP_TERMINATE_GRACE_SECONDS)
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(5)
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(5)
        # Killing the child unblocks a writer stuck in an unbuffered pipe write.
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        if os.name == "posix":
            for thread in pump.threads:
                if thread.ident is not None:
                    thread.join(timeout=1.0)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        if os.name != "posix":
            for thread in pump.threads:
                if thread.ident is not None:
                    thread.join(timeout=1.0)
    # Producers have stopped: drain again so no late EOF/data survives cleanup.
    while True:
        try:
            pump.out_q.get_nowait()
        except queue.Empty:
            break
    _clear_capture_buffers(pump.captured, pump.buffers)
    pump.emitted_logins.clear()
    pump.capture_bytes = 0
    if (
        proc is not None
        and (
            proc.returncode is None
            or any(
                pipe is not None and not pipe.closed
                for pipe in (proc.stdin, proc.stdout, proc.stderr)
            )
        )
    ) or any(thread.is_alive() for thread in pump.threads):
        raise _StreamFailure()
    pump.reaped = True


def _run_pumped_output(pump: _Pump) -> tuple[int, str]:
    proc = pump.proc
    assert proc is not None and proc.stdout is not None
    streams = pump.streams
    buffers = pump.buffers
    captured = pump.captured
    echo = pump.echo
    out_q = pump.out_q
    eof_seen = pump.eof_seen
    emitted_logins = pump.emitted_logins
    deadline = pump.deadline
    consent_notice_emitted = False

    def _emit_sensitive_notice(line: str, *, eof: bool) -> None:
        nonlocal consent_notice_emitted
        _try_emit_device_login_line(line, emitted=emitted_logins, eof=eof)
        if not consent_notice_emitted and _ADMIN_CONSENT_WAIT_RE.match(_line_content(line)):
            consent_notice_emitted = True
            sys.stdout.write(_ADMIN_CONSENT_WAIT_NOTICE)
            sys.stdout.flush()

    def _handle_complete_line(line: str) -> None:
        captured.append(line)
        if echo == "all":
            sys.stdout.write(line)
            sys.stdout.flush()
        elif echo == "device_login":
            _emit_sensitive_notice(line, eof=False)

    def _flush_eof_tail(name: str) -> None:
        buffer = buffers[name]
        previous = buffer.total_bytes
        tail = buffer.flush_eof()
        _count_bytes(buffer.total_bytes - previous)
        if not tail:
            return
        captured.append(tail)
        if echo == "all":
            sys.stdout.write(tail)
            sys.stdout.flush()
        elif echo == "device_login":
            _emit_sensitive_notice(tail, eof=True)

    def _count_bytes(size: int) -> None:
        pump.capture_bytes += size
        if pump.capture_bytes > _MAX_TOTAL_CAPTURE_BYTES:
            _clear_capture_buffers(captured, buffers)
            raise _CaptureLimit()

    def _drain_chunk(name: str, chunk: bytes) -> None:
        buffer = buffers[name]
        previous = buffer.total_bytes
        lines = buffer.feed(chunk)
        _count_bytes(buffer.total_bytes - previous)
        for line in lines:
            _handle_complete_line(line)

    def _process_queue_item(
        name: str, payload: bytes | Literal["reader_failure", "stdin_failure"] | None
    ) -> None:
        if isinstance(payload, str):
            raise _StreamFailure()
        if payload is None:
            eof_seen[name] = True
            _flush_eof_tail(name)
            return
        _drain_chunk(name, payload)

    hit_deadline = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            hit_deadline = True
            break
        try:
            name, payload = out_q.get(timeout=min(0.05, max(remaining, 0.0)))
        except queue.Empty:
            for thread, stream_name in pump.thread_streams:
                if not thread.is_alive() and not eof_seen[stream_name]:
                    raise _StreamFailure() from None
            if proc.poll() is not None and all(eof_seen[s] for s in streams):
                while True:
                    try:
                        name, payload = out_q.get_nowait()
                    except queue.Empty:
                        break
                    _process_queue_item(name, payload)
                break
            continue
        _process_queue_item(name, payload)
        if all(eof_seen[s] for s in streams) and out_q.empty() and proc.poll() is not None:
            break

    if hit_deadline or pump.stdin_truncated:
        raise _Deadline()

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    return proc.returncode or 0, "".join(captured)


def _run_child_output(
    argv: list[str],
    *,
    timeout: float,
    stdin_input: str | None,
    echo: Literal["all", "device_login"],
    merge_stderr: bool,
    sensitive: bool,
) -> tuple[int, str]:
    streams: tuple[str, ...] = ("stdout",) if merge_stderr else ("stdout", "stderr")
    pump = _Pump(
        proc=None,
        argv=argv,
        timeout=timeout,
        streams=streams,
        echo=echo,
        sensitive=sensitive,
        stop=threading.Event(),
        out_q=queue.Queue(maxsize=_QUEUE_MAXSIZE),
        threads=[],
        buffers={name: _LineBuffer() for name in streams},
        eof_seen={name: False for name in streams},
        emitted_logins=set(),
        captured=[],
        deadline=time.monotonic() + timeout,
    )
    failure = None
    try:
        pump.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            bufsize=0,
            preexec_fn=_owner_only_child_setup if os.name == "posix" else None,
        )
        proc = pump.proc
        assert proc.stdout is not None
        for name in streams:
            pipe = getattr(proc, name)
            assert pipe is not None
            thread = threading.Thread(
                target=_pump_stream,
                args=(pipe.fileno(), name, pump.out_q, pump.stop),
                daemon=True,
            )
            # Register before start: an interrupt may follow the native start.
            # Cleanup checks ident and joins only threads that actually started.
            pump.threads.append(thread)
            pump.thread_streams.append((thread, name))
            thread.start()
        if stdin_input is not None:
            writer = threading.Thread(target=_feed_stdin, args=(pump, stdin_input), daemon=True)
            pump.threads.append(writer)
            writer.start()
        stdin_input = None
        return _run_pumped_output(pump)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        stdin_input = None
        _reap(pump, failure)


def _run_sensitive_streaming(
    argv: list[str],
    *,
    timeout: float,
    stdin_input: str | None = None,
) -> tuple[int, str]:
    return _run_child_output(
        argv,
        timeout=timeout,
        stdin_input=stdin_input,
        echo="device_login",
        merge_stderr=False,
        sensitive=True,
    )


def _run_streaming(
    argv: list[str],
    *,
    timeout: float,
    stdin_input: str | None = None,
) -> tuple[int, str]:
    """Run ``argv`` to completion, streaming combined output to ``sys.stdout``.

    Returns ``(returncode, combined_output)``. Raises
    :class:`subprocess.TimeoutExpired` without captured output when the
    wall-clock deadline is hit.

    Child stderr is merged into stdout so interleaved CLI progress keeps
    chronological order in the captured stream.
    """
    return _run_child_output(
        argv,
        timeout=timeout,
        stdin_input=stdin_input,
        echo="all",
        merge_stderr=True,
        sensitive=False,
    )


def get_mutator() -> Mutator:
    return A365CliMutator()
