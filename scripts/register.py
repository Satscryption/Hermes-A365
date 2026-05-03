"""hermes a365 register — Entra app registration + user-FIC configuration.

Spec: SPEC.md §6.2. Composes two reconciler plans (T1 first-party, T2
confidential client), drives the underlying ``a365 setup app`` and
``a365 fic configure`` mutations, persists ``A365_TENANT_ID``/``A365_APP_ID``
to ``~/.hermes/.env``, and stores the T2 client secret in the OS keychain.

Default mode is **dry-run** (planning only); pass ``--apply`` to execute.

The actual A365 mutations are mediated by the :class:`Mutator` protocol so
the apply path is unit-testable without a live ``a365`` CLI. The default
implementation, :class:`A365CliMutator`, shells out and translates Microsoft
``AADSTS<code>`` error strings into :class:`AADSTSError` so the apply loop
can implement spec'd retries (``AADSTS500011`` — license not propagated).

Files this command writes:
- ``~/.hermes/.env`` — atomically updated (tmp + rename); existing keys preserved.
- OS keychain entry ``hermes-a365.<tenant>.<appId>`` — T2 secret only,
  via :mod:`secrets`. Never written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from secrets import KeychainBackend, get_backend, store_secret
from typing import Any, Literal, Protocol

from _common import parse_env
from reconcile_app import (
    ActualAppRegistration,
    AppPlan,
    DesiredAppRegistration,
    reconcile_app,
)
from status import QuerySource, get_query_source

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 30.0

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# AADSTS codes the apply loop reacts to. Anything else surfaces as a fatal error.
_AADSTS_LICENSE_NOT_PROPAGATED = "AADSTS500011"
_AADSTS_CONSENT_REQUIRED = "AADSTS90094"
_AADSTS_RE = re.compile(r"AADSTS\d{4,7}")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AADSTSError(RuntimeError):
    """Raised by the mutator when an A365 CLI mutation fails with an AADSTS code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RegisterError(RuntimeError):
    """Raised when register's apply path can't proceed (e.g. abort plan)."""


# ---------------------------------------------------------------------------
# Mutator protocol — wraps the mutating subset of the a365 CLI
# ---------------------------------------------------------------------------


class Mutator(Protocol):
    """Mutating operations against the A365 control plane.

    Implementations either drive a real ``a365`` CLI (production) or record
    the calls (tests). On Azure failure every method raises :class:`AADSTSError`
    when an ``AADSTS<code>`` token is present in the CLI output; other failures
    raise :class:`RuntimeError`.
    """

    available: bool

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]: ...
    def fic_configure(self, *, app_id: str) -> None: ...
    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]: ...
    def create_instance(self, *, blueprint_slug: str, instance_id: str) -> dict[str, Any]: ...


class A365CliMutator:
    """Default :class:`Mutator` — shells out to the ``a365`` CLI.

    ``setup_app`` parses the JSON object the CLI prints on success; the
    expected shape is ``{"appId": str, "secret": str | None}``. ``secret``
    is only populated for T2 (Microsoft emits it once, at creation).

    Defensive parsing: the CLI may print extra log lines around the JSON
    (variants differ here), so we extract the last balanced JSON object
    rather than ``json.loads`` the whole stdout.
    """

    name = "a365-cli"

    def __init__(self) -> None:
        self.available = shutil.which("a365") is not None

    def _run(self, argv: list[str], *, timeout: float = 60.0) -> str:
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
            raise RuntimeError(f"{argv[0]} {argv[1]} failed (rc={proc.returncode}): {combined}")
        return proc.stdout

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("a365 CLI not on PATH; cannot run setup app")
        out = self._run(
            ["a365", "setup", "app", f"--tier={tier}", f"--name={name}", "--output=json"]
        )
        return _extract_json_object(out)

    def fic_configure(self, *, app_id: str) -> None:
        if not self.available:
            raise RuntimeError("a365 CLI not on PATH; cannot configure FIC")
        self._run(["a365", "fic", "configure", f"--app={app_id}"])

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("a365 CLI not on PATH; cannot run setup blueprint")
        out = self._run(["a365", "setup", "blueprint", f"--file={file_path}", "--output=json"])
        return _extract_json_object(out)

    def create_instance(self, *, blueprint_slug: str, instance_id: str) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("a365 CLI not on PATH; cannot run create-instance")
        out = self._run(
            [
                "a365",
                "create-instance",
                f"--blueprint={blueprint_slug}",
                f"--instance={instance_id}",
                "--output=json",
            ]
        )
        return _extract_json_object(out)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the last JSON object out of mixed log/JSON CLI output."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"no JSON object found in CLI output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def get_mutator() -> Mutator:
    return A365CliMutator()


