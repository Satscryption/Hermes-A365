"""hermes a365 register — orchestrate `a365 setup blueprint` + permissions.

Composes the three real CLI steps the operator needs to bootstrap an
Agent 365 blueprint, in order:

1. ``a365 setup blueprint --agent-name <name>`` — registers the Entra app
   that backs the blueprint.
2. ``a365 setup permissions mcp --agent-name <name>`` — configures
   MCP-server OAuth grants + inheritable permissions.
3. ``a365 setup permissions bot --agent-name <name>`` — configures the
   Messaging Bot API OAuth grants.

The CLI itself is responsible for idempotency, JSON shape, and Azure /
Entra round-trips. Our job is to:

- Compose the right ``argv`` for each step from operator inputs.
- Run them in order via the :class:`Mutator` protocol (see ``mutator.py``).
- Surface ``AADSTS500011`` retries (license propagation) and
  ``AADSTS90094`` (admin consent required → defer to ``hermes a365 consent``).
- Update ``a365.config.json`` if the operator wants the file form rather
  than passing ``--agent-name`` on every call.

Default mode is dry-run (prints the planned argv list without running);
``--apply`` executes each step.

Operator prerequisites the CLI itself enforces (run ``a365 setup
requirements`` to validate):

- PowerShell 7+ on PATH (the CLI shells out for some operations).
- Tenant enrolled in Microsoft's Frontier Preview Program.
- A custom Entra client app named "Agent 365 CLI" with delegated Graph
  permissions and admin-consent granted.
- ``az login`` with at least Agent ID Developer role; Global Administrator
  is required for the ``setup permissions *`` steps.

See ``references/a365-cli-reference.md`` for the verified command surface.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._common import (
    TenantResolutionError,
    active_az_tenant,
    resolve_expected_tenant,
    tenant_ids_match,
    write_owner_only_text_atomic,
)
from .a365_config import A365Config, merge, read, write_atomic
from .mutator import (
    AADSTS_CONSENT_REQUIRED,
    AADSTS_LICENSE_NOT_PROPAGATED,
    AADSTSError,
    CliInvocationError,
    Mutator,
    RunResult,
    get_mutator,
)

# Re-export for cleanup.py / instance_create.py until those are rewritten in
# Slices 18c/18d. New code should import from `mutator` directly.
__all__ = [
    "AADSTSError",
    "ApplyResult",
    "Mutator",
    "RegisterError",
    "RegisterInputs",
    "RegisterPlan",
    "RegisterStep",
    "SecretRecoveryOutcome",
    "apply_register_plan",
    "auto_recover_secret",
    "build_register_plan",
    "default_recovery_display_name",
    "detect_missing_secret",
    "get_mutator",
    "main",
    "report_missing_secret_warning",
]

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 30.0

VALID_AUTH_MODES: frozenset[str] = frozenset({"obo", "s2s", "both"})
AITEAMMATE_REGISTER_UNSUPPORTED = (
    "`register --aiteammate` is unsupported. Register only runs "
    "`a365 setup blueprint` plus MCP/Bot permission setup; the AI Teammate "
    "agentic user is created/activated after `publish --aiteammate` via "
    "M365 Admin Centre upload and per-user activation."
)

GENERATED_CONFIG_FILENAME = "a365.generated.config.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegisterError(RuntimeError):
    """Raised when register can't proceed (bad inputs, missing prereqs)."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class RegisterInputs:
    """User-supplied arguments for the register orchestration."""

    agent_name: str
    tenant_id: str | None = None  # None → CLI auto-detects from `az account show`
    m365: bool = False  # register messaging endpoint via MCP Platform
    aiteammate: bool = False  # Parser back-compat; rejected in __post_init__.
    authmode: str = "obo"  # obo / s2s / both — only used by `setup all`, kept here for config
    no_endpoint: bool = False  # blueprint-only; skip endpoint registration
    skip_requirements: bool = False  # pass --skip-requirements to setup blueprint

    def __post_init__(self) -> None:
        if not self.agent_name:
            raise ValueError("agent_name must be non-empty")
        if self.aiteammate:
            raise ValueError(AITEAMMATE_REGISTER_UNSUPPORTED)
        if self.authmode not in VALID_AUTH_MODES:
            raise ValueError(
                f"authmode must be one of {sorted(VALID_AUTH_MODES)}, got {self.authmode!r}"
            )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class RegisterStep:
    """One ordered step in the register orchestration."""

    name: str  # "blueprint" / "permissions-mcp" / "permissions-bot"
    argv: list[str]
    description: str


