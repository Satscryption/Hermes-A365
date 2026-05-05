"""Thin wrapper over the real Microsoft.Agents.A365.DevTools.Cli (binary ``a365``).

v0.2 design: instead of one named protocol method per CLI verb (the v0.1
``setup_app`` / ``fic_configure`` / ``setup_blueprint(file_path=…)`` /
``create_instance`` / ``deploy`` / ``cleanup(kind, …)`` set, all of which
targeted speculative CLI shapes that don't exist in the GA build), the
:class:`Mutator` here exposes a single :meth:`Mutator.run` that takes an
``argv`` list. Per-script appliers build the argv themselves and parse
the captured output. This is closer to how the real CLI is invoked,
keeps the protocol stable as the CLI's flag set evolves, and makes
test fakes trivial (record argv, return scripted output).

See ``references/a365-cli-reference.md`` for the verified command
surface (CLI v1.1.171, .NET only — no npm variant).

AADSTS handling lives here too: the CLI surfaces Microsoft auth errors
embedded in its stdout/stderr, so every :meth:`run` call screens for
``AADSTS<code>`` tokens and raises :class:`AADSTSError` on a non-zero
exit when one is found.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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

    def run(self, argv: list[str], *, timeout: float = 900.0) -> RunResult: ...


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

    def run(self, argv: list[str], *, timeout: float = 900.0) -> RunResult:
        if not self.available:
            raise CliInvocationError(argv, -1, f"{A365_CLI_BINARY} not on PATH")
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            combined = (proc.stderr + proc.stdout).strip()
            match = _AADSTS_RE.search(combined)
            if match:
                raise AADSTSError(match.group(0), combined)
            raise CliInvocationError(argv, proc.returncode, combined)
        return RunResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def get_mutator() -> Mutator:
    return A365CliMutator()
