"""Render an A365 agent blueprint JSON from inputs.

Spec: SPEC.md §6.4. The output shape is approximate — Microsoft does not
publish a JSON Schema for blueprints; we author by example and warn on
unknown properties via the doctor at registration time.

Programmatic use::

    from render_blueprint import BlueprintInputs, render_blueprint_json
    payload = render_blueprint_json(BlueprintInputs(
        slug="inbox-helper",
        description="Summarises unread mail",
        purpose="productivity",
        workiq_tools=["mail", "calendar"],
    ))

CLI use::

    python scripts/render_blueprint.py \\
        --slug inbox-helper \\
        --description "Summarises unread mail" \\
        --purpose productivity \\
        --workiq mail,calendar
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from _common import jinja_env

DEFAULT_DLP = "default-restricted"
DEFAULT_EXTERNAL_ACCESS = "tenant-only"
DEFAULT_LOGGING = "verbose"
DEFAULT_OPTIONAL_CLAIMS: tuple[str, ...] = ("oid", "tid", "preferred_username")
DEFAULT_APP_ROLES: tuple[str, ...] = ("User",)

WORKIQ_TOOLS: frozenset[str] = frozenset(
    {
        "mail",
        "calendar",
        "sharepoint",
        "teams",
        "tasks",
        "people",
    }
)


@dataclass
class BlueprintInputs:
    slug: str
    description: str
    purpose: str
    functions: list[str] = field(default_factory=list)
    app_roles: list[str] = field(default_factory=lambda: list(DEFAULT_APP_ROLES))
    optional_claims: list[str] = field(default_factory=lambda: list(DEFAULT_OPTIONAL_CLAIMS))
    dlp_policy: str = DEFAULT_DLP
    external_access_policy: str = DEFAULT_EXTERNAL_ACCESS
    logging_policy: str = DEFAULT_LOGGING
    workiq_tools: list[str] = field(default_factory=list)
    display_name: str | None = None  # defaults to slug

    def __post_init__(self) -> None:
        if self.display_name is None:
            self.display_name = self.slug
        unknown = set(self.workiq_tools) - WORKIQ_TOOLS
        if unknown:
            raise ValueError(
                f"unknown workiq tools: {sorted(unknown)}. Allowed: {sorted(WORKIQ_TOOLS)}"
            )


def render_blueprint(inputs: BlueprintInputs) -> dict[str, Any]:
    """Render the blueprint as a Python dict (canonical shape; sortable)."""
    env = jinja_env()
    template = env.get_template("blueprint.json.j2")
    rendered = template.render(
        slug=inputs.slug,
        display_name=inputs.display_name,
        description=inputs.description,
        purpose=inputs.purpose,
        functions=list(inputs.functions),
        app_roles=list(inputs.app_roles),
        optional_claims=list(inputs.optional_claims),
        dlp_policy=inputs.dlp_policy,
        external_access_policy=inputs.external_access_policy,
        logging_policy=inputs.logging_policy,
        workiq_tools=list(inputs.workiq_tools),
    )
    return json.loads(rendered)


def render_blueprint_json(inputs: BlueprintInputs, *, indent: int = 2) -> str:
    """Render the blueprint as canonicalised JSON text (sorted, trailing newline)."""
    payload = render_blueprint(inputs)
    return json.dumps(payload, indent=indent, sort_keys=True) + "\n"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an A365 agent blueprint JSON to stdout.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--workiq", default="", help="comma-separated Work IQ tools")
    parser.add_argument("--functions", default="", help="comma-separated function names")
    parser.add_argument("--dlp", default=DEFAULT_DLP)
    parser.add_argument("--external-access", default=DEFAULT_EXTERNAL_ACCESS)
    parser.add_argument("--logging", default=DEFAULT_LOGGING)
    args = parser.parse_args(argv)

    inputs = BlueprintInputs(
        slug=args.slug,
        description=args.description,
        purpose=args.purpose,
        display_name=args.display_name,
        workiq_tools=_split_csv(args.workiq),
        functions=_split_csv(args.functions),
        dlp_policy=args.dlp,
        external_access_policy=args.external_access,
        logging_policy=args.logging,
    )
    sys.stdout.write(render_blueprint_json(inputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
