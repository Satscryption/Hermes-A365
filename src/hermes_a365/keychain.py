"""hermes a365 secrets — OS-keychain wrapper for the client secrets.

Stores a client secret in the OS keychain under service ``hermes-a365``
with account ``<tenant>.<appId>``. Both #19 identities are keyed this
way: the Path A agent blueprint secret and the Path B Bot Framework one.

Storing here is **opt-in and additive** — nothing mirrors a secret into
the keychain automatically, and this store never removes the plaintext
copies (``a365.generated.config.json``, ``~/.hermes/.env``, the per-agent
``.env``, the gateway platform ``extra`` block). It is read back only as
the *miss-fill* tier behind those plaintext sources: see
``hermes_a365.secrets_provider``, where the existing plaintext value
always wins so a stale keychain entry cannot shadow a rotation.

Backends
--------
- macOS: opt-in ``security`` command fallback (disabled by default because
  the command exposes stored values in process metadata)
- Linux: ``secret-tool`` (libsecret)
- Windows: not supported in v0.1 (per SPEC §10 Q3)

Trade-off note
--------------
``security add-generic-password`` only accepts the secret via ``-w`` (argv).
That backend therefore requires the explicit
``HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI=1`` opt-in. The Linux backend
pipes the secret via stdin.

Programmatic use::

    from hermes_a365.keychain import store_secret, get_secret, delete_secret
    store_secret("contoso.onmicrosoft.com", "9e2d…", "shh")
    secret = get_secret("contoso.onmicrosoft.com", "9e2d…")
    delete_secret("contoso.onmicrosoft.com", "9e2d…")

CLI use::

    python -m hermes_a365.keychain store --tenant=… --app-id=… --secret -   # stdin
    python -m hermes_a365.keychain store --tenant=… --app-id=…              # interactive prompt
    python -m hermes_a365.keychain get    --tenant=… --app-id=… --output-fd=3
    python -m hermes_a365.keychain delete --tenant=… --app-id=…
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import warnings
from typing import Protocol

SERVICE = "hermes-a365"

# Platform-specific exit codes the backends rely on.
_MACOS_NOT_FOUND = 44  # `security` returns 44 when the item doesn't exist

# #19: bound every keychain subprocess. These now run on a runtime credential
# read (the secrets provider), and on the gateway that read happens inside the
# asyncio lifecycle. An un-timed `security`/`secret-tool` call that hangs
# (locked keychain prompt, stuck D-Bus/gnome-keyring) could stall indefinitely.
# Lookup timeouts become provider misses; CLI writes/deletes fail cleanly.
_BACKEND_TIMEOUT_SECONDS = 10.0


class KeychainError(RuntimeError):
    """Raised when a keychain operation fails for an unexpected reason."""


class KeychainTimeoutError(KeychainError):
    """Raised when an OS-keychain command exceeds the bounded timeout."""


class KeychainBackend(Protocol):
    """Protocol for keychain backends. The ``name`` attribute is informational."""

    name: str

    def store(self, account: str, secret: str) -> None: ...
    def get(self, account: str) -> str | None: ...
    def delete(self, account: str) -> bool: ...


def _run_keychain_command(
    argv: list[str],
    *,
    input_text: str | None = None,
    sensitive_argv_indices: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a keychain command and sanitize timeout failures.

    On macOS the store command necessarily carries the secret in ``argv``.
    ``subprocess.TimeoutExpired`` includes that argv in its message, so it
    must never cross this boundary or an uncaught traceback can persist the
    credential. The replacement exception contains only a fixed operation
    label and is caught by the CLI as a normal ``KeychainError``.
    """
    try:
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=_BACKEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        # ``raise ... from None`` suppresses the context in a standard
        # traceback, but exception collectors may still inspect it. Redact
        # the original TimeoutExpired object and its shared argv list too.
        for index in sensitive_argv_indices:
            if -len(argv) <= index < len(argv):
                argv[index] = "<redacted>"
        exc.cmd = list(argv)
        exc.args = (exc.cmd, exc.timeout)
        exc.output = None
        exc.stderr = None
        raise KeychainTimeoutError(
            f"OS-keychain operation timed out after {_BACKEND_TIMEOUT_SECONDS:g}s"
        ) from None


