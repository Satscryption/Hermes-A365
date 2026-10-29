#!/usr/bin/env python3
# ruff: noqa: E501
"""Regenerate docs/surface/public-surface.json and docs/SURFACE.md from code + surface.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hermes_a365.surface_collect import (
    collect_adapter_public_members,
    collect_cli_trees,
    collect_keychain_cli_surface,
    collect_product_http_routes,
    collect_python_module_surface,
    collect_task_fetch_card_from_invoke,
    scan_env_literals,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = REPO_ROOT / "docs" / "surface" / "public-surface.json"
MD_PATH = REPO_ROOT / "docs" / "SURFACE.md"


def _packaging_section() -> dict[str, Any]:
    import tomllib

    from hermes_a365.surface import D7_SHIP_PY_TYPED

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    return {
        "name": project["name"],
        "version": project["version"],
        "requires_python": project["requires-python"],
        "license": project.get("license"),
        "classifiers": list(project.get("classifiers", [])),
        "urls": dict(project.get("urls", {})),
        "scripts": dict(project.get("scripts", {})),
        "entry_points": project.get("entry-points", {}),
        "optional_dependencies": list(project.get("optional-dependencies", {}).keys()),
        "py_typed": D7_SHIP_PY_TYPED,
    }


def _load_surface_constants() -> dict[str, Any]:
    from hermes_a365 import surface as s

    return {
        "owner_decisions": {
            "status": s.OWNER_DECISIONS_STATUS,
            "accepted": dict(s.ACCEPTED_OWNER_DECISIONS),
            "D1_serve_supported": s.D1_SERVE_SUPPORTED,
            "D2_cea_default_scopes": list(s.D2_CEA_SCOPES),
            "D3_task_fetch_card_frozen": s.D3_TASK_FETCH_CARD_FROZEN,
            "D4_security_latest_1x_minor_only": s.D4_SECURITY_LATEST_1X_MINOR_ONLY,
            "D6_auto_migrate_additive_only": s.D6_AUTO_MIGRATE_ADDITIVE_ONLY,
            "D7_ship_py_typed": s.D7_SHIP_PY_TYPED,
            "D8_keychain_public": s.D8_KEYCHAIN_PUBLIC,
            "D8_responder_reference_only": s.D8_RESPONDER_REFERENCE_ONLY,
        },
        "env_classification": dict(s.ENV_CLASSIFICATION),
        "instance_env_public_keys": list(s.INSTANCE_ENV_PUBLIC_KEYS),
        "gateway_extra_keys": list(s.GATEWAY_EXTRA_KEYS),
        "a365_config_fields": list(s.A365_CONFIG_FIELDS),
        "plugin_requires_env": list(s.PLUGIN_REQUIRES_ENV),
        "hermes_owned_sidecars": list(s.HERMES_OWNED_SIDECARS),
        "upstream_sidecars": list(s.UPSTREAM_SIDECARS),
        "not_schema_artifacts": list(s.NOT_SCHEMA_ARTIFACTS),
        "adapter_public_members": sorted(s.ADAPTER_PUBLIC_MEMBERS),
        "keychain_cli": None,
        "python_modules": None,
        "product_http_routes_frozen": [dict(r) for r in s.PRODUCT_HTTP_ROUTES],
        "invoke": {
            "registry": list(s.INVOKE_REGISTRY_PUBLIC),
            "adapter_handlers": list(s.INVOKE_ADAPTER_PUBLIC),
            "task_fetch_card": dict(s.TASK_FETCH_CARD),
        },
        "exit_codes": s.EXIT_CODES,
        "sidecar_migration_policy": s.SIDECAR_MIGRATION_POLICY,
        "sidecar_migration_today": s.SIDECAR_MIGRATION_TODAY,
        "security_support_after_v1": s.SECURITY_SUPPORT_AFTER_V1,
        "register_aiteammate_refusal": None,
        "publish_default_scopes": list(s.D2_CEA_SCOPES),
    }


def build_snapshot() -> dict[str, Any]:
    import asyncio

    from hermes_a365 import __version__
    from hermes_a365.register import AITEAMMATE_REGISTER_UNSUPPORTED

    scanned_env = scan_env_literals()
    constants = _load_surface_constants()
    constants["register_aiteammate_refusal"] = AITEAMMATE_REGISTER_UNSUPPORTED
    constants["python_modules"] = collect_python_module_surface()
    constants["keychain_cli"] = collect_keychain_cli_surface()
    constants["invoke"]["task_fetch_card"] = asyncio.run(collect_task_fetch_card_from_invoke())

    return {
        "schema_version": 1,
        "package_version": __version__,
        **constants,
        "env_literals_scanned": sorted(scanned_env),
        "cli": collect_cli_trees(),
        "http_routes_live": collect_product_http_routes(),
        "adapter_members_live": sorted(collect_adapter_public_members()),
        "packaging": _packaging_section(),
    }


def render_markdown(snapshot: dict[str, Any]) -> str:
    od = snapshot["owner_decisions"]
    lines = [
        "# Hermes-A365 public surface (frozen inventory)",
        "",
        f"Package version **{snapshot['package_version']}**. "
        "Machine-readable snapshot: [`docs/surface/public-surface.json`](surface/public-surface.json). "
        "Regenerate with `scripts/render_surface.py --write`; CI runs `--check`.",
        "",
        "## Owner decisions (accepted)",
        "",
        f"Status: **{od['status']}** (recorded in `src/hermes_a365/surface.py`).",
        "",
    ]
    for key in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"):
        lines.append(f"- **{key}** {od['accepted'][key]}")
    lines.extend(
        [
            "",
            f"- **D1 constant** `D1_SERVE_SUPPORTED={od['D1_serve_supported']}`.",
            f"- **D2 constant** `D2_CEA_SCOPES={tuple(od['D2_cea_default_scopes'])}`.",
            f"- **D3 constant** `D3_TASK_FETCH_CARD_FROZEN={od['D3_task_fetch_card_frozen']}`.",
            f"- **D4 constant** `D4_SECURITY_LATEST_1X_MINOR_ONLY={od['D4_security_latest_1x_minor_only']}`.",
            f"- **D6 constant** `D6_AUTO_MIGRATE_ADDITIVE_ONLY={od['D6_auto_migrate_additive_only']}`.",
            f"- **D7 constant** `D7_SHIP_PY_TYPED={od['D7_ship_py_typed']}`.",
            f"- **D8 constants** `D8_KEYCHAIN_PUBLIC={od['D8_keychain_public']}`, "
            f"`D8_RESPONDER_REFERENCE_ONLY={od['D8_responder_reference_only']}`.",
            "",
            "## CLI",
            "",
            "Verbs: `doctor`, `license`, `register`, `consent`, `instance create`, `publish`, "
            "`status`, `cleanup`, `activity-bridge` (`verify` / `serve` / `update-endpoint`), "
            "`bot-service` (`create` / `enable-channel` / `update-endpoint` / `cleanup` / `verify`). "
            "Both `hermes-a365` and `hermes a365` share identical argparse trees (see JSON). "
            "Help strings are not frozen except `register --aiteammate` refusal (exit 2).",
            "",
            "### Exit codes",
            "",
            "```json",
            json.dumps(snapshot["exit_codes"], indent=2, sort_keys=True),
            "```",
            "",
            "## Environment",
            "",
            "Scanner classifies every `A365_*` / `HERMES_*` string literal in `src/hermes_a365`. "
            "Per-agent `.env` keys from `instance create` are listed separately.",
            "",
            "### Public env literals (scanner)",
            "",
            ", ".join(
                sorted(k for k, v in snapshot["env_classification"].items() if v == "public")
            ),
            "",
            "### Instance create `.env` keys",
            "",
            ", ".join(snapshot["instance_env_public_keys"]),
            "",
            "### Internal env literals",
            "",
            ", ".join(
                sorted(k for k, v in snapshot["env_classification"].items() if v == "internal")
            )
            or "(none)",
            "",
            "## Gateway config",
            "",
            f"`platforms.agent365.extra` keys: {', '.join(snapshot['gateway_extra_keys'])}.",
            "",
            f"`A365Config` fields: {', '.join(snapshot['a365_config_fields'])} (upstream `a365.config.json`; no `schemaVersion`).",
            "",
            f"`plugin.yaml` `requires_env`: {', '.join(snapshot['plugin_requires_env'])}.",
            "",
            "## Sidecars",
            "",
            snapshot["sidecar_migration_policy"],
            "",
            "## Python API",
            "",
            "- Console script `hermes-a365`; entry point `hermes_agent.plugins` / `agent365` → `hermes_a365.plugin:register`.",
            f"- `hermes_a365.__version__` → `{snapshot['package_version']}`.",
            f"- `Agent365Adapter` public members: {', '.join(snapshot['adapter_public_members'])}.",
            f"- Public `python -m`: `{snapshot['keychain_cli']['module']}` subcommands "
            f"{', '.join(snapshot['keychain_cli']['subcommands'])}.",
            "",
            "## HTTP (product)",
            "",
            "Gateway `build_app` and `activity-bridge serve`:",
            "",
        ]
    )
    for route in snapshot["product_http_routes_frozen"]:
        lines.append(f"- `{route['method']} {route['path']}`")
    lines.extend(
        [
            "",
            "`GET /healthz` → 200 `{ok, slug, blueprint_client_id}` (id truncated to 8 chars). "
            "`POST /api/messages`: 400 bad activity; 401 bearer; 403 serviceUrl / allowlist / user; "
            "200 ack bodies; plugin-only 503 while connect failed. "
            '`serve` speaks operator webhook contract version `"1"` (`references/webhook-contract.md`).',
            "",
            "## Invoke wire names",
            "",
            f"- Registry: {', '.join(snapshot['invoke']['registry'])}.",
            f"- Adapter handlers: {', '.join(snapshot['invoke']['adapter_handlers'])}.",
            f"- `task/fetch` card (D3): Adaptive Card {snapshot['invoke']['task_fetch_card']['adaptive_card_version']}, "
            f"title `{snapshot['invoke']['task_fetch_card']['title']}`, "
            f"text `{snapshot['invoke']['task_fetch_card']['body_text']}`.",
            "",
            "## Packaging (snapshot; slice 3 may change metadata)",
            "",
            f"Requires Python {snapshot['packaging']['requires_python']}. "
            f"Extras: {', '.join(snapshot['packaging']['optional_dependencies'])}.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="fail if generated files differ")
    group.add_argument("--write", action="store_true", help="write JSON + SURFACE.md")
    args = parser.parse_args(argv)

    snapshot = build_snapshot()
    json_text = _canonical_json(snapshot)
    md_text = render_markdown(snapshot)

    if args.write:
        JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSON_PATH.write_text(json_text, encoding="utf-8")
        MD_PATH.write_text(md_text, encoding="utf-8")
        return 0

    errors: list[str] = []
    if not JSON_PATH.exists():
        errors.append(f"missing {JSON_PATH.relative_to(REPO_ROOT)}")
    elif JSON_PATH.read_text(encoding="utf-8") != json_text:
        errors.append(f"out of date: {JSON_PATH.relative_to(REPO_ROOT)}")

    if not MD_PATH.exists():
        errors.append(f"missing {MD_PATH.relative_to(REPO_ROOT)}")
    elif MD_PATH.read_text(encoding="utf-8") != md_text:
        errors.append(f"out of date: {MD_PATH.relative_to(REPO_ROOT)}")

    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        print("Run: scripts/render_surface.py --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
