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

import os
import re
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Protocol

# Verified in references/a365-cli-reference.md; only .NET ships.
A365_CLI_BINARY = "a365"

# Codes the apply paths react to specifically. Anything else surfaces as a
# generic AADSTSError (which itself is a non-zero exit with a known token).
AADSTS_LICENSE_NOT_PROPAGATED = "AADSTS500011"
AADSTS_CONSENT_REQUIRED = "AADSTS90094"
AADSTS_TOKEN_EXPIRED = "AADSTS70043"
_AADSTS_RE = re.compile(r"AADSTS\d{4,7}")


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

    def __init__(self, argv: list[str], returncode: int, output: str) -> None:
        super().__init__(
            f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed (rc={returncode}): {output}"
        )
        self.argv = argv
        self.returncode = returncode
        self.output = output


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
    ) -> RunResult:
        if not self.available:
            raise CliInvocationError(argv, -1, f"{A365_CLI_BINARY} not on PATH")
        returncode, combined = _run_streaming(
            argv, timeout=timeout, stdin_input=stdin_input
        )
        if returncode != 0:
            stripped = combined.strip()
            match = _AADSTS_RE.search(stripped)
            if match:
                raise AADSTSError(match.group(0), stripped)
            raise CliInvocationError(argv, returncode, stripped)
        return RunResult(
            argv=list(argv),
            returncode=returncode,
            stdout=combined,
            stderr="",
        )


# ---------------------------------------------------------------------------
# Streaming subprocess helper
# ---------------------------------------------------------------------------


def _run_streaming(
    argv: list[str],
    *,
    timeout: float,
    stdin_input: str | None = None,
) -> tuple[int, str]:
    """Run ``argv`` to completion, streaming combined output to ``sys.stdout``.

    Returns ``(returncode, combined_output)``. Raises
    :class:`subprocess.TimeoutExpired` if the wall-clock deadline is hit
    (the partial captured output goes into ``output``).

    Behaviour:

    - ``stderr=STDOUT`` so chronological order is preserved across the
      merged stream (a CLI that prints progress on stdout and warnings
      on stderr would otherwise interleave incoherently).
    - Lines are written to ``sys.stdout`` as they arrive — the operator
      sees device-code prompts and CLI progress in real time.
    - When ``stdin_input`` is provided, the subprocess gets a piped stdin
      pre-fed with that string (then closed). Slice 18w added this so
      ``cleanup`` can answer the GA CLI's "Continue with X cleanup?
      (y/N):" prompts that ``-y`` on the parent doesn't actually
      suppress for subcommands. Default is ``stdin=None`` (inherit
      parent stdin), which preserves the device-code-prompt behaviour
      slice 18j relied on for ``setup`` flows.
    - The captured copy is returned for AADSTS detection in :meth:`run`.

    Tests patch this helper directly rather than reaching into Popen.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if stdin_input is not None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_input)
        finally:
            proc.stdin.close()
    assert proc.stdout is not None  # for type checkers; guaranteed by stdout=PIPE
    buf: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=timeout, output="".join(buf)
                )
            ready, _, _ = select.select([proc.stdout], [], [], min(1.0, remaining))
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break  # EOF
                buf.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            elif proc.poll() is not None:
                tail = proc.stdout.read()
                if tail:
                    buf.append(tail)
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                break
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    proc.wait(timeout=5)
    return proc.returncode, "".join(buf)


def get_mutator() -> Mutator:
    return A365CliMutator()
