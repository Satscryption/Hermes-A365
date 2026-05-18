"""CLI commands for the agent365 plugin.

Wires ``hermes a365 <subcommand>`` against the ``hermes_a365`` package
modules. Each subcommand re-uses the module's own ``build_parser`` +
``run`` surface, so ``hermes a365 doctor --help`` shows exactly the same
flags as ``hermes-a365 doctor --help``.

  doctor           — read-only environment probe
  license          — license model recommendation
  register         — orchestrate ``a365 setup blueprint`` + permissions
  consent          — render admin-consent URL and poll
  instance create  — write per-agent runtime .env file
  publish          — package the manifest for admin-centre upload
  status           — per-component status report
  cleanup          — destructive teardown
  activity-bridge  — Bot Framework adapter daemon (verify / serve / update-endpoint)
  bot-service      — Azure Bot Service Path B create / verify
"""

from __future__ import annotations

import argparse
import sys


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes a365`` argparse tree.

    Called by the Hermes plugin loader at gateway / CLI startup via
    ``ctx.register_cli_command(name="a365", setup_fn=register_cli, ...)``.
    """
    # Deferred imports so plugin-load time doesn't pay for them when
    # the operator never invokes the CLI.
    from hermes_a365 import activity_bridge as _activity_bridge
    from hermes_a365 import bot_service as _bot_service
    from hermes_a365 import cleanup as _cleanup
    from hermes_a365 import consent as _consent
    from hermes_a365 import doctor as _doctor
    from hermes_a365 import instance_create as _instance_create
    from hermes_a365 import license as _license
    from hermes_a365 import publish as _publish
    from hermes_a365 import register as _register
    from hermes_a365 import status as _status

    subs = subparser.add_subparsers(dest="a365_command")

    doctor_p = subs.add_parser(
        "doctor", help="Read-only environment probe"
    )
    _doctor.build_parser(doctor_p)

    license_p = subs.add_parser(
        "license", help="Recommend an A365 license model (read-only)"
    )
    _license.build_parser(license_p)

    register_p = subs.add_parser(
        "register",
        help="Orchestrate `a365 setup blueprint` + permissions (mcp + bot)",
    )
    _register.build_parser(register_p)

    consent_p = subs.add_parser(
        "consent", help="Render admin-consent URL and poll for grant"
    )
    _consent.build_parser(consent_p)

    # `instance` namespace; today only `create`. Future verbs (list,
    # delete, show) can attach to the same subparser.
    instance_p = subs.add_parser("instance", help="Manage per-agent runtime instances")
    instance_subs = instance_p.add_subparsers(dest="instance_command")
    instance_create_p = instance_subs.add_parser(
        "create", help="Write the per-agent runtime .env file"
    )
    _instance_create.build_parser(instance_create_p)

    publish_p = subs.add_parser(
        "publish", help="Package the agent manifest for admin-centre upload"
    )
    _publish.build_parser(publish_p)

    status_p = subs.add_parser("status", help="Per-component status report")
    _status.build_parser(status_p)

    cleanup_p = subs.add_parser("cleanup", help="Destructive teardown of an A365 agent")
    _cleanup.build_parser(cleanup_p)

    bridge_p = subs.add_parser(
        "activity-bridge",
        help="Bot Framework adapter daemon (verify / serve / update-endpoint)",
    )
    _activity_bridge.build_parser(bridge_p)

    bot_service_p = subs.add_parser(
        "bot-service",
        help="Manage Path B Azure Bot Service resources (create / verify)",
    )
    _bot_service.build_parser(bot_service_p)

    subparser.set_defaults(func=a365_command)


_USAGE = (
    "usage: hermes a365 "
    "{doctor,license,register,consent,instance,publish,status,cleanup,activity-bridge,bot-service}"
)


def a365_command(args: argparse.Namespace) -> int:
    """Dispatch ``hermes a365 <verb>`` to the matching module's ``run``."""
    sub = getattr(args, "a365_command", None)
    if not sub:
        print(_USAGE)
        return 2

    if sub == "doctor":
        from hermes_a365 import doctor as _doctor
        return _doctor.run(args)
    if sub == "license":
        from hermes_a365 import license as _license
        return _license.run(args)
    if sub == "register":
        from hermes_a365 import register as _register
        return _register.run(args)
    if sub == "consent":
        from hermes_a365 import consent as _consent
        return _consent.run(args)
    if sub == "instance":
        instance_sub = getattr(args, "instance_command", None)
        if instance_sub == "create":
            from hermes_a365 import instance_create as _instance_create
            return _instance_create.run(args)
        print("usage: hermes a365 instance {create}")
        return 2
    if sub == "publish":
        from hermes_a365 import publish as _publish
        return _publish.run(args)
    if sub == "status":
        from hermes_a365 import status as _status
        return _status.run(args)
    if sub == "cleanup":
        from hermes_a365 import cleanup as _cleanup
        return _cleanup.run(args)
    if sub == "activity-bridge":
        from hermes_a365 import activity_bridge as _activity_bridge
        return _activity_bridge.run(args)
    if sub == "bot-service":
        from hermes_a365 import bot_service as _bot_service
        return _bot_service.run(args)

    print(f"unknown subcommand: {sub}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
