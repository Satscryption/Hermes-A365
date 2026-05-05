"""hermes a365 secrets — OS-keychain wrapper for the T2 client secret.

Spec: SPEC.md §6.2, §6.5, §6.7, §7.1. The skill stores the T2 confidential-
client secret in the OS keychain under service ``hermes-a365`` with account
``<tenant>.<appId>``. The secret is **never** written to disk.

Backends
--------
- macOS: ``security`` command (Security framework)
- Linux: ``secret-tool`` (libsecret)
- Windows: not supported in v0.1 (per SPEC §10 Q3)

Trade-off note
--------------
``security add-generic-password`` only accepts the secret via ``-w`` (argv),
making it briefly visible in the process table during the call. This is the
platform-standard interface; mitigations are short runtime and immediate
exit. The Linux backend pipes the secret via stdin, which is preferred.

Programmatic use::

    from keychain import store_secret, get_secret, delete_secret
    store_secret("contoso.onmicrosoft.com", "9e2d…", "shh")
    secret = get_secret("contoso.onmicrosoft.com", "9e2d…")
    delete_secret("contoso.onmicrosoft.com", "9e2d…")

CLI use::

    python scripts/secrets.py store --tenant=… --app-id=… --secret -   # stdin
    python scripts/secrets.py store --tenant=… --app-id=…              # interactive prompt
    python scripts/secrets.py get    --tenant=… --app-id=…
    python scripts/secrets.py delete --tenant=… --app-id=…
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from typing import Protocol

SERVICE = "hermes-a365"

# Platform-specific exit codes the backends rely on.
_MACOS_NOT_FOUND = 44  # `security` returns 44 when the item doesn't exist


class KeychainError(RuntimeError):
    """Raised when a keychain operation fails for an unexpected reason."""


class KeychainBackend(Protocol):
    """Protocol for keychain backends. The ``name`` attribute is informational."""

    name: str

    def store(self, account: str, secret: str) -> None: ...
    def get(self, account: str) -> str | None: ...
    def delete(self, account: str) -> bool: ...


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
        proc = subprocess.run(
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
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise KeychainError(
                f"security add-generic-password failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )

    def get(self, account: str) -> str | None:
        # -w: print password to stdout
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                SERVICE,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
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
        proc = subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                SERVICE,
                "-a",
                account,
            ],
            capture_output=True,
            text=True,
            check=False,
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
        proc = subprocess.run(
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
            input=secret,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise KeychainError(
                f"secret-tool store failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )

    def get(self, account: str) -> str | None:
        proc = subprocess.run(
            [
                "secret-tool",
                "lookup",
                "service",
                SERVICE,
                "account",
                account,
            ],
            capture_output=True,
            text=True,
            check=False,
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
        proc = subprocess.run(
            [
                "secret-tool",
                "clear",
                "service",
                SERVICE,
                "account",
                account,
            ],
            capture_output=True,
            text=True,
            check=False,
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
    - ``--secret <value>`` → use the literal value
    - omitted → interactive ``getpass`` prompt
    """
    if value == "-":
        return sys.stdin.read().rstrip("\n")
    if value is not None:
        return value
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
    sys.stdout.write(value)
    if not value.endswith("\n"):
        sys.stdout.write("\n")
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

    sub.add_parser("get", parents=[common], help="retrieve a secret to stdout")
    sub.add_parser("delete", parents=[common], help="remove a secret")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = get_backend()
    except KeychainError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "store":
            return _cmd_store(args, backend)
        if args.cmd == "get":
            return _cmd_get(args, backend)
        if args.cmd == "delete":
            return _cmd_delete(args, backend)
    except KeychainError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    parser.error(f"unknown subcommand: {args.cmd}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