# ---------------------------------------------------------------------------
# Env-file persistence (~/.hermes/.env)
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _format_env(values: dict[str, str]) -> str:
    """Serialise an env dict in deterministic key-sorted order."""
    return "".join(f"{k}={values[k]}\n" for k in sorted(values))


def write_env_atomic(path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Merge ``updates`` into ``path`` (key=value), writing atomically.

    Existing keys not in ``updates`` are preserved. Returns the merged dict
    that was written. Creates parent directories if needed. Atomicity uses
    write-to-tmp + ``os.replace`` so a crash mid-write leaves the original.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        existing = parse_env(path.read_text())
    merged = {**existing, **updates}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_format_env(merged))
    os.replace(tmp, path)
    return merged


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class RegisterInputs:
    app_name: str
    tenant_id: str  # written to ~/.hermes/.env as A365_TENANT_ID
    cli_variant: str | None = None  # optional metadata; written if provided

    def __post_init__(self) -> None:
        if not self.app_name:
            raise ValueError("app_name must be non-empty")
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")

    @property
    def t1_name(self) -> str:
        return self.app_name

    @property
    def t2_name(self) -> str:
        return f"{self.app_name}-conf"


PlanLevelAction = Literal["create", "noop", "patch", "abort"]


@dataclass
class RegisterPlan:
    """Composite plan for the register command — T1, T2, FIC, env, keychain."""

    inputs: RegisterInputs
    t1: AppPlan
    t2: AppPlan
    # Captured app-id when actual already exists (so apply can skip mutate
    # but still write the env file with the correct id on a re-run).
    t1_existing_app_id: str | None = None
    t2_existing_app_id: str | None = None

    @property
    def has_abort(self) -> bool:
        return self.t1.action == "abort" or self.t2.action == "abort"

    @property
    def is_noop(self) -> bool:
        return not self.has_abort and self.t1.action == "noop" and self.t2.action == "noop"

    def render_human(self) -> str:
        prefix = "[plan] hermes a365 register"
        lines: list[str] = [prefix]
        lines.append(f"  T1 first-party app {self.inputs.t1_name!r}      — {_phrase(self.t1)}")
        lines.append(f"  T2 confidential client {self.inputs.t2_name!r} — {_phrase(self.t2)}")
        if self.has_abort:
            return "\n".join(lines)
        fic_phrase = "would configure" if _fic_needed(self.t2) else "already configured"
        lines.append(f"  user-FIC on T2                                 — {fic_phrase}")
        lines.append("  ~/.hermes/.env: would set A365_TENANT_ID, A365_APP_ID")
        if self.t2.action == "create":
            lines.append("  OS keychain entry hermes-a365.<tenant>.<appId> — would store T2 secret")
        return "\n".join(lines)


def _phrase(plan: AppPlan) -> str:
    if plan.action == "create":
        return "would create"
    if plan.action == "noop":
        return "no change"
    if plan.action == "patch":
        return f"would patch ({len(plan.diff)} field(s))"
    return f"abort: {plan.abort_reason}"


def _fic_needed(t2: AppPlan) -> bool:
    if t2.action == "create":
        return True
    return t2.action == "patch" and "fic_configured" in t2.diff


def _query_actual(qs: QuerySource, *, name: str) -> ActualAppRegistration | None:
    """Pull actual state for an app by display name. None if missing or unavailable."""
    if not qs.available:
        return None
    payload = qs.query_app_by_name(name=name)
    if payload is None:
        return None
    return ActualAppRegistration.from_query_json(payload)