@dataclass
class RegisterPlan:
    inputs: RegisterInputs
    steps: list[RegisterStep]

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 register {self.inputs.agent_name}"]
        if self.inputs.tenant_id:
            lines.append(f"  tenant: {self.inputs.tenant_id}")
        else:
            lines.append("  tenant: (auto-detect from `az account show`)")
        lines.append("  steps (in order):")
        for s in self.steps:
            lines.append(f"    - {s.name:<18} {s.description}")
            # shlex.join (slice 18p, bug #7) keeps multi-word values like
            # `--agent-name "Hermes Inbox Helper"` quoted so an operator
            # who copy-pastes the line gets a working shell command.
            lines.append(f"      $ {shlex.join(s.argv)}")
        return "\n".join(lines)


def _common_flags(inputs: RegisterInputs, *, agent_name: bool = True) -> list[str]:
    """Flags shared across every `a365 setup *` step we drive."""
    flags: list[str] = []
    if agent_name:
        flags.extend(["--agent-name", inputs.agent_name])
    if inputs.tenant_id:
        flags.extend(["--tenant-id", inputs.tenant_id])
    return flags


def build_register_plan(inputs: RegisterInputs) -> RegisterPlan:
    """Compose the ordered argv list for the orchestration."""
    steps: list[RegisterStep] = []

    blueprint_argv = ["a365", "setup", "blueprint", *_common_flags(inputs)]
    if inputs.m365:
        blueprint_argv.append("--m365")
    if inputs.no_endpoint:
        blueprint_argv.append("--no-endpoint")
    if inputs.skip_requirements:
        blueprint_argv.append("--skip-requirements")
    steps.append(
        RegisterStep(
            name="blueprint",
            argv=blueprint_argv,
            description="create agent blueprint (Entra app + service principal)",
        )
    )

    perm_mcp_argv = ["a365", "setup", "permissions", "mcp", *_common_flags(inputs)]
    steps.append(
        RegisterStep(
            name="permissions-mcp",
            argv=perm_mcp_argv,
            description="configure MCP server OAuth grants",
        )
    )

    perm_bot_argv = ["a365", "setup", "permissions", "bot", *_common_flags(inputs)]
    steps.append(
        RegisterStep(
            name="permissions-bot",
            argv=perm_bot_argv,
            description="configure Messaging Bot API OAuth grants",
        )
    )

    return RegisterPlan(inputs=inputs, steps=steps)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    plan: RegisterPlan
    completed: list[str] = field(default_factory=list)  # step names that ran successfully
    consent_deferred: bool = False  # AADSTS90094 surfaced; operator must run consent
    not_run: list[str] = field(default_factory=list)  # remaining steps after a defer / failure
    messages: list[str] = field(default_factory=list)
    raw_outputs: dict[str, RunResult] = field(default_factory=dict)


def _run_with_license_retry(
    mutator: Mutator,
    argv: list[str],
    *,
    retries: int,
    backoff: float,
    sleep_fn: Callable[[float], None],
) -> RunResult:
    """Run ``argv``; on AADSTS500011, sleep and retry up to ``retries`` times.

    Other AADSTS codes propagate immediately — they're not retryable.
    """
    last: AADSTSError | None = None
    for attempt in range(retries + 1):
        try:
            return mutator.run(argv)
        except AADSTSError as e:
            if e.code != AADSTS_LICENSE_NOT_PROPAGATED:
                raise
            last = e
            if attempt == retries:
                break
            sleep_fn(backoff)
    assert last is not None
    raise last


