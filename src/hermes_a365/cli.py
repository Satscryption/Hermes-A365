"""Standalone ``hermes-a365`` CLI entry point.

Same verb surface as the Hermes plugin path (``hermes a365 <verb>``),
but reachable without the Hermes harness — for operators who installed
``hermes-a365`` via ``pipx`` to drive ``register`` / ``cleanup`` / etc.
ahead of (or independently of) a Hermes gateway.

Both this entry point and ``hermes_a365.plugin.cli.register_cli`` build
the same argparse tree and dispatch to ``<module>.run(args)`` — see
slice 19x-a for the build_parser+run factoring on each module.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``hermes-a365`` parser."""
    from hermes_a365 import activity_bridge as _activity_bridge
    from hermes_a365 import cleanup as _cleanup
    from hermes_a365 import consent as _consent
    from hermes_a365 import doctor as _doctor
    from hermes_a365 import instance_create as _instance_create
    from hermes_a365 import license as _license
    from hermes_a365 import publish as _publish
    from hermes_a365 import register as _register
    from hermes_a365 import status as _status

    parser = argparse.ArgumentParser(
        prog="hermes-a365",
        description=(
            "Microsoft Agent 365 wrapper for Hermes — setup, status, "
            "cleanup, and the Bot Framework activity bridge that backs "
            "the agent365 gateway platform."
        ),
    )
    subs = parser.add_subparsers(dest="a365_command")

    _doctor.build_parser(subs.add_parser("doctor", help="Read-only environment probe"))
    _license.build_parser(
        subs.add_parser("license", help="Recommend an A365 license model (read-only)")
    )
    _register.build_parser(
        subs.add_parser(
            "register",
            help="Orchestrate `a365 setup blueprint` + permissions (mcp + bot)",
        )
    )
    _consent.build_parser(
        subs.add_parser("consent", help="Render admin-consent URL and poll for grant")
    )

    instance_p = subs.add_parser("instance", help="Manage per-agent runtime instances")
    instance_subs = instance_p.add_subparsers(dest="instance_command")
    _instance_create.build_parser(
        instance_subs.add_parser(
            "create", help="Write the per-agent runtime .env file"
        )
    )

    _publish.build_parser(
        subs.add_parser(
            "publish", help="Package the agent manifest for admin-centre upload"
        )
    )
    _status.build_parser(subs.add_parser("status", help="Per-component status report"))
    _cleanup.build_parser(
        subs.add_parser("cleanup", help="Destructive teardown of an A365 agent")
    )
    _activity_bridge.build_parser(
        subs.add_parser(
            "activity-bridge",
            help="Bot Framework adapter daemon (verify / serve / update-endpoint)",
        )
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    sub = getattr(args, "a365_command", None)
    if not sub:
        parser.print_help()
        return 2

    if sub == "doctor":
        from hermes_a365 import doctor
        return doctor.run(args)
    if sub == "license":
        from hermes_a365 import license as _license
        return _license.run(args)
    if sub == "register":
        from hermes_a365 import register
        return register.run(args)
    if sub == "consent":
        from hermes_a365 import consent
        return consent.run(args)
    if sub == "instance":
        instance_sub = getattr(args, "instance_command", None)
        if instance_sub == "create":
            from hermes_a365 import instance_create
            return instance_create.run(args)
        print("usage: hermes-a365 instance {create}", file=sys.stderr)
        return 2
    if sub == "publish":
        from hermes_a365 import publish
        return publish.run(args)
    if sub == "status":
        from hermes_a365 import status
        return status.run(args)
    if sub == "cleanup":
        from hermes_a365 import cleanup
        return cleanup.run(args)
    if sub == "activity-bridge":
        from hermes_a365 import activity_bridge
        return activity_bridge.run(args)

    print(f"unknown subcommand: {sub}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