def build_register_plan(
    inputs: RegisterInputs,
    *,
    query_source: QuerySource | None = None,
) -> RegisterPlan:
    """Reconcile desired T1/T2 against what the tenant currently holds."""
    qs = query_source or get_query_source()

    t1_actual = _query_actual(qs, name=inputs.t1_name)
    t2_actual = _query_actual(qs, name=inputs.t2_name)

    t1_plan = reconcile_app(
        DesiredAppRegistration(name=inputs.t1_name, tier=1, is_multi_tenant=True),
        t1_actual,
    )
    t2_plan = reconcile_app(
        DesiredAppRegistration(
            name=inputs.t2_name, tier=2, is_multi_tenant=False, fic_required=True
        ),
        t2_actual,
    )

    return RegisterPlan(
        inputs=inputs,
        t1=t1_plan,
        t2=t2_plan,
        t1_existing_app_id=t1_actual.app_id if t1_actual else None,
        t2_existing_app_id=t2_actual.app_id if t2_actual else None,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of executing a register plan."""

    t1_app_id: str
    t2_app_id: str
    t2_secret_stored: bool
    fic_configured: bool
    consent_deferred: bool = False  # AADSTS90094 surfaced — caller should run `consent`
    env_written: dict[str, str] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def _setup_app_with_retry(
    mutator: Mutator,
    *,
    tier: int,
    name: str,
    retries: int,
    backoff: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    """Call ``mutator.setup_app``; on AADSTS500011, back off and retry up to ``retries``.

    Other AADSTS codes propagate immediately — they're not retryable.
    """
    last_exc: AADSTSError | None = None
    for attempt in range(retries + 1):
        try:
            return mutator.setup_app(tier=tier, name=name)
        except AADSTSError as e:
            if e.code != _AADSTS_LICENSE_NOT_PROPAGATED:
                raise
            last_exc = e
            if attempt == retries:
                break
            sleep_fn(backoff)
    assert last_exc is not None
    raise last_exc


def apply_register_plan(
    plan: RegisterPlan,
    *,
    mutator: Mutator,
    keychain: KeychainBackend,
    hermes_home: Path,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ApplyResult:
    """Execute the plan: create/patch apps, configure FIC, persist env + secret.

    Idempotent — re-running on a noop plan touches nothing other than the
    env file (which is rewritten atomically with the same values).
    """
    if plan.has_abort:
        reason = plan.t1.abort_reason or plan.t2.abort_reason or "(unspecified)"
        raise RegisterError(f"refusing to apply: {reason}")

    messages: list[str] = []
    consent_deferred = False

    # --- T1 -----------------------------------------------------------------
    if plan.t1.action == "create":
        t1_result = _setup_app_with_retry(
            mutator,
            tier=1,
            name=plan.inputs.t1_name,
            retries=retries,
            backoff=backoff,
            sleep_fn=sleep_fn,
        )
        t1_app_id = str(t1_result.get("appId") or "")
        if not t1_app_id:
            raise RegisterError("T1 setup app returned no appId")
        messages.append(f"[apply] T1 created: appId={t1_app_id}")
    else:
        t1_app_id = plan.t1_existing_app_id or ""
        if not t1_app_id:
            raise RegisterError("T1 plan is non-create but no existing appId recorded")
        messages.append(f"[apply] T1 exists, appId={_short(t1_app_id)} — no change")

    # --- T2 -----------------------------------------------------------------
    t2_secret: str | None = None
    if plan.t2.action == "create":
        t2_result = _setup_app_with_retry(
            mutator,
            tier=2,
            name=plan.inputs.t2_name,
            retries=retries,
            backoff=backoff,
            sleep_fn=sleep_fn,
        )
        t2_app_id = str(t2_result.get("appId") or "")
        t2_secret = t2_result.get("secret") or None
        if not t2_app_id:
            raise RegisterError("T2 setup app returned no appId")
        if not t2_secret:
            raise RegisterError("T2 setup app returned no client secret")
        messages.append(f"[apply] T2 created: appId={t2_app_id}")
    else:
        t2_app_id = plan.t2_existing_app_id or ""
        if not t2_app_id:
            raise RegisterError("T2 plan is non-create but no existing appId recorded")
        messages.append(f"[apply] T2 exists, appId={_short(t2_app_id)} — no change")

    # --- FIC ----------------------------------------------------------------
    fic_configured = False
    if _fic_needed(plan.t2):
        try:
            mutator.fic_configure(app_id=t2_app_id)
            fic_configured = True
            messages.append("[apply] user-FIC configured on T2")
        except AADSTSError as e:
            if e.code == _AADSTS_CONSENT_REQUIRED:
                consent_deferred = True
                messages.append(
                    "[apply] FIC configure deferred: admin consent required (AADSTS90094)"
                )
            else:
                raise
    else:
        messages.append("[apply] user-FIC already configured — no change")

    # --- secret -------------------------------------------------------------
    secret_stored = False
    if t2_secret:
        store_secret(plan.inputs.tenant_id, t2_app_id, t2_secret, backend=keychain)
        secret_stored = True
        messages.append(
            f"[apply] OS keychain: hermes-a365.{plan.inputs.tenant_id}.{_short(t2_app_id)} (stored)"
        )

    # --- env file -----------------------------------------------------------
    env_updates: dict[str, str] = {
        "A365_TENANT_ID": plan.inputs.tenant_id,
        "A365_APP_ID": t2_app_id,
    }
    if plan.inputs.cli_variant:
        env_updates["A365_CLI_VARIANT"] = plan.inputs.cli_variant
    written = write_env_atomic(hermes_home / ".env", env_updates)
    messages.append(f"[apply] ~/.hermes/.env updated ({len(env_updates)} keys)")

    return ApplyResult(
        t1_app_id=t1_app_id,
        t2_app_id=t2_app_id,
        t2_secret_stored=secret_stored,
        fic_configured=fic_configured,
        consent_deferred=consent_deferred,
        env_written=written,
        messages=messages,
    )


def _short(app_id: str) -> str:
    return f"{app_id[:8]}…" if len(app_id) > 8 else app_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 register — Entra app registration (dry-run by default).",
    )
    parser.add_argument("--app-name", required=True, help="display name for the T1 app")
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="tenant id or domain (written to ~/.hermes/.env as A365_TENANT_ID)",
    )
    parser.add_argument(
        "--cli-variant",
        choices=["atk-npm", "a365-dotnet"],
        help="record A365_CLI_VARIANT in ~/.hermes/.env",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute the plan; default is dry-run",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"AADSTS500011 retries (default {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=DEFAULT_BACKOFF_SECONDS,
        help=f"backoff between retries in seconds (default {DEFAULT_BACKOFF_SECONDS:g})",
    )
    args = parser.parse_args(argv)

    inputs = RegisterInputs(
        app_name=args.app_name,
        tenant_id=args.tenant_id,
        cli_variant=args.cli_variant,
    )

    plan = build_register_plan(inputs)
    sys.stdout.write(plan.render_human() + "\n")

    if plan.has_abort:
        sys.stdout.write("\n")
        return 2

    if not args.apply:
        sys.stdout.write("\nNo mutations made. Re-run with --apply to execute.\n")
        return 0

    if plan.is_noop:
        # Still write the env file so a re-run after a manual edit re-establishes
        # the recorded state. Mutator + keychain are not touched.
        sys.stdout.write("\n")
        # Refresh env file with currently-known ids so it converges.
        if plan.t2_existing_app_id:
            write_env_atomic(
                _resolve_hermes_home() / ".env",
                {
                    "A365_TENANT_ID": inputs.tenant_id,
                    "A365_APP_ID": plan.t2_existing_app_id,
                    **({"A365_CLI_VARIANT": inputs.cli_variant} if inputs.cli_variant else {}),
                },
            )
        sys.stdout.write("done. (no mutations)\n")
        return 0

    try:
        mutator = get_mutator()
        keychain = get_backend()
        result = apply_register_plan(
            plan,
            mutator=mutator,
            keychain=keychain,
            hermes_home=_resolve_hermes_home(),
            retries=args.retries,
            backoff=args.backoff,
        )
    except RegisterError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\n")
    if result.consent_deferred:
        sys.stdout.write("done. Next: hermes a365 consent (admin consent required)\n")
    else:
        sys.stdout.write("done. Next: hermes a365 consent\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
