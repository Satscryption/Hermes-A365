"""hermes a365 doctor — read-only environment probe.

Targets the .NET ``a365`` CLI (the only variant that ships at GA —
verified 2026-05-04) and checks the prereqs the real CLI's own
``setup requirements`` enforces:

- ``a365`` CLI present + version
- ``az`` CLI present + signed in
- ``pwsh`` (PowerShell 7+) on PATH — the CLI shells out to it for some
  steps; missing pwsh causes ``setup requirements`` to fail.
- Custom Entra client app (named ``Agent 365 CLI`` by Microsoft's
  convention) discoverable via the signed-in ``az`` context.
- Network reachability for ``login.microsoftonline.com`` and
  ``graph.microsoft.com``.
- OS keychain backend.
- Local config (``~/.hermes/.env``).
- Hermes harness version.

Frontier Preview Program enrollment is not auto-verifiable; doctor
mentions it in prose. Deeper, auth-requiring checks (license posture,
tenant-side Azure subscription role) are deferred to ``a365 setup
requirements``, which the operator runs separately when bootstrapping.

Exit codes:
- 0 — all probes ``ok``
- 1 — at least one ``warn``, no ``error``
- 2 — at least one ``error``
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ._common import parse_env, safe_run, tcp_reachable

ProbeState = Literal["ok", "warn", "error"]

_OK = "ok"
_WARN = "warn"
_ERROR = "error"

_DEFAULT_NETWORK_HOSTS = ("login.microsoftonline.com", "graph.microsoft.com")
_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# Microsoft's docs use this display name for the operator-managed custom
# client app the A365 CLI runs as. Operators can name theirs differently
# but the spec / docs default is this string; doctor reports a warning if
# nothing matches, with the URL the operator follows to fix it.
DEFAULT_CLIENT_APP_NAME = "Agent 365 CLI"

# Slice 18r (bug #3): operators with a non-default client-app name can
# set this env var to skip the rename. Looked up at probe time so the
# shell `export` route works without re-importing.
CLIENT_APP_NAME_ENV = "A365_CLIENT_APP_NAME"
CUSTOM_CLIENT_APP_DOCS = (
    "https://learn.microsoft.com/microsoft-agent-365/developer/custom-client-app-registration"
)
FRONTIER_PROGRAM_URL = "https://adoption.microsoft.com/copilot/frontier-program/"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    state: ProbeState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_a365_cli() -> ProbeResult:
    """v0.2 only ships the .NET CLI; no variant detection."""
    binary = shutil.which("a365")
    if not binary:
        return ProbeResult(
            "a365_cli",
            _ERROR,
            "a365 not found on PATH (install via "
            "`dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli --prerelease`)",
        )
    version_text = safe_run([binary, "--version"], timeout=10.0) or ""
    version_first = version_text.splitlines()[0] if version_text else "version unknown"
    return ProbeResult(
        "a365_cli",
        _OK,
        f"present at {binary}; {version_first}",
        {"path": binary, "version_raw": version_text},
    )


def probe_az_cli() -> ProbeResult:
    binary = shutil.which("az")
    if not binary:
        return ProbeResult(
            "az_cli",
            _ERROR,
            "az not found on PATH (install Azure CLI ≥ 2.55.0)",
        )
    signed_in = safe_run([binary, "account", "show", "--query", "user.name", "-o", "tsv"])
    if not signed_in:
        return ProbeResult(
            "az_cli",
            _WARN,
            f"present at {binary}; not signed in (run `az login`)",
            {"path": binary},
        )
    return ProbeResult(
        "az_cli",
        _OK,
        f"present and signed in as {signed_in.strip()} ({binary})",
        {"path": binary, "user": signed_in.strip()},
    )


def probe_powershell() -> ProbeResult:
    """PowerShell 7+ as ``pwsh`` — the A365 CLI shells out to it."""
    binary = shutil.which("pwsh")
    if not binary:
        return ProbeResult(
            "powershell",
            _ERROR,
            "pwsh not found on PATH; install PowerShell 7+ "
            "(https://learn.microsoft.com/powershell/scripting/install/installing-powershell)",
        )
    version_raw = safe_run([binary, "-Command", "$PSVersionTable.PSVersion.ToString()"]) or ""
    version = version_raw.strip() or "unknown"
    if version != "unknown" and not version.startswith(("7.", "8.", "9.")):
        return ProbeResult(
            "powershell",
            _WARN,
            f"present at {binary}; version {version} (CLI requires 7+)",
            {"path": binary, "version": version},
        )
    return ProbeResult(
        "powershell",
        _OK,
        f"present at {binary}; version {version}",
        {"path": binary, "version": version},
    )


def probe_custom_client_app(*, name: str | None = None) -> ProbeResult:
    """Best-effort: ask az whether the operator-managed client app exists.

    A miss isn't fatal — operators may have named the app differently —
    but it's a warn so the operator can confirm. The CLI itself will
    fail clearly later if the app doesn't exist or its permissions are
    wrong (see ``a365 setup requirements`` for the authoritative check).

    If ``name`` is not given, reads ``A365_CLIENT_APP_NAME`` from the
    environment, falling back to :data:`DEFAULT_CLIENT_APP_NAME`. The
    underlying ``a365`` CLI hard-codes the default; operators with a
    different name need to either rename the Entra app to match (the
    appId stays stable) or accept that the CLI itself won't find it
    until they do.
    """
    if name is None:
        name = os.environ.get(CLIENT_APP_NAME_ENV) or DEFAULT_CLIENT_APP_NAME
    if not shutil.which("az"):
        return ProbeResult(
            "custom_client_app",
            _WARN,
            "az CLI not on PATH; cannot look up custom client app",
        )
    out = safe_run(
        [
            "az",
            "ad",
            "app",
            "list",
            "--display-name",
            name,
            "--query",
            "[].appId",
            "-o",
            "tsv",
        ],
        timeout=15.0,
    )
    if out is None:
        # `az ad app list` may also fail if not signed in — surface that.
        return ProbeResult(
            "custom_client_app",
            _WARN,
            f"could not query Entra for app {name!r} (az not signed in?)",
            {"display_name": name},
        )
    out = out.strip()
    if not out:
        return ProbeResult(
            "custom_client_app",
            _WARN,
            f"no Entra app named {name!r} in tenant; register one per {CUSTOM_CLIENT_APP_DOCS}",
            {"display_name": name, "docs": CUSTOM_CLIENT_APP_DOCS},
        )
    app_id = out.splitlines()[0].strip()
    return ProbeResult(
        "custom_client_app",
        _OK,
        f"{name!r} exists (appId={app_id[:8]}…)",
        {"display_name": name, "app_id": app_id},
    )


def probe_network(hosts: tuple[str, ...] = _DEFAULT_NETWORK_HOSTS) -> ProbeResult:
    unreachable = [h for h in hosts if not tcp_reachable(h)]
    if unreachable:
        return ProbeResult(
            "network",
            _ERROR,
            f"unreachable: {unreachable}",
            {"checked": list(hosts), "unreachable": unreachable},
        )
    return ProbeResult(
        "network",
        _OK,
        f"reachable: {list(hosts)}",
        {"checked": list(hosts)},
    )


def probe_keychain() -> ProbeResult:
    if sys.platform == "darwin":
        binary = shutil.which("security")
        if not binary:
            return ProbeResult(
                "keychain",
                _ERROR,
                "macOS `security` not on PATH (Security framework wrapper)",
            )
        return ProbeResult("keychain", _OK, f"macOS Security framework available ({binary})")
    if sys.platform.startswith("linux"):
        binary = shutil.which("secret-tool")
        if not binary:
            return ProbeResult(
                "keychain",
                _ERROR,
                "Linux `secret-tool` not found (install libsecret-tools / libsecret-1-0)",
            )
        return ProbeResult("keychain", _OK, f"libsecret available ({binary})")
    return ProbeResult(
        "keychain",
        _ERROR,
        f"unsupported platform: {sys.platform} (v0.1 supports macOS + Linux)",
    )


def probe_local_config() -> ProbeResult:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    home = Path(os.path.expanduser(raw))
    env_file = home / ".env"
    config_file = home / "config.yaml"

    parts: list[str] = [str(home)]
    state: ProbeState = _OK

    if env_file.exists():
        try:
            parsed = parse_env(env_file.read_text())
        except OSError as e:
            return ProbeResult(
                "local_config",
                _ERROR,
                f".env unreadable: {e}",
                {"home": str(home)},
            )
        parts.append(f".env: {len(parsed)} keys")
    else:
        parts.append(".env: absent or empty")
        state = _WARN

    parts.append("config.yaml: " + ("present" if config_file.exists() else "absent"))

    return ProbeResult(
        "local_config",
        state,
        " | ".join(parts),
        {
            "home": str(home),
            "env_present": env_file.exists(),
            "config_yaml_present": config_file.exists(),
        },
    )


def probe_hermes_harness() -> ProbeResult:
    binary = shutil.which("hermes")
    if not binary:
        return ProbeResult(
            "hermes_harness",
            _WARN,
            "hermes not on PATH (skill loads, but harness CLI unavailable)",
        )
    out = safe_run([binary, "--version"], timeout=10.0) or ""
    return ProbeResult(
        "hermes_harness",
        _OK,
        f"{out.splitlines()[0] if out else 'version unknown'} ({binary})",
        {"path": binary, "version_raw": out},
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class DoctorReport:
    probes: list[ProbeResult]

    @property
    def overall(self) -> ProbeState:
        if any(p.state == _ERROR for p in self.probes):
            return _ERROR
        if any(p.state == _WARN for p in self.probes):
            return _WARN
        return _OK


def overall_to_exit_code(o: ProbeState) -> int:
    return {"ok": 0, "warn": 1, "error": 2}[o]


def run_all_probes(*, no_network: bool = False) -> DoctorReport:
    probes: list[ProbeResult] = []
    probes.append(probe_a365_cli())
    probes.append(probe_az_cli())
    probes.append(probe_powershell())
    probes.append(probe_custom_client_app())
    if not no_network:
        probes.append(probe_network())
    probes.append(probe_keychain())
    probes.append(probe_local_config())
    probes.append(probe_hermes_harness())
    return DoctorReport(probes=probes)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_human(report: DoctorReport) -> str:
    name_w = max((len(p.name) for p in report.probes), default=4)
    name_w = max(name_w, len("probe"))
    lines = ["hermes a365 doctor", "-" * 30]
    for p in report.probes:
        marker = {_OK: "[ ok ]", _WARN: "[WARN]", _ERROR: "[FAIL]"}[p.state]
        lines.append(f"  {marker}  {p.name:<{name_w}}  {p.detail}")
    lines.append("")
    lines.append(f"overall: {report.overall}")
    lines.append("")
    lines.append(
        "Frontier Preview Program enrollment is not auto-verifiable; "
        f"verify your tenant at {FRONTIER_PROGRAM_URL}."
    )
    lines.append("Deeper auth-requiring checks: `a365 setup requirements`")
    return "\n".join(lines) + "\n"


def render_json(report: DoctorReport) -> str:
    return (
        json.dumps(
            {
                "overall": report.overall,
                "probes": [asdict(p) for p in report.probes],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 doctor — read-only environment probe.",
        )
    parser.add_argument("--human", action="store_true", help="formatted output for terminals")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip network reachability probes (offline diagnostic)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    report = run_all_probes(no_network=args.no_network)
    sys.stdout.write(render_human(report) if args.human else render_json(report))
    return overall_to_exit_code(report.overall)


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