def apply_register_plan(
    plan: RegisterPlan,
    *,
    mutator: Mutator,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    config_path: Path | None = None,
) -> ApplyResult:
    """Run each step in order. Stop on a consent-required defer; propagate
    other AADSTS errors and any non-AADSTS CLI failures.

    #102 M7: when ``plan.inputs.tenant_id`` carries a pin (the CLI layer
    resolves it from ``--tenant-id`` or the operator env's ``A365_TENANT_ID``
    before building inputs), the active az session tenant is asserted to
    match BEFORE any provisioning step — the a365 CLI auto-detects its tenant
    from that session, so a mismatched login would provision the agent into
    the wrong tenant. Unpinned inputs (direct API callers, fresh setups with
    nothing to compare against) keep the previous behaviour."""
    result = ApplyResult(plan=plan)

    expected_tenant = (plan.inputs.tenant_id or "").strip()
    if not expected_tenant:
        result.messages.append(
            "[apply] WARNING: no tenant pin (--tenant-id absent and no "
            "A365_TENANT_ID resolved) — provisioning against the ambient "
            "az/a365 session tenant"
        )
    if expected_tenant:
        active = active_az_tenant(lambda argv: mutator.run(argv))
        if active is None:
            raise RegisterError(
                "could not verify the active az tenant (az account show failed "
                f"or returned no tenantId) while a tenant pin exists "
                f"({expected_tenant}); refusing to provision against an "
                "unverifiable session. Run `az login --tenant "
                f"{expected_tenant}` and re-try."
            )
        try:
            tenant_matches = tenant_ids_match(
                active, expected_tenant, source="--tenant-id or A365_TENANT_ID"
            )
        except TenantResolutionError as e:
            raise RegisterError(str(e)) from e
        if not tenant_matches:
            raise RegisterError(
                f"active az tenant {active} does not match the pinned tenant "
                f"{expected_tenant}; refusing to provision into the wrong "
                f"tenant. Run `az login --tenant {expected_tenant}` and re-try."
            )
        result.messages.append(
            f"[apply] tenant pin OK: active az tenant matches {expected_tenant}"
        )

    # Keep the local config mutation behind the same tenant preflight as the
    # cloud steps. A mismatch/unverifiable session must leave existing config
    # untouched so a failed command cannot steer a later run at the rejected
    # tenant.
    if config_path is not None:
        update_config_for_agent(config_path, plan.inputs)

    for i, step in enumerate(plan.steps):
        try:
            run = _run_with_license_retry(
                mutator,
                step.argv,
                retries=retries,
                backoff=backoff,
                sleep_fn=sleep_fn,
            )
        except AADSTSError as e:
            if e.code == AADSTS_CONSENT_REQUIRED:
                result.consent_deferred = True
                result.not_run = [s.name for s in plan.steps[i:]]
                result.messages.append(
                    f"[apply] {step.name}: deferred — admin consent required "
                    f"(AADSTS90094). Run `hermes a365 consent`, then re-run."
                )
                return result
            raise

        result.completed.append(step.name)
        result.raw_outputs[step.name] = run
        result.messages.append(f"[apply] {step.name}: {step.description} — done")

    return result


# ---------------------------------------------------------------------------
# a365.config.json maintenance
# ---------------------------------------------------------------------------


def update_config_for_agent(
    config_path: Path,
    inputs: RegisterInputs,
) -> A365Config:
    """Merge agent-name-derived display names into ``a365.config.json``.

    The CLI accepts either ``--agent-name`` or a config file. Operators
    who run multiple commands benefit from the file form — write it
    here so subsequent ``a365 publish`` / ``cleanup`` invocations don't
    need to repeat the agent name.
    """
    base = read(config_path)
    updates: dict[str, Any] = {
        "agentIdentityDisplayName": f"{inputs.agent_name} Identity",
        "agentBlueprintDisplayName": f"{inputs.agent_name} Blueprint",
    }
    if inputs.tenant_id:
        updates["tenantId"] = inputs.tenant_id
    merged = merge(base, updates)
    write_atomic(config_path, merged)
    return merged


