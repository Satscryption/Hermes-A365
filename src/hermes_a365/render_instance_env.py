"""Render the per-agent ``.env`` file for ``hermes a365 instance create``.

The output is the file written to ``~/.hermes/agents/<slug>/.env``.
Note that the blueprint client secret
is **never** written to this file; the activity bridge pulls it from
the OS keychain (or, on macOS / Linux where DPAPI isn't available,
from ``a365.generated.config.json`` per slice 18i's gitignore).

Programmatic use::

    from hermes_a365.render_instance_env import InstanceEnvInputs, render_instance_env
    text = render_instance_env(InstanceEnvInputs(
        agent_identity="inbox-helper",
        owner="sadiq@contoso.com",
        owner_aad_id="00000000-0000-0000-0000-000000000001",
        a365_app_id="00000000-0000-0000-0000-00000000aaa1",
        a365_tenant_id="contoso.onmicrosoft.com",
        aa_instance_id="550e8400-e29b-41d4-a716-446655440000",
        hermes_otlp_endpoint="https://contoso.otel.agent365.microsoft.com",
    ))

CLI use::

    python -m hermes_a365.render_instance_env \\
        --agent-identity inbox-helper \\
        --owner sadiq@contoso.com \\
        --owner-aad-id <oid> \\
        --a365-app-id <appId> \\
        --a365-tenant-id contoso.onmicrosoft.com \\
        --hermes-otlp-endpoint <url>
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass

from ._common import jinja_env


@dataclass
class InstanceEnvInputs:
    agent_identity: str
    owner: str
    owner_aad_id: str
    a365_app_id: str
    a365_tenant_id: str
    hermes_otlp_endpoint: str
    aa_instance_id: str | None = None  # generated if None
    business_hours_tz: str | None = None
    business_hours_start: str | None = None
    business_hours_end: str | None = None

    def __post_init__(self) -> None:
        if self.aa_instance_id is None:
            self.aa_instance_id = str(uuid.uuid4())


def render_instance_env(inputs: InstanceEnvInputs) -> str:
    """Render the per-agent .env content as a string (with trailing newline)."""
    env = jinja_env()
    template = env.get_template("instance.env.j2")
    return template.render(
        agent_identity=inputs.agent_identity,
        owner=inputs.owner,
        owner_aad_id=inputs.owner_aad_id,
        a365_app_id=inputs.a365_app_id,
        a365_tenant_id=inputs.a365_tenant_id,
        aa_instance_id=inputs.aa_instance_id,
        hermes_otlp_endpoint=inputs.hermes_otlp_endpoint,
        business_hours_tz=inputs.business_hours_tz,
        business_hours_start=inputs.business_hours_start,
        business_hours_end=inputs.business_hours_end,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the per-agent A365 instance .env file to stdout."
    )
    parser.add_argument("--agent-identity", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--owner-aad-id", required=True)
    parser.add_argument("--a365-app-id", required=True)
    parser.add_argument("--a365-tenant-id", required=True)
    parser.add_argument("--hermes-otlp-endpoint", required=True)
    parser.add_argument("--aa-instance-id", help="UUID; generated if omitted")
    parser.add_argument("--business-hours-tz")
    parser.add_argument("--business-hours-start")
    parser.add_argument("--business-hours-end")
    args = parser.parse_args(argv)

    inputs = InstanceEnvInputs(
        agent_identity=args.agent_identity,
        owner=args.owner,
        owner_aad_id=args.owner_aad_id,
        a365_app_id=args.a365_app_id,
        a365_tenant_id=args.a365_tenant_id,
        hermes_otlp_endpoint=args.hermes_otlp_endpoint,
        aa_instance_id=args.aa_instance_id,
        business_hours_tz=args.business_hours_tz,
        business_hours_start=args.business_hours_start,
        business_hours_end=args.business_hours_end,
    )
    sys.stdout.write(render_instance_env(inputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
