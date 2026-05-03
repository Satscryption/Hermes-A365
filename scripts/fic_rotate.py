"""hermes a365 fic rotate — rotate the user-FIC for the T2 confidential client.

Spec: SPEC.md §6.10. Re-issues the user-federated-identity credential
backing the T2 app and updates the OS keychain entry written by
:mod:`register` / :mod:`secrets`. After rotation the activity bridge needs
to be restarted to pick up the new credential — this command surfaces the
reminder; it does not (yet) restart the bridge itself.

Schedule note: A365 user-FICs expire on a tenant-configured cadence
(default 90 days). ``hermes a365 status`` and the ``fic`` doctor check
surface the upcoming expiry.

Default mode is dry-run; ``--apply`` runs the rotation.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from secrets import KeychainBackend, get_backend, store_secret
from typing import Any

from _common import parse_env
from register import AADSTSError, Mutator, get_mutator

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

_REQUIRED_PARENT_KEYS = ("A365_APP_ID", "A365_TENANT_ID")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FicRotateError(RuntimeError):
    """Raised when fic rotate can't proceed (missing parent env, no app id)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _load_skill_env(hermes_home: Path) -> dict[str, str]:
    env_file = hermes_home / ".env"
    if not env_file.exists():
        raise FicRotateError(f"{env_file} does not exist; run `hermes a365 register` first")
    env = parse_env(env_file.read_text())
    missing = [k for k in _REQUIRED_PARENT_KEYS if not env.get(k)]
    if missing:
        raise FicRotateError(
            f"{env_file} missing required keys: {missing}; re-run `hermes a365 register`"
        )
    return env


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class FicRotateResult:
    tenant_id: str
    app_id: str
    rotated: bool
    new_secret_stored: bool
    expires: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan + apply
# ---------------------------------------------------------------------------


def plan_fic_rotate(
    *,
    hermes_home: Path | None = None,
) -> tuple[str, str]:
    """Resolve (tenant_id, app_id) for the rotation. Raises on missing config."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    env = _load_skill_env(hermes_home)
    return env["A365_TENANT_ID"], env["A365_APP_ID"]


def apply_fic_rotate(
    *,
    tenant_id: str,
    app_id: str,
    mutator: Mutator,
    keychain: KeychainBackend,
) -> FicRotateResult:
    """Run the rotation and store the new T2 client secret in the keychain.

    The mutator is expected to return ``{"secret": str, "expires": str?}``.
    A response without ``secret`` raises :class:`FicRotateError` since the
    rotation is meaningless if we can't capture the new credential.
    """
    response = mutator.fic_rotate(app_id=app_id)
    new_secret = response.get("secret") or response.get("clientSecret")
    expires = response.get("expires") or response.get("expiresAt")

    if not new_secret:
        raise FicRotateError(
            f"fic rotate returned no secret; cannot update keychain. response={response!r}"
        )

    store_secret(tenant_id, app_id, new_secret, backend=keychain)

    messages = [
        f"[apply] a365 fic rotate --app={app_id}",
        f"[apply] OS keychain: hermes-a365.{tenant_id}.{app_id[:8]}… (refreshed)",
    ]
    if expires:
        messages.append(f"[apply] new credential expires: {expires}")
    messages.append("[apply] restart the activity bridge to pick up the new credential")

    return FicRotateResult(
        tenant_id=tenant_id,
        app_id=app_id,
        rotated=True,
        new_secret_stored=True,
        expires=expires,
        raw_response=response,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 fic rotate — rotate the user-FIC for the T2 confidential client.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="execute the rotation; default is dry-run"
    )
    args = parser.parse_args(argv)

    try:
        tenant_id, app_id = plan_fic_rotate()
    except FicRotateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(
        "[plan] hermes a365 fic rotate\n"
        f"  tenant: {tenant_id}\n"
        f"  T2 appId: {app_id}\n"
        "  would re-issue user-FIC and refresh the OS keychain entry.\n"
    )

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to rotate.\n")
        return 0

    try:
        result = apply_fic_rotate(
            tenant_id=tenant_id,
            app_id=app_id,
            mutator=get_mutator(),
            keychain=get_backend(),
        )
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except FicRotateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