# ---------------------------------------------------------------------------
# Slice 19s — surface + auto-recover the GA CLI client-secret persistence
# regression (#14). After `a365 setup blueprint` runs, the CLI logs
# "Secret stored in generated config" but `agentBlueprintClientSecret`
# can be `null` on disk. Hit on rounds 3, 4, and §9d round-5 of the
# walkthrough. Recovery shape is `az ad app credential reset --append`
# + patch + chmod 0600.
# ---------------------------------------------------------------------------


@dataclass
class SecretRecoveryOutcome:
    """Outcome of the missing-secret detection + recovery pass."""

    detected: bool  # True when CLI claimed success but secret is null/empty
    recovered: bool  # True when --auto-recover-secret succeeded end-to-end
    blueprint_app_id: str | None = None
    messages: list[str] = field(default_factory=list)


def _read_generated_config_dict(path: Path) -> dict[str, Any] | None:
    """Read ``a365.generated.config.json`` as a raw dict.

    Distinct from :func:`a365_config.read` (which models the *input*
    config); the generated config carries server-assigned fields like
    ``agentBlueprintClientSecret`` we don't want to round-trip through
    a typed schema. Returns ``None`` on missing / unreadable / non-dict.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def detect_missing_secret(generated_config_path: Path) -> tuple[bool, str | None]:
    """Detect the "CLI claimed to write secret but didn't" state.

    Returns ``(is_missing, blueprint_app_id)``. The detection requires
    ``agentBlueprintId`` to be populated (so we know the credential
    exists on the Entra app side) AND ``agentBlueprintClientSecret`` to
    be null / empty / missing on disk. Any other shape is "no signal".
    """
    data = _read_generated_config_dict(generated_config_path)
    if not data:
        return False, None
    bp_id = data.get("agentBlueprintId")
    if not isinstance(bp_id, str) or not bp_id:
        return False, None
    secret = data.get("agentBlueprintClientSecret")
    is_missing = not (isinstance(secret, str) and secret)
    return is_missing, bp_id


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Find the first balanced JSON object in ``text`` and parse it.

    The mutator's :func:`_run_streaming` merges stderr into stdout, so
    `az -o json` output is preceded by a one-line credential-protection
    ``WARNING:`` and can be followed by other diagnostic lines. We
    can't ``json.loads`` the raw stream — locate the first ``{`` and
    let :class:`json.JSONDecoder` consume just the object. Returns
    ``None`` when nothing parsable is found.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _build_recovery_argv(
    blueprint_app_id: str, display_name: str, *, years: int = 2
) -> list[str]:
    """The exact `az ad app credential reset` argv we run + suggest."""
    return [
        "az",
        "ad",
        "app",
        "credential",
        "reset",
        "--id",
        blueprint_app_id,
        "--append",
        "--display-name",
        display_name,
        "--years",
        str(years),
        "-o",
        "json",
    ]


def default_recovery_display_name(now: _dt.datetime | None = None) -> str:
    """Timestamped display name for the appended credential."""
    when = now or _dt.datetime.now()
    return f"hermes-bridge-recovery-{when.strftime('%Y%m%dT%H%M%S')}"


def auto_recover_secret(
    generated_config_path: Path,
    blueprint_app_id: str,
    *,
    mutator: Mutator,
    display_name: str,
) -> SecretRecoveryOutcome:
    """Mint a fresh credential via az + patch into the generated config.

    Caller must have already confirmed the missing-secret state via
    :func:`detect_missing_secret`. On success the file is rewritten
    atomically, owner-only from birth (#112 / CS-004). The reset runs
    ``sensitive=True`` so the minted secret in az's JSON output is never
    echoed to stdout (#111 / CS-003). Failures (az invocation error, az
    output without ``.password``, file not on disk) leave the generated
    config untouched and return an outcome with ``recovered=False`` plus
    a paste-ready recovery hint.
    """
    argv = _build_recovery_argv(blueprint_app_id, display_name)
    paste_cmd = shlex.join(argv)
    outcome = SecretRecoveryOutcome(
        detected=True, recovered=False, blueprint_app_id=blueprint_app_id
    )

    try:
        run = mutator.run(argv, sensitive=True)
    except CliInvocationError as e:
        outcome.messages.append(
            f"[recover] `az ad app credential reset` failed: {e}; "
            f"recover by hand: {paste_cmd}"
        )
        return outcome

    payload = _extract_first_json_object(run.stdout) or {}
    new_secret = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(new_secret, str) or not new_secret:
        outcome.messages.append(
            f"[recover] `az` returned no `.password` field; "
            f"recover by hand: {paste_cmd}"
        )
        return outcome

    data = _read_generated_config_dict(generated_config_path)
    if data is None:
        outcome.messages.append(
            f"[recover] {generated_config_path} disappeared between "
            f"detection and recover; secret minted but not persisted. "
            f"Paste manually: {paste_cmd}"
        )
        return outcome

    data["agentBlueprintClientSecret"] = new_secret
    write_owner_only_text_atomic(
        generated_config_path, json.dumps(data, indent=2, sort_keys=True) + "\n"
    )

    outcome.recovered = True
    outcome.messages.append(
        f"[recover] minted fresh credential on app {blueprint_app_id}; "
        f"agentBlueprintClientSecret patched into "
        f"{generated_config_path.name} (mode 0600)."
    )
    return outcome


def report_missing_secret_warning(
    blueprint_app_id: str, generated_config_path: Path
) -> str:
    """Operator-facing warning when detection fires without auto-recovery.

    Carries the exact paste-ready commands, mirroring the operator tone
    of the slice 19g/h orphan recovery hints. The manual patch command
    reads the secret from a hidden ``getpass`` prompt — never from argv,
    which would land it in shell history and the process list (#113 /
    CS-005) — and writes it with the same exclusive-create 0600 ordering
    as the automatic path (#112).
    """
    display_name = default_recovery_display_name()
    az_cmd = shlex.join(_build_recovery_argv(blueprint_app_id, display_name))
    # Reads the secret from a hidden getpass prompt (never argv), pre-unlinks
    # a stale/planted temp so O_EXCL can't lock out a retry, and writes 0600
    # before any bytes (#112/#113).
    patch_code = (
        "import getpass,json,os,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "d=json.loads(p.read_text());"
        "d['agentBlueprintClientSecret']=getpass.getpass('client secret: ');"
        "t=p.with_suffix(p.suffix+'.tmp');"
        "(os.remove(t) if os.path.lexists(t) else None);"
        "fd=os.open(t,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);"
        "os.write(fd,(json.dumps(d,indent=2,sort_keys=True)+chr(10)).encode());"
        "os.close(fd);"
        "os.replace(t,p)"
    )
    # Render the WHOLE command (program, -c, code, and the config path) as a
    # single shlex.join'd argv so a config path with spaces or shell
    # metacharacters is safely quoted — pasting it can't split the path or
    # execute injected commands (review P1). Never splice the path in raw.
    patch_cmd = shlex.join(["python3", "-c", patch_code, str(generated_config_path)])
    return (
        f"[warn] CLI minted a credential on app {blueprint_app_id} but "
        f"did not persist it locally — known regression (#14).\n"
        f"  preferred: re-run with --auto-recover-secret (mints + persists "
        f"without echoing the secret).\n"
        f"  or recover by hand — mint, then paste at the hidden prompt; "
        f"never put the secret on the command line:\n"
        f"    {az_cmd}\n"
        f"    {patch_cmd}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description=(
                "hermes a365 register — orchestrate `a365 setup blueprint` "
                "+ `setup permissions mcp` + `setup permissions bot`."
            ),
        )
    parser.add_argument(
        "--agent-name",
        required=True,
        help="agent base name; CLI derives `<name> Identity` and `<name> Blueprint`",
    )
    parser.add_argument(
        "--tenant-id",
        help="tenant id; default auto-detects via `az account show`",
    )
    parser.add_argument(
        "--m365",
        action="store_true",
        help="register the messaging endpoint via MCP Platform (M365 agent)",
    )
    parser.add_argument(
        "--aiteammate",
        action="store_true",
        help=(
            "deprecated/unsupported on register; AI Teammate activation happens "
            "after `publish --aiteammate` via M365 Admin Centre"
        ),
    )
    parser.add_argument(
        "--authmode",
        choices=sorted(VALID_AUTH_MODES),
        default="obo",
        help="auth pattern recorded in a365.config.json (only enforced by `setup all`)",
    )
    parser.add_argument(
        "--no-endpoint",
        action="store_true",
        help="blueprint-only; skip messaging-endpoint registration",
    )
    parser.add_argument(
        "--skip-requirements",
        action="store_true",
        help="pass --skip-requirements to `a365 setup blueprint`",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute the plan; default is dry-run",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("a365.config.json"),
        help=(
            "path to a365.config.json to update with derived display names "
            "(default ./a365.config.json)"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"AADSTS500011 retries on license propagation (default {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=DEFAULT_BACKOFF_SECONDS,
        help=f"backoff between retries in seconds (default {DEFAULT_BACKOFF_SECONDS:g})",
    )
    parser.add_argument(
        "--auto-recover-secret",
        action="store_true",
        help=(
            "if `a365 setup blueprint` claims to write a client secret "
            "but `agentBlueprintClientSecret` is null on disk (#14), "
            "automatically run `az ad app credential reset --append` "
            "and patch the generated config. Off by default; without "
            "the flag the wrapper just prints a paste-ready hint."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        # #102 M7: resolve the tenant pin up-front (explicit --tenant-id wins,
        # else the operator env's A365_TENANT_ID) so every `a365 setup *` step
        # carries --tenant-id and apply asserts the session matches.
        hermes_home = Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))
        pinned_tenant, _tenant_source = resolve_expected_tenant(
            hermes_home, args.tenant_id
        )
        inputs = RegisterInputs(
            agent_name=args.agent_name,
            tenant_id=pinned_tenant,
            m365=args.m365,
            aiteammate=args.aiteammate,
            authmode=args.authmode,
            no_endpoint=args.no_endpoint,
            skip_requirements=args.skip_requirements,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    plan = build_register_plan(inputs)
    sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to execute.\n")
        return 0

    try:
        result = apply_register_plan(
            plan,
            mutator=get_mutator(),
            retries=args.retries,
            backoff=args.backoff,
            config_path=args.config,
        )
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2
    except CliInvocationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except RegisterError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\n")

    # Slice 19s — surface (and optionally recover) the missing-secret state.
    # Only meaningful if blueprint actually ran; if the apply deferred at
    # the consent step before blueprint, there's no secret to check yet.
    if "blueprint" in result.completed:
        generated_path = Path(GENERATED_CONFIG_FILENAME)
        is_missing, bp_id = detect_missing_secret(generated_path)
        if is_missing and bp_id:
            if args.auto_recover_secret:
                outcome = auto_recover_secret(
                    generated_path,
                    bp_id,
                    mutator=get_mutator(),
                    display_name=default_recovery_display_name(),
                )
                sys.stdout.write("\n" + "\n".join(outcome.messages) + "\n")
            else:
                sys.stdout.write(
                    "\n" + report_missing_secret_warning(bp_id, generated_path) + "\n"
                )

    if result.consent_deferred:
        sys.stdout.write(
            "\nNext: run `hermes a365 consent`, grant admin consent, then re-run "
            "`hermes a365 register --apply` to finish.\n"
        )
        return 1
    sys.stdout.write("\ndone. Next: `hermes a365 publish` to package the manifest.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