# ---------------------------------------------------------------------------
# Account naming
# ---------------------------------------------------------------------------


def account_name(tenant: str, app_id: str) -> str:
    """Compose the keychain account name for a (tenant, app_id) pair.

    Format: ``<tenant>.<appId>``. Both inputs must be non-empty.
    """
    if not tenant or not app_id:
        raise ValueError("tenant and app_id must both be non-empty")
    if "/" in tenant or "/" in app_id:
        # Defensive — neither field should contain slashes; reject early so
        # the secret never ends up under a corrupted account name.
        raise ValueError("tenant and app_id must not contain '/'")
    return f"{tenant}.{app_id}"


# ---------------------------------------------------------------------------
# macOS backend
# ---------------------------------------------------------------------------


class MacOSBackend:
    name = "macos-security"

    def store(self, account: str, secret: str) -> None:
        # -U: update if exists. The secret is passed via -w (argv) — the only
        # interface `security` exposes for generic passwords.
        proc = _run_keychain_command(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                SERVICE,
                "-a",
                account,
                "-w",
                secret,
            ],
            sensitive_argv_indices=(-1,),
        )
        if proc.returncode != 0:
            raise KeychainError(
                f"security add-generic-password failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )

    def get(self, account: str) -> str | None:
        # -w: print password to stdout
        proc = _run_keychain_command(
            [
                "security",
                "find-generic-password",
                "-s",
                SERVICE,
                "-a",
                account,
                "-w",
            ]
        )
        if proc.returncode == 0:
            # Trailing newline is added by `security`; strip it.
            return proc.stdout.rstrip("\n")
        if proc.returncode == _MACOS_NOT_FOUND:
            return None
        raise KeychainError(
            f"security find-generic-password failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    def delete(self, account: str) -> bool:
        proc = _run_keychain_command(
            [
                "security",
                "delete-generic-password",
                "-s",
                SERVICE,
                "-a",
                account,
            ]
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == _MACOS_NOT_FOUND:
            return False
        raise KeychainError(
            f"security delete-generic-password failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Linux backend
# ---------------------------------------------------------------------------


class LinuxBackend:
    name = "libsecret"

    def store(self, account: str, secret: str) -> None:
        # secret-tool reads the secret from stdin — preferred over argv.
        proc = _run_keychain_command(
            [
                "secret-tool",
                "store",
                "--label",
                f"{SERVICE} {account}",
                "service",
                SERVICE,
                "account",
                account,
            ],
            input_text=secret,
        )
        if proc.returncode != 0:
            raise KeychainError(
                f"secret-tool store failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )

    def get(self, account: str) -> str | None:
        proc = _run_keychain_command(
            [
                "secret-tool",
                "lookup",
                "service",
                SERVICE,
                "account",
                account,
            ]
        )
        # secret-tool prints the secret on stdout with no trailing newline
        # when found, exits 1 with empty stdout when missing.
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        if proc.returncode == 1 and not proc.stdout:
            return None
        raise KeychainError(
            f"secret-tool lookup failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    def delete(self, account: str) -> bool:
        # `secret-tool clear` is silent on success regardless of whether the
        # entry existed. We probe with `get` first so the return value
        # matches the macOS backend's semantics.
        existed = self.get(account) is not None
        proc = _run_keychain_command(
            [
                "secret-tool",
                "clear",
                "service",
                SERVICE,
                "account",
                account,
            ]
        )
        if proc.returncode != 0:
            raise KeychainError(
                f"secret-tool clear failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return existed


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def get_backend() -> KeychainBackend:
    """Return the appropriate backend for the current platform.

    Raises ``KeychainError`` if the platform is unsupported or the required
    binary is missing.
    """
    if sys.platform == "darwin":
        if not shutil.which("security"):
            raise KeychainError("`security` command not found on PATH (macOS)")
        if os.environ.get("HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI", "").lower() not in {
            "1", "true", "yes", "on"
        }:
            raise KeychainError(
                "the macOS `security` CLI exposes stored values in process metadata; "
                "set HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI=1 to opt in"
            )
        warnings.warn(
            "using the opt-in macOS security CLI backend; secret values may be visible "
            "in child process metadata",
            RuntimeWarning,
            stacklevel=2,
        )
        return MacOSBackend()
    if sys.platform.startswith("linux"):
        if not shutil.which("secret-tool"):
            raise KeychainError("`secret-tool` not found (install libsecret-tools / libsecret-1-0)")
        return LinuxBackend()
    raise KeychainError(f"unsupported platform: {sys.platform} (v0.1 supports macOS + Linux)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def store_secret(
    tenant: str,
    app_id: str,
    secret: str,
    *,
    backend: KeychainBackend | None = None,
) -> None:
    """Store the T2 client secret for (tenant, app_id) in the OS keychain."""
    if not secret:
        raise ValueError("refusing to store an empty secret")
    (backend or get_backend()).store(account_name(tenant, app_id), secret)


def get_secret(
    tenant: str,
    app_id: str,
    *,
    backend: KeychainBackend | None = None,
) -> str | None:
    """Return the stored secret, or ``None`` if no entry exists."""
    return (backend or get_backend()).get(account_name(tenant, app_id))


def delete_secret(
    tenant: str,
    app_id: str,
    *,
    backend: KeychainBackend | None = None,
) -> bool:
    """Delete the stored secret. Returns ``True`` if it existed and was removed."""
    return (backend or get_backend()).delete(account_name(tenant, app_id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_secret_arg(value: str | None, *, prompt_label: str) -> str:
    """Resolve the secret value for the ``store`` subcommand.

    - ``--secret -`` → read from stdin (everything up to EOF, trailing newline trimmed)
    - literal argv values are rejected because process metadata is observable
    - omitted → interactive ``getpass`` prompt
    """
    if value == "-":
        return sys.stdin.read().rstrip("\n")
    if value is not None:
        raise ValueError("literal --secret values are refused; use --secret - or the prompt")
    return getpass.getpass(f"secret for {prompt_label}: ")


def _cmd_store(args: argparse.Namespace, backend: KeychainBackend) -> int:
    label = f"{args.tenant}.{args.app_id}"
    secret = _read_secret_arg(args.secret, prompt_label=label)
    if not secret:
        print("ERROR: no secret provided", file=sys.stderr)
        return 2
    store_secret(args.tenant, args.app_id, secret, backend=backend)
    print(f"stored: {SERVICE}/{label} via {backend.name}", file=sys.stderr)
    return 0


def _cmd_get(args: argparse.Namespace, backend: KeychainBackend) -> int:
    value = get_secret(args.tenant, args.app_id, backend=backend)
    if value is None:
        print(f"not found: {args.tenant}.{args.app_id}", file=sys.stderr)
        return 1
    if args.output_fd is None or args.output_fd <= 2:
        print(
            "ERROR: --output-fd must be an inherited descriptor greater than 2; "
            "secrets are not written to standard streams",
            file=sys.stderr,
        )
        return 2
    os.write(args.output_fd, value.encode("utf-8"))
    return 0


def _cmd_delete(args: argparse.Namespace, backend: KeychainBackend) -> int:
    deleted = delete_secret(args.tenant, args.app_id, backend=backend)
    label = f"{args.tenant}.{args.app_id}"
    if deleted:
        print(f"deleted: {label} via {backend.name}", file=sys.stderr)
        return 0
    print(f"not found: {label}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="hermes a365 secrets — OS-keychain wrapper for T2 client secrets.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tenant", required=True, help="tenant id or domain")
    common.add_argument("--app-id", required=True, dest="app_id", help="T2 application id")

    p_store = sub.add_parser("store", parents=[common], help="store a secret")
    p_store.add_argument(
        "--secret",
        help="secret value, or '-' to read from stdin (default: interactive prompt)",
    )

    p_get = sub.add_parser(
        "get", parents=[common], help="retrieve a secret to an inherited file descriptor"
    )
    p_get.add_argument("--output-fd", type=int, help="already-open inherited output fd")
    sub.add_parser("delete", parents=[common], help="remove a secret")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = get_backend()
    except (KeychainError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "store":
            return _cmd_store(args, backend)
        if args.cmd == "get":
            return _cmd_get(args, backend)
        if args.cmd == "delete":
            return _cmd_delete(args, backend)
    except (KeychainError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    parser.error(f"unknown subcommand: {args.cmd}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
