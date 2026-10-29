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

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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
        if sensitive:
            try:
                returncode, combined = _run_sensitive_streaming(
                    argv, timeout=timeout, stdin_input=stdin_input
                )
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout) from None
        else:
            try:
                returncode, combined = _run_streaming(
                    argv, timeout=timeout, stdin_input=stdin_input
                )
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout) from None
        if returncode != 0:
            stripped = combined.strip()
            match = _AADSTS_RE.search(stripped)
            detail = _sensitive_failure_detail(stripped) if sensitive else stripped
            graph_error = _extract_sensitive_graph_error(stripped) if sensitive else None
            if match:
                code = match.group(0)
                stripped = ""
                combined = ""
                raise AADSTSError(code, detail)
            stripped = ""
            combined = ""
            raise CliInvocationError(
                argv,
                returncode,
                detail,
                sensitive=sensitive,
                graph_error=graph_error,
            )
        return RunResult(
            argv=list(argv),
            returncode=returncode,
            stdout=combined,
            stderr="",
        )


# ---------------------------------------------------------------------------
# Subprocess helpers — streaming (default) and sensitive device-login echo
# ---------------------------------------------------------------------------

_SENSITIVE_OUTPUT_SUPPRESSED = "<output suppressed: sensitive command may emit credential material>"

_READ_CHUNK_BYTES = 4096

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?0-9;]*[ -/]*[@-~]")

_DEVICE_LOGIN_LINE_RE = re.compile(
    r"^To sign in, use a web browser to open (?:the page )?"
    r"(?P<url>https://login\.microsoft\.com/device|https://microsoft\.com/devicelogin)"
    r" and enter the code "
    r"(?P<code>[A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z0-9]{8}|[A-Z0-9]{9})"
    r"(?: to authenticate)?\.?\s*$",
    re.IGNORECASE | re.ASCII,
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
    """Pull an allowlisted Graph ``error.code`` and redacted message from captured text."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
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
    message_raw = error.get("message")
    message = str(message_raw) if isinstance(message_raw, str) else ""
    from hermes_a365.entra_state import redact

    return code, redact(message)


@dataclass
class _LineBuffer:
    pending: str = ""

    def feed(self, chunk: bytes) -> list[str]:
        self.pending += chunk.decode("utf-8", errors="replace")
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
                break
            lines.append(self.pending[: split_at + split_len])
            self.pending = self.pending[split_at + split_len :]
        return lines


def _pump_stream(fd: int, name: str, out_q: queue.Queue[tuple[str, bytes | None]]) -> None:
    try:
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except OSError:
                break
            if not chunk:
                break
            out_q.put((name, chunk))
    finally:
        out_q.put((name, None))


def _run_pumped_output(
    proc: subprocess.Popen[bytes],
    *,
    argv: list[str],
    timeout: float,
    streams: tuple[str, ...],
    echo: Literal["all", "device_login"],
) -> tuple[int, str]:
    assert proc.stdout is not None
    out_q: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    fds: list[tuple[int, str]] = [(proc.stdout.fileno(), "stdout")]
    if proc.stderr is not None:
        fds.append((proc.stderr.fileno(), "stderr"))
    threads = [
        threading.Thread(target=_pump_stream, args=(fd, name, out_q), daemon=True)
        for fd, name in fds
    ]
    for thread in threads:
        thread.start()

    buffers = {name: _LineBuffer() for name in streams}
    eof_seen = {name: False for name in streams}
    emitted_logins: set[tuple[str, str]] = set()
    captured: list[str] = []
    deadline = time.monotonic() + timeout

    def _handle_complete_line(line: str) -> None:
        captured.append(line)
        if echo == "all":
            sys.stdout.write(line)
            sys.stdout.flush()
        elif echo == "device_login":
            _try_emit_device_login_line(line, emitted=emitted_logins, eof=False)

    def _flush_eof_tail(name: str) -> None:
        buffer = buffers[name]
        if not buffer.pending:
            return
        tail = buffer.pending
        buffer.pending = ""
        captured.append(tail)
        if echo == "all":
            sys.stdout.write(tail)
            sys.stdout.flush()
        elif echo == "device_login":
            _try_emit_device_login_line(tail, emitted=emitted_logins, eof=True)

    def _drain_chunk(name: str, chunk: bytes) -> None:
        for line in buffers[name].feed(chunk):
            _handle_complete_line(line)

    def _process_queue_item(name: str, payload: bytes | None) -> None:
        if payload is None:
            eof_seen[name] = True
            _flush_eof_tail(name)
            return
        _drain_chunk(name, payload)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            proc.wait(timeout=5)
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        try:
            name, payload = out_q.get(timeout=min(0.05, max(remaining, 0.0)))
        except queue.Empty:
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

    proc.wait(timeout=5)
    proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    for thread in threads:
        thread.join(timeout=1.0)
    return proc.returncode or 0, "".join(captured)


def _run_child_output(
    argv: list[str],
    *,
    timeout: float,
    stdin_input: str | None,
    echo: Literal["all", "device_login"],
    merge_stderr: bool,
) -> tuple[int, str]:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        preexec_fn=_owner_only_child_setup if os.name == "posix" else None,
    )
    if stdin_input is not None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_input.encode("utf-8"))
        finally:
            proc.stdin.close()
    streams: tuple[str, ...] = ("stdout",) if merge_stderr else ("stdout", "stderr")
    return _run_pumped_output(proc, argv=argv, timeout=timeout, streams=streams, echo=echo)


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
    )


def get_mutator() -> Mutator:
    return A365CliMutator()
